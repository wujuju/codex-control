import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from wechat_codex.chatgpt_runner import ChatGPTRunner, _is_chat_url


class FakeSession:
    def __init__(self, calls: list[tuple[str | None, str]], reply_number: int) -> None:
        self.calls = calls
        self.reply_number = reply_number

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def ask(self, conversation_url, prompt, timeout_seconds, stopped):
        self.calls.append((conversation_url, prompt))
        return f"回复{self.reply_number}", (
            conversation_url or "https://chatgpt.com/c/web-conversation-1"
        )


class FakeSessionFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []
        self.session_count = 0

    def __call__(self, profile_dir, browser_channel, headless):
        self.session_count += 1
        return FakeSession(self.calls, self.session_count)


def wait_until_idle(runner: ChatGPTRunner) -> None:
    deadline = time.monotonic() + 2
    while runner.active and time.monotonic() < deadline:
        time.sleep(0.01)
    if runner.active:
        raise AssertionError("ChatGPT runner did not become idle")


def make_runner(runtime_dir: Path, factory: FakeSessionFactory) -> ChatGPTRunner:
    return ChatGPTRunner(
        browser_channel="msedge",
        headless=True,
        timeout_seconds=30,
        runtime_dir=runtime_dir,
        session_factory=factory,
    )


class ChatGPTRunnerTests(unittest.TestCase):
    def test_conversation_url_survives_runner_restart(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            factory = FakeSessionFactory()

            first = make_runner(runtime_dir, factory)
            self.assertTrue(first.begin_chat("friend:测试", "第一问")[0])
            wait_until_idle(first)
            self.assertEqual([event.text for event in first.drain_events()], ["回复1"])

            second = make_runner(runtime_dir, factory)
            self.assertTrue(second.begin_chat("friend:测试", "第二问")[0])
            wait_until_idle(second)

            url = "https://chatgpt.com/c/web-conversation-1"
            self.assertEqual(factory.calls, [(None, "第一问"), (url, "第二问")])
            saved = json.loads(
                (runtime_dir / "chatgpt_web_conversations.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(saved, {"friend:测试": url})

    def test_new_chat_removes_mapping_but_keeps_old_web_chat(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            factory = FakeSessionFactory()
            runner = make_runner(runtime_dir, factory)
            runner.begin_chat("friend:测试", "你好")
            wait_until_idle(runner)
            runner.drain_events()

            self.assertTrue(runner.begin_reset("friend:测试")[0])

            saved = json.loads(
                (runtime_dir / "chatgpt_web_conversations.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(saved, {})
            self.assertIn("旧对话仍保留", runner.drain_events()[0].text)

    def test_only_chatgpt_conversation_urls_are_persisted(self) -> None:
        self.assertTrue(_is_chat_url("https://chatgpt.com/c/abc"))
        self.assertTrue(_is_chat_url("https://chatgpt.com/g/custom/c/abc"))
        self.assertFalse(_is_chat_url("https://example.com/c/abc"))


if __name__ == "__main__":
    unittest.main()
