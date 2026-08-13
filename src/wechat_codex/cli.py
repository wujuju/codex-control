from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .app import WeComBridge
from .config import AppConfig, load_config
from .wecom_bot import WeComBotGateway, check_bot_connection


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="企业微信智能机器人长连接到 ChatGPT Plus 和本地 Codex"
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="显示调试日志")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="启动企业微信智能机器人长连接")
    doctor = sub.add_parser("doctor", help="检查配置和 API 凭证")
    doctor.add_argument(
        "--connect",
        action="store_true",
        help="实际连接企业微信（会短暂占用机器人唯一长连接）",
    )
    sub.add_parser("chatgpt-login", help="打开专用 Chrome 并保存 ChatGPT Plus 登录状态")
    return parser


def doctor(config: AppConfig, connect: bool) -> int:
    failed = False
    print(f"[OK] Python {sys.version.split()[0]}")
    codex = shutil.which(config.codex_command)
    if codex:
        print(f"[OK] Codex: {codex}")
    else:
        failed = True
        print(f"[FAIL] 找不到 Codex 命令：{config.codex_command}")

    name = config.wecom_bot_secret_env
    if os.environ.get(name, "").strip():
        print(f"[OK] 环境变量：{name}")
    else:
        failed = True
        print(f"[FAIL] 尚未设置环境变量：{name}")

    if "REPLACE" in config.wecom_bot_id.upper():
        failed = True
        print("[FAIL] wecom_bot_id 仍是示例占位值")
    else:
        print(f"[OK] 企业微信智能机器人 BotID：{config.wecom_bot_id}")
    print(f"[OK] 企业微信长连接：{config.wecom_websocket_url}")
    if config.group_only:
        print("[OK] 仅响应群聊 @机器人 消息")
    if config.allowed_group_chat_ids:
        print(
            "[OK] 已锁定群 chatid：" + "、".join(sorted(config.allowed_group_chat_ids))
        )
    else:
        print("[WARN] 尚未锁定群 chatid；将响应机器人所在的所有群")
    if config.default_group_chat_name:
        print(f"[OK] 默认 ChatGPT 群对话标题：{config.default_group_chat_name}")
    if config.group_chat_names:
        print(f"[OK] 已配置群名称映射：{len(config.group_chat_names)} 个")
    if config.user_chat_names:
        print(f"[OK] 已配置个人名称映射：{len(config.user_chat_names)} 个")

    try:
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory(
            prefix="wecom-chatgpt-browser-check-"
        ) as profile:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    profile,
                    channel=config.chatgpt_browser_channel,
                    headless=True,
                )
                context.close()
        print(f"[OK] ChatGPT Plus 浏览器：{config.chatgpt_browser_channel}")
    except Exception as exc:
        failed = True
        print(f"[FAIL] 无法启动 ChatGPT 自动化浏览器：{exc}")
    if config.chatgpt_profile_dir.is_dir() and any(
        config.chatgpt_profile_dir.iterdir()
    ):
        print(f"[OK] ChatGPT Plus 独立登录资料：{config.chatgpt_profile_dir}")
    else:
        failed = True
        print("[FAIL] 尚未保存 ChatGPT Plus 登录状态，请先运行 chatgpt-login")

    for name, path in config.projects.items():
        if path.is_dir() and (path / ".git").exists():
            print(f"[OK] 项目 {name}: {path}")
        else:
            failed = True
            print(f"[FAIL] 项目 {name} 不是可用 Git 仓库：{path}")

    if connect and not failed:
        try:
            asyncio.run(
                check_bot_connection(
                    config.wecom_websocket_url,
                    config.wecom_bot_id,
                    config.wecom_bot_secret,
                    config.wecom_timeout_seconds,
                )
            )
            print("[OK] 企业微信智能机器人订阅成功")
        except Exception as exc:
            failed = True
            print(f"[FAIL] 企业微信智能机器人长连接：{exc}")

    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
    )
    try:
        config = load_config(Path(args.config))
        if args.command == "doctor":
            return doctor(config, args.connect)
        if args.command == "chatgpt-login":
            from .chatgpt_runner import login_chatgpt

            login_chatgpt(
                config.chatgpt_profile_dir,
                config.chatgpt_browser_channel,
            )
            return 0

        bridge = WeComBridge(config)
        gateway = WeComBotGateway(
            bridge,
            url=config.wecom_websocket_url,
            bot_id=config.wecom_bot_id,
            secret=config.wecom_bot_secret,
            timeout_seconds=config.wecom_timeout_seconds,
        )
        print("正在连接企业微信智能机器人；请在群 ChatGpt 中 @机器人 发送消息")
        gateway.run_forever()
        return 0
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
