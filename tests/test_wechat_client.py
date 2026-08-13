import unittest

from wechat_codex.wechat_client import (
    WeChatClient,
    extract_voice_transcript,
    is_voice_message,
    normalize_message,
    normalize_voice_message,
    split_text,
    strip_required_group_mention,
)


class FakeMessage:
    attr = "friend"
    type = "text"
    sender = "张三"
    content = "你好"
    hash = "message-1"


class FakeVoiceMessage:
    attr = "friend"
    type = "voice"
    sender = "张三"
    content = '语音9"秒语音内容'
    id = "stable-voice-id"
    hash = "voice-1"

    def to_text(self) -> str:
        raise AssertionError("background mode must not call to_text")


class WeChatClientTests(unittest.TestCase):
    def test_normalize_text_message(self) -> None:
        message = normalize_message(FakeMessage())
        self.assertIsNotNone(message)
        self.assertEqual(message.content, "你好")
        self.assertEqual(message.key, "hash:message-1")

    def test_split_text(self) -> None:
        parts = split_text("a" * 250, 100)
        self.assertEqual([len(part) for part in parts], [100, 100, 50])

    def test_normalize_recognized_voice(self) -> None:
        raw = FakeVoiceMessage()
        self.assertTrue(is_voice_message(raw))
        message = normalize_voice_message(raw, " 干活：运行测试 ")
        self.assertIsNotNone(message)
        self.assertEqual(message.key, "id:stable-voice-id")
        self.assertEqual(message.content, "干活：运行测试")
        self.assertEqual(message.attr, "friend")

    def test_client_recognizes_voice(self) -> None:
        client = WeChatClient("张三", True, False, True, 3, "[助手] ", 1800)
        message = client._recognize_voice(FakeVoiceMessage(), "hash:voice-1")
        self.assertIsNotNone(message)
        self.assertEqual(message.content, "语音内容")

    def test_extracts_only_native_voice_transcript(self) -> None:
        self.assertEqual(extract_voice_transcript('语音12"秒 今天星期几？'), "今天星期几？")
        self.assertEqual(extract_voice_transcript('语音12"秒'), "")

    def test_message_key_prefers_stable_id(self) -> None:
        raw = FakeVoiceMessage()
        first = normalize_voice_message(raw, "第一次")
        raw.hash = "voice-changed-after-transcription"
        second = normalize_voice_message(raw, "第二次")
        self.assertEqual(first.key, second.key)

    def test_group_requires_bot_mention(self) -> None:
        self.assertIsNone(strip_required_group_mention("大家好", "ChatGpt机器人"))
        self.assertEqual(
            strip_required_group_mention(
                "@ChatGpt机器人\u2005 帮我解释 rebase", "ChatGpt机器人"
            ),
            "帮我解释 rebase",
        )

    def test_group_poll_ignores_unmentioned_message(self) -> None:
        raw = FakeMessage()
        raw.content = "大家好"
        raw.sender = "無惧"
        client = WeChatClient(
            "测试群", True, False, True, 3, "[助手] ", 1800, "group", "ChatGpt机器人"
        )
        client._active_chat_type = "group"
        client._all_messages = lambda: [raw]
        self.assertEqual(client.poll(), [])

    def test_group_poll_strips_mention_and_keeps_sender(self) -> None:
        raw = FakeMessage()
        raw.content = "@ChatGpt机器人\u2005 干活：运行测试"
        raw.sender = "無惧"
        client = WeChatClient(
            "测试群", True, False, True, 3, "[助手] ", 1800, "group", "ChatGpt机器人"
        )
        client._active_chat_type = "group"
        client._all_messages = lambda: [raw]
        messages = client.poll()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "干活：运行测试")
        self.assertEqual(messages[0].sender, "無惧")
        self.assertEqual(messages[0].chat_type, "group")

    def test_background_selection_prefers_selection_pattern(self) -> None:
        calls: list[str] = []

        class Selection:
            def Select(self) -> None:
                calls.append("select")

        class Control:
            def GetSelectionItemPattern(self):
                return Selection()

            def GetInvokePattern(self):
                raise AssertionError("invoke should not be requested")

        WeChatClient._select_without_focus(Control())
        self.assertEqual(calls, ["select"])


if __name__ == "__main__":
    unittest.main()
