import unittest

from wechat_codex.wechat_client import normalize_message, split_text


class FakeMessage:
    attr = "friend"
    type = "text"
    sender = "张三"
    content = "你好"
    hash = "message-1"


class WeChatClientTests(unittest.TestCase):
    def test_normalize_text_message(self) -> None:
        message = normalize_message(FakeMessage())
        self.assertIsNotNone(message)
        self.assertEqual(message.content, "你好")
        self.assertEqual(message.key, "hash:message-1")

    def test_split_text(self) -> None:
        parts = split_text("a" * 250, 100)
        self.assertEqual([len(part) for part in parts], [100, 100, 50])


if __name__ == "__main__":
    unittest.main()

