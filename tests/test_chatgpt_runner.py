import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from wechat_codex.chatgpt_runner import (
    ACCOUNT_SELECTOR,
    ChatGPTRunner,
    LOGIN_SELECTOR,
    PlaywrightChatSession,
    _is_chat_url,
    _read_account_state,
)


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

    def __call__(self, profile_dir, browser_channel, headless, proxy_server):
        self.session_count += 1
        return FakeSession(self.calls, self.session_count)


class FakeProfile:
    def __init__(self, text: str, label: str = "", visible: bool = True) -> None:
        self.text = text
        self.label = label
        self.visible = visible

    def is_visible(self, timeout):
        return self.visible

    def inner_text(self, timeout):
        return self.text

    def get_attribute(self, name, timeout):
        return self.label if name == "aria-label" else None


class FakeProfileList:
    def __init__(self, profiles: list[FakeProfile]) -> None:
        self.profiles = profiles

    def count(self):
        return len(self.profiles)

    def nth(self, index):
        return self.profiles[index]


class FakeAccountPage:
    def __init__(
        self,
        profiles: list[FakeProfile],
        login_buttons: list[FakeProfile] | None = None,
    ) -> None:
        self.profiles = FakeProfileList(profiles)
        self.login_buttons = FakeProfileList(login_buttons or [])

    def locator(self, selector):
        if selector == ACCOUNT_SELECTOR:
            return self.profiles
        if selector == LOGIN_SELECTOR:
            return self.login_buttons
        raise AssertionError(f"unexpected selector: {selector}")


class FakeNavigationPage:
    def __init__(self, results: list[Exception | None]) -> None:
        self.results = results
        self.goto_count = 0
        self.waits: list[int] = []
        self.url = "about:blank"

    def goto(self, url, wait_until, timeout):
        result = self.results[self.goto_count]
        self.goto_count += 1
        if result is not None:
            raise result
        self.url = url

    def wait_for_timeout(self, timeout):
        self.waits.append(timeout)


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
        proxy_server=None,
        timeout_seconds=30,
        runtime_dir=runtime_dir,
        session_factory=factory,
    )


class ChatGPTRunnerTests(unittest.TestCase):
    def test_anonymous_composer_is_not_treated_as_logged_in(self) -> None:
        state = _read_account_state(
            FakeAccountPage(
                [FakeProfile("", label="打开个人资料菜单")],
                login_buttons=[FakeProfile("登录")],
            )
        )

        self.assertFalse(state.logged_in)
        self.assertFalse(state.plus)

    def test_plus_account_requires_visible_profile_and_plus_label(self) -> None:
        page = FakeAccountPage(
            [
                FakeProfile(""),
                FakeProfile("he yang", label="he yang Plus，打开个人资料菜单"),
            ]
        )

        state = _read_account_state(page)

        self.assertTrue(state.logged_in)
        self.assertTrue(state.plus)

    def test_logged_in_free_account_is_not_treated_as_plus(self) -> None:
        state = _read_account_state(FakeAccountPage([FakeProfile("测试用户")]))

        self.assertTrue(state.logged_in)
        self.assertFalse(state.plus)

    def test_navigation_retries_transient_proxy_disconnect(self) -> None:
        page = FakeNavigationPage(
            [
                RuntimeError("net::ERR_CONNECTION_CLOSED"),
                RuntimeError("net::ERR_CONNECTION_RESET"),
                None,
            ]
        )

        PlaywrightChatSession._navigate(page, "https://chatgpt.com/", 30)

        self.assertEqual(page.goto_count, 3)
        self.assertEqual(page.waits, [1000, 2000])

    def test_conversation_url_survives_runner_restart(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            factory = FakeSessionFactory()

            first = make_runner(runtime_dir, factory)
            self.assertTrue(first.begin_chat("friend:测试", "第一问")[0])
            wait_until_idle(first)
            self.assertEqual([event.text for event in first.drain_events()], ["回复1"])
            first.close()

            second = make_runner(runtime_dir, factory)
            self.assertTrue(second.begin_chat("friend:测试", "第二问")[0])
            wait_until_idle(second)
            second.close()

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
            runner.close()

    def test_multiple_chats_reuse_one_browser_session(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            factory = FakeSessionFactory()
            runner = make_runner(runtime_dir, factory)

            self.assertTrue(runner.begin_chat("friend:甲", "第一问")[0])
            wait_until_idle(runner)
            runner.drain_events()
            self.assertTrue(runner.begin_chat("friend:乙", "第二问")[0])
            wait_until_idle(runner)
            runner.close()

            self.assertEqual(factory.session_count, 1)
            self.assertEqual(factory.calls, [(None, "第一问"), (None, "第二问")])

    def test_only_chatgpt_conversation_urls_are_persisted(self) -> None:
        self.assertTrue(_is_chat_url("https://chatgpt.com/c/abc"))
        self.assertTrue(_is_chat_url("https://chatgpt.com/g/custom/c/abc"))
        self.assertFalse(_is_chat_url("https://example.com/c/abc"))


if __name__ == "__main__":
    unittest.main()
