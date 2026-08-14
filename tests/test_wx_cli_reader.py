import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_codex.wx_cli_reader import (
    WxCliReadError,
    WxCliReader,
    WxCliUnavailable,
)


def completed(payload: object, *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr=stderr,
    )


class WxCliReaderTests(unittest.TestCase):
    def test_missing_executable_has_actionable_error(self) -> None:
        reader = WxCliReader(contact="無惧", chat_type="friend")

        with patch("wechat_codex.wx_cli_reader.shutil.which", return_value=None):
            with self.assertRaisesRegex(WxCliUnavailable, "wx_cli_path"):
                reader.connect()

    def test_configured_command_is_resolved_from_path(self) -> None:
        reader = WxCliReader(
            contact="無惧",
            chat_type="friend",
            executable="wechat-cli",
        )

        with patch(
            "wechat_codex.wx_cli_reader.shutil.which",
            return_value=r"C:\Tools\wechat-cli.exe",
        ):
            reader.connect()

        self.assertEqual(reader.executable, r"C:\Tools\wechat-cli.exe")

    def test_baseline_is_discarded_and_next_poll_is_incremental(self) -> None:
        baseline = {
            "count": 2,
            "messages": [
                {
                    "chat": "無惧",
                    "username": "wxid_target",
                    "chat_type": "private",
                    "timestamp": 100,
                    "sender": "",
                    "content": "旧消息",
                    "type": "text",
                },
                {
                    "chat": "其他人",
                    "username": "wxid_other",
                    "chat_type": "private",
                    "timestamp": 100,
                    "sender": "",
                    "content": "其他旧消息",
                    "type": "text",
                },
            ],
            "new_state": {},
        }
        incremental = {
            "count": 2,
            "messages": [
                {
                    "chat": "無惧",
                    "username": "wxid_target",
                    "chat_type": "private",
                    "timestamp": 100,
                    "sender": "",
                    "content": "旧消息",
                    "type": "text",
                },
                {
                    "chat": "無惧",
                    "username": "wxid_target",
                    "chat_type": "private",
                    "timestamp": 101,
                    "sender": "",
                    "content": "新消息",
                    "type": "text",
                }
            ],
            "new_state": {},
        }
        reader = WxCliReader(
            contact="無惧",
            chat_type="friend",
            executable="wx",
        )

        with patch(
            "wechat_codex.wx_cli_reader.shutil.which",
            return_value=r"C:\Tools\wx.exe",
        ), patch(
            "wechat_codex.wx_cli_reader.subprocess.run",
            side_effect=[completed(baseline), completed(incremental)],
        ) as run:
            reader.connect()
            baseline_count = reader.baseline()
            messages = reader.poll()

        self.assertEqual(baseline_count, 1)
        self.assertEqual([message.content for message in messages], ["新消息"])
        self.assertEqual(messages[0].sender, "")
        self.assertFalse(messages[0].is_self)
        self.assertEqual(
            run.call_args_list[0].args[0],
            [r"C:\Tools\wx.exe", "new-messages", "--json"],
        )
        self.assertFalse(run.call_args_list[0].kwargs["shell"])

    def test_filters_exact_username_chat_type_and_non_text(self) -> None:
        payload = {
            "data": {
                "messages": [
                    {
                        "chat": "重名",
                        "username": "wxid_right",
                        "chat_type": "private",
                        "timestamp": 1,
                        "sender": "",
                        "content": "正确",
                        "type": "quote",
                    },
                    {
                        "chat": "重名",
                        "username": "wxid_wrong",
                        "chat_type": "private",
                        "timestamp": 2,
                        "sender": "",
                        "content": "错误账号",
                        "type": "text",
                    },
                    {
                        "chat": "重名",
                        "username": "wxid_right",
                        "chat_type": "group",
                        "timestamp": 3,
                        "sender": "张三",
                        "content": "错误类型",
                        "type": "text",
                    },
                    {
                        "chat": "重名",
                        "username": "wxid_right",
                        "chat_type": "private",
                        "timestamp": 4,
                        "sender": "",
                        "content": "[图片]",
                        "type": "image",
                    },
                ]
            }
        }
        reader = WxCliReader(
            contact="重名",
            chat_type="friend",
            username="wxid_right",
        )
        reader._executable = "wx"

        with patch(
            "wechat_codex.wx_cli_reader.subprocess.run",
            return_value=completed(payload),
        ):
            messages = reader.poll()

        self.assertEqual([message.content for message in messages], ["正确"])

    def test_private_labeled_sender_is_treated_as_self(self) -> None:
        reader = WxCliReader(contact="無惧", chat_type="friend")
        reader._executable = "wx"
        payload = {
            "messages": [
                {
                    "chat": "無惧",
                    "username": "wxid_target",
                    "chat_type": "private",
                    "timestamp": 1,
                    "sender": "he yang",
                    "content": "我发出的消息",
                    "type": "text",
                }
            ]
        }

        with patch(
            "wechat_codex.wx_cli_reader.subprocess.run",
            return_value=completed(payload),
        ):
            messages = reader.poll()

        self.assertTrue(messages[0].is_self)

    def test_invalid_json_and_failed_command_are_reported(self) -> None:
        reader = WxCliReader(contact="無惧", chat_type="friend")
        reader._executable = "wx"

        with patch(
            "wechat_codex.wx_cli_reader.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
        ):
            with self.assertRaisesRegex(WxCliReadError, "有效 JSON"):
                reader.poll()

        with patch(
            "wechat_codex.wx_cli_reader.subprocess.run",
            return_value=SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="daemon unavailable",
            ),
        ):
            with self.assertRaisesRegex(WxCliReadError, "daemon unavailable"):
                reader.poll()


if __name__ == "__main__":
    unittest.main()
