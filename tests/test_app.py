from pathlib import Path
from types import SimpleNamespace
import unittest

from wechat_codex.app import WeComBridge, WeComMessage, parse_wecom_message


class FakeChatRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def begin_chat(self, session_key, prompt, on_result, *, conversation_title=""):
        self.calls.append((session_key, prompt, conversation_title))
        return True, "started"

    def is_active(self, session_key):
        return False

    def status(self, session_key):
        return "chat idle"

    def stop(self, session_key):
        return "chat stopped"

    def stop_all(self):
        return None

    def reset(self, session_key):
        return True, "reset"


class FakeCodexRunner:
    active = False

    def __init__(self) -> None:
        self.work_calls = 0

    def begin_work(self, *args):
        self.work_calls += 1
        return True, "started"

    def begin_continue(self, prompt):
        return False, "nothing"

    def drain_events(self):
        return []

    def status(self):
        return "codex idle"

    def stop(self):
        return "codex stopped"


def make_config(*, allowed_groups=frozenset()):
    return SimpleNamespace(
        group_only=True,
        allowed_group_chat_ids=allowed_groups,
        authorized_senders=frozenset({"owner"}),
        default_group_chat_name="ChatGpt",
        group_chat_names={"chat-special": "研发群"},
        user_chat_names={"u2": "张三"},
        response_prefix="",
        max_reply_bytes=18000,
        projects={"control": Path(".")},
        default_project="control",
    )


def group_message(*, sender="u2", content="你好", chat_id="chat-1"):
    return WeComMessage(
        key="msg-1",
        request_id="req-1",
        sender=sender,
        chat_id=chat_id,
        chat_type="group",
        content=content,
        msg_type="text",
    )


class WeComBridgeTests(unittest.TestCase):
    def test_parses_group_callback_and_removes_bot_mention(self) -> None:
        message = parse_wecom_message(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "req-1"},
                "body": {
                    "msgid": "msg-1",
                    "chatid": "chat-1",
                    "chattype": "group",
                    "from": {"userid": "owner"},
                    "msgtype": "text",
                    "text": {"content": "@ChatGPT 你好"},
                },
            }
        )
        self.assertEqual(message.sender, "owner")
        self.assertEqual(message.content, "你好")
        self.assertEqual(message.chat_id, "chat-1")

    def test_group_chat_is_isolated_by_chat_and_user(self) -> None:
        chat = FakeChatRunner()
        bridge = WeComBridge(
            make_config(), chat_runner=chat, codex_runner=FakeCodexRunner()
        )
        try:
            bridge._handle(group_message(), lambda _request_id, _text: None)
            self.assertEqual(
                chat.calls,
                [("wecom:group:chat-1:u2", "你好", "ChatGpt")],
            )
        finally:
            bridge.close()

    def test_unauthorized_user_cannot_start_codex(self) -> None:
        replies: list[tuple[str, str]] = []
        codex = FakeCodexRunner()
        bridge = WeComBridge(
            make_config(), chat_runner=FakeChatRunner(), codex_runner=codex
        )
        try:
            bridge._handle(
                group_message(content="干活：运行测试"),
                lambda request_id, text: replies.append((request_id, text)),
            )
            self.assertEqual(codex.work_calls, 0)
            self.assertEqual(replies, [("req-1", "无权限执行 Codex 操作")])
        finally:
            bridge.close()

    def test_unlisted_group_is_ignored(self) -> None:
        bridge = WeComBridge(
            make_config(allowed_groups=frozenset({"chat-allowed"})),
            chat_runner=FakeChatRunner(),
            codex_runner=FakeCodexRunner(),
        )
        payload = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-1"},
            "body": {
                "msgid": "msg-1",
                "chatid": "chat-other",
                "chattype": "group",
                "from": {"userid": "owner"},
                "msgtype": "text",
                "text": {"content": "@Bot 你好"},
            },
        }
        try:
            self.assertFalse(bridge.submit(payload, lambda _id, _text: None))
        finally:
            bridge.close()

    def test_titles_use_group_mapping_and_person_mapping(self) -> None:
        config = make_config()
        group = group_message(chat_id="chat-special")
        single = WeComMessage(
            key="msg-2",
            request_id="req-2",
            sender="u2",
            chat_id="u2",
            chat_type="single",
            content="你好",
            msg_type="text",
        )
        self.assertEqual(group.conversation_title(config), "研发群")
        self.assertEqual(single.conversation_title(config), "张三")


if __name__ == "__main__":
    unittest.main()
