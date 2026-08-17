from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .app import BridgeApp
from .config import AppConfig, load_config
from .ilink_api import ILinkError
from .ilink_auth import load_credentials, login_with_qr
from .ilink_client import ILinkClient
from .instance_lock import runtime_instance_lock
from .onboarding import ensure_logins


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="微信 iLink Bot 到 ChatGPT/Codex 的桥接器")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="显示调试日志")
    parser.add_argument("--log-file", help="将日志写入滚动文件，而不是标准错误")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="检查登录并开始监听微信 Bot 消息")
    start.add_argument(
        "--skip-login-check",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    sub.add_parser("setup", help="依次完成微信 iLink 和 ChatGPT Plus 登录")
    doctor = sub.add_parser("doctor", help="检查运行环境")
    doctor.add_argument("--connect", action="store_true", help="同时测试 iLink 连接")
    sub.add_parser("read", help="只读取并输出新的微信 Bot 消息")
    sub.add_parser("chatgpt-login", help="保存 ChatGPT Plus 网页登录状态")
    ilink_login = sub.add_parser("ilink-login", help="扫码登录微信 iLink Bot")
    ilink_login.add_argument("--force", action="store_true", help="忽略现有凭证并重新扫码")
    send = sub.add_parser("send", help="发送一条微信 Bot 测试消息")
    send.add_argument("text", nargs="?", default="连接测试成功")
    send.add_argument("--to", dest="user_id", help="目标 iLink 用户 ID；默认扫码用户")
    return parser


def _ilink_client(config: AppConfig) -> ILinkClient:
    return ILinkClient(
        credentials_path=config.ilink_credentials_file,
        state_path=config.ilink_state_file,
        response_prefix=config.response_prefix,
        max_reply_chars=config.max_reply_chars,
        long_poll_timeout_seconds=config.ilink_long_poll_timeout_seconds,
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
                options = {
                    "user_data_dir": profile,
                    "channel": config.chatgpt_browser_channel,
                    "headless": True,
                }
                if config.chatgpt_proxy_server:
                    options["proxy"] = {"server": config.chatgpt_proxy_server}
                context = playwright.chromium.launch_persistent_context(**options)
                context.close()
        print(f"[OK] ChatGPT 浏览器：{config.chatgpt_browser_channel}")
    except Exception as exc:
        failed = True
        print(f"[FAIL] 无法启动 ChatGPT 自动化浏览器：{exc}")

    if config.chatgpt_profile_dir.is_dir() and any(config.chatgpt_profile_dir.iterdir()):
        print(f"[OK] ChatGPT Plus 登录资料：{config.chatgpt_profile_dir}")
    else:
        failed = True
        print("[FAIL] 尚未保存 ChatGPT Plus 登录状态，请先运行 chatgpt-login")

    try:
        credentials = load_credentials(config.ilink_credentials_file)
        if credentials is None:
            raise RuntimeError("尚未扫码登录")
        print(f"[OK] iLink Bot：{credentials.account_id}")
        print(f"[OK] iLink API：{credentials.base_url}")
    except Exception as exc:
        failed = True
        print(f"[FAIL] iLink 凭证：{exc}；请运行 ilink-login")

    for name, path in config.projects.items():
        if path.is_dir() and (path / ".git").exists():
            print(f"[OK] 项目 {name}: {path}")
        else:
            failed = True
            print(f"[FAIL] 项目 {name} 不是可用 Git 仓库：{path}")

    if connect and not failed:
        client = _ilink_client(config)
        try:
            client.connect()
            time.sleep(0.2)
            client.poll()
            print("[OK] iLink 长轮询已启动")
        except Exception as exc:
            failed = True
            print(f"[FAIL] iLink 连接失败：{exc}")
        finally:
            client.close()
    return 1 if failed else 0


def read_messages(config: AppConfig) -> int:
    client = _ilink_client(config)
    client.connect()
    print("正在等待新的微信 Bot 消息，按 Ctrl+C 停止。")
    try:
        while True:
            for message in client.poll():
                print(json.dumps(asdict(message), ensure_ascii=False), flush=True)
                client.acknowledge(message.key)
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        print("已停止读取消息。")
        return 0
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    handlers: list[logging.Handler]
    if args.log_file:
        log_path = Path(args.log_file).expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers = [
            RotatingFileHandler(
                log_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        ]
    else:
        handlers = [logging.StreamHandler()]
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    try:
        config = load_config(Path(args.config))
        if args.command == "doctor":
            if args.connect:
                with runtime_instance_lock(config.runtime_dir):
                    return doctor(config, True)
            return doctor(config, args.connect)
        with runtime_instance_lock(config.runtime_dir):
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
            if args.command == "ilink-login":
                login_with_qr(
                    config.ilink_credentials_file,
                    api_base_url=config.ilink_api_base_url,
                    force=args.force,
                )
                return 0
            if args.command == "send":
                client = _ilink_client(config)
                try:
                    client.connect()
                    target = (
                        client.target_for(args.user_id)
                        if args.user_id
                        else client.default_target()
                    )
                    client.send(args.text, target)
                    print(f"消息已发送到 {target.user_id}")
                finally:
                    client.close()
                return 0
            if args.command == "setup":
                ensure_logins(config)
                return 0
            if not args.skip_login_check:
                ensure_logins(config)
            BridgeApp(config).run()
        return 0
    except (FileNotFoundError, ValueError, RuntimeError, ILinkError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
