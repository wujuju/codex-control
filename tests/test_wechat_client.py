import unittest

from wechat_codex.wechat_client import (
    WeChatClient,
    is_voice_message,
    normalize_message,
    normalize_voice_message,
    split_text,
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
    content = "[语音]"
    hash = "voice-1"

    def to_text(self) -> str:
        return "语音内容"


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
        self.assertEqual(message.key, "hash:voice-1")
        self.assertEqual(message.content, "干活：运行测试")
        self.assertEqual(message.attr, "friend")

    def test_client_recognizes_voice(self) -> None:
        client = WeChatClient("张三", True, False, True, 3, "[助手] ", 1800)
        message = client._recognize_voice(FakeVoiceMessage(), "hash:voice-1")
        self.assertIsNotNone(message)
        self.assertEqual(message.content, "语音内容")

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
