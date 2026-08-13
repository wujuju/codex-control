import unittest
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_background_send_prefers_invoke_without_setting_focus(self) -> None:
        from wxauto4.uia import uiautomation  # noqa: F401 - load before Win32 fakes

        class Value:
            Value = ""

            def SetValue(self, text: str) -> None:
                self.Value = text

        value = Value()
        focus_calls: list[str] = []

        class Edit:
            def GetValuePattern(self):
                return value

            def SetFocus(self) -> None:
                focus_calls.append("focus")

        class Invoke:
            def Invoke(self, waitTime: float = 0.5) -> bool:
                value.Value = ""
                return True

        class Button:
            def GetInvokePattern(self):
                return Invoke()

        client = WeChatClient("张三", True, False, True, 3, "[助手] ", 1800)
        client._root = SimpleNamespace(NativeWindowHandle=100)
        client._find_control = lambda _uia, **criteria: (
            Edit() if criteria.get("automation_id") == "chat_input_field" else Button()
        )
        client._send_window_to_background = lambda _gui: None

        fake_gui = SimpleNamespace(GetForegroundWindow=lambda: 200)
        with patch.dict("sys.modules", {"win32gui": fake_gui}):
            client._send_background("后台消息")

        self.assertEqual(focus_calls, [])
        self.assertTrue(client._invoke_send_supported)

    def test_background_send_fallback_restores_foreground(self) -> None:
        from wxauto4.uia import uiautomation  # noqa: F401 - load before Win32 fakes

        class Value:
            Value = ""

            def SetValue(self, text: str) -> None:
                self.Value = text

        value = Value()
        calls: list[object] = []
        foreground = [200]

        class Edit:
            def GetValuePattern(self):
                return value

            def SetFocus(self) -> None:
                calls.append("focus")
                foreground[0] = 100

        client = WeChatClient("张三", True, False, True, 3, "[助手] ", 1800)
        client._root = SimpleNamespace(NativeWindowHandle=100)
        client._find_control = lambda _uia, **_criteria: Edit()
        client._try_invoke_send = lambda _uia, _value: False
        client._send_window_to_background = lambda _gui: calls.append("background")

        def send_message(_hwnd: int, message: int, _key: int, _flags: int) -> None:
            calls.append(("send", message))
            value.Value = ""

        def set_foreground(hwnd: int) -> None:
            calls.append(("restore", hwnd))
            foreground[0] = hwnd

        fake_api = SimpleNamespace(SendMessage=send_message)
        fake_con = SimpleNamespace(WM_KEYDOWN=1, WM_KEYUP=2, VK_RETURN=13)
        fake_gui = SimpleNamespace(
            GetForegroundWindow=lambda: foreground[0],
            SetForegroundWindow=set_foreground,
        )
        with patch.dict(
            "sys.modules",
            {"win32api": fake_api, "win32con": fake_con, "win32gui": fake_gui},
        ):
            client._send_background("回退消息")

        self.assertEqual(calls[0], "focus")
        self.assertIn(("restore", 200), calls)
        self.assertEqual(calls[-1], "background")


if __name__ == "__main__":
    unittest.main()
