from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from .app import BridgeApp
from .config import AppConfig, load_config
from .wechat_client import WeChatClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PC 微信到 ChatGPT 和本地 Codex 的轻量桥接器")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="显示调试日志")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="开始监听微信消息")
    doctor = sub.add_parser("doctor", help="检查环境；不连接微信")
    doctor.add_argument("--connect", action="store_true", help="同时尝试连接微信和联系人")
    sub.add_parser("read", help="只读取并输出新微信消息，不启动 ChatGPT 或发送回复")
    sub.add_parser("chatgpt-login", help="打开浏览器并保存 ChatGPT Plus 登录状态")
    send = sub.add_parser("send", help="发送一条测试微信消息")
    send.add_argument("text", nargs="?", default="连接测试成功")
    return parser


def _wechat_processes() -> list[str]:
    try:
        import psutil
    except ImportError:
        return []
    result: list[str] = []
    for process in psutil.process_iter(["name", "pid"]):
        try:
            name = process.info.get("name") or ""
            if name.lower() in {"wechat.exe", "weixin.exe"}:
                result.append(f"{name} (PID {process.info['pid']})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def _wechat_client(
    config: AppConfig,
    *,
    use_all_read_filters: bool = False,
) -> WeChatClient:
    return WeChatClient(
        contact=config.contact,
        background_mode=config.background_mode,
        allow_self_messages=config.allow_self_messages,
        voice_recognition=config.voice_recognition,
        voice_retry_count=config.voice_retry_count,
        response_prefix=config.response_prefix,
        max_reply_chars=config.max_reply_chars,
        chat_type=config.chat_type,
        bot_name=config.bot_name,
        message_source=config.wechat_message_source,
        wx_cli_path=config.wx_cli_path,
        wx_cli_username=config.wx_cli_username,
        wx_cli_contacts=config.wx_cli_contacts if use_all_read_filters else None,
        wx_cli_groups=config.wx_cli_groups if use_all_read_filters else None,
        wx_cli_timeout_seconds=config.wx_cli_timeout_seconds,
        require_group_mention=not use_all_read_filters,
    )


def doctor(config: AppConfig, connect: bool) -> int:
    failed = False
    print(f"[OK] Python {sys.version.split()[0]}")
    codex = shutil.which(config.codex_command)
    if codex:
        print(f"[OK] Codex: {codex}")
    else:
        failed = True
        print(f"[FAIL] 找不到 Codex 命令：{config.codex_command}")

    try:
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory(prefix="wechat-codex-browser-check-") as profile:
            with sync_playwright() as playwright:
                launch_options = {
                    "user_data_dir": profile,
                    "channel": config.chatgpt_browser_channel,
                    "headless": True,
                }
                if config.chatgpt_proxy_server:
                    launch_options["proxy"] = {
                        "server": config.chatgpt_proxy_server
                    }
                context = playwright.chromium.launch_persistent_context(
                    **launch_options
                )
                context.close()
        print(f"[OK] ChatGPT Plus 浏览器：{config.chatgpt_browser_channel}")
        if config.chatgpt_proxy_server:
            print(f"[OK] ChatGPT 浏览器代理：{config.chatgpt_proxy_server}")
    except Exception as exc:
        failed = True
        print(f"[FAIL] 无法启动 ChatGPT 自动化浏览器：{exc}")

    if config.chatgpt_profile_dir.is_dir() and any(
        config.chatgpt_profile_dir.iterdir()
    ):
        print(f"[OK] ChatGPT Plus 独立浏览器资料：{config.chatgpt_profile_dir}")
    else:
        failed = True
        print("[FAIL] 尚未保存 ChatGPT Plus 登录状态，请先运行 chatgpt-login")

    processes = _wechat_processes()
    if processes:
        print(f"[OK] 微信进程：{', '.join(processes)}")
    else:
        failed = True
        print("[FAIL] 未发现 PC 微信进程，请先启动并登录微信 4.x")

    print(f"[OK] 联系人：{config.contact}")
    if config.wechat_message_source == "wx_cli":
        try:
            reader_check = _wechat_client(config, use_all_read_filters=True)
            reader_check.connect()
            executable = reader_check._wx_cli_reader.executable
            print(f"[OK] wx-cli：{executable}")
        except Exception as exc:
            failed = True
            print(f"[FAIL] wx-cli：{exc}")
    for name, path in config.projects.items():
        if path.is_dir() and (path / ".git").exists():
            print(f"[OK] 项目 {name}: {path}")
        else:
            failed = True
            print(f"[FAIL] 项目 {name} 不是可用 Git 仓库：{path}")

    if connect:
        client = _wechat_client(config)
        try:
            client.connect()
            count = client.baseline()
            if config.wechat_message_source == "wx_cli":
                print(f"[OK] wx-cli 增量读取已就绪，基线忽略 {count} 条目标会话消息")
            else:
                print(f"[OK] 微信 UI 连接成功，当前窗口读取到 {count} 条文本消息")
        except Exception as exc:
            failed = True
            source_name = (
                "wx-cli 增量读取" if config.wechat_message_source == "wx_cli" else "微信 UI"
            )
            print(f"[FAIL] {source_name}连接失败：{exc}")
    return 1 if failed else 0


def read_messages(config: AppConfig) -> int:
    """Print new messages without starting ChatGPT or invoking WeChat send."""
    client = _wechat_client(config, use_all_read_filters=True)
    client.connect()
    baseline_count = client.baseline()
    print(
        f"消息基线已建立，已忽略 {baseline_count} 条目标会话历史消息；"
        "正在等待新消息，按 Ctrl+C 停止。"
    )
    try:
        while True:
            for message in client.poll():
                print(json.dumps(asdict(message), ensure_ascii=False), flush=True)
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        print("已停止读取消息。")
        return 0


def main(argv: list[str] | None = None) -> int:
    # wxauto may adjust the console code page. Pin Python's streams so Chinese
    # diagnostics remain readable in PowerShell and redirected logs.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = load_config(Path(args.config))
        if args.command == "doctor":
            return doctor(config, args.connect)
        if args.command == "read":
            return read_messages(config)
        if args.command == "chatgpt-login":
            from .chatgpt_runner import login_chatgpt

            login_chatgpt(
                config.chatgpt_profile_dir,
                config.chatgpt_browser_channel,
                config.chatgpt_proxy_server,
            )
            return 0
        if args.command == "send":
            client = _wechat_client(config)
            client.connect()
            client.send(args.text)
            print("消息已发送")
            return 0
        BridgeApp(config).run()
        return 0
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
