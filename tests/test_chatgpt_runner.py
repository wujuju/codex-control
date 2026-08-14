import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from wechat_codex.chatgpt_runner import (
    ACCOUNT_SELECTOR,
    ChatGPTRunner,
    LOGIN_SELECTOR,
    PlaywrightChatSession,
    STOP_SELECTOR,
    _is_chat_url,
    _persistent_context_options,
    _read_account_state,
)


class FakeSession:
    def __init__(
        self,
        calls: list[tuple[str | None, str, str | None]],
        image_calls: list[tuple[Path, ...]],
        reply_number: int,
        reply_images: tuple[Path, ...],
    ) -> None:
        self.calls = calls
        self.image_calls = image_calls
        self.reply_number = reply_number
        self.reply_images = reply_images

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def ask(
        self,
        conversation_url,
        prompt,
        conversation_title,
        timeout_seconds,
        stopped,
        image_paths,
    ):
        self.calls.append((conversation_url, prompt, conversation_title))
        self.image_calls.append(image_paths)
        return f"回复{self.reply_number}", (
            conversation_url or "https://chatgpt.com/c/web-conversation-1"
        ), self.reply_images


class FakeSessionFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, str | None]] = []
        self.image_calls: list[tuple[Path, ...]] = []
        self.session_count = 0
        self.reply_images: tuple[Path, ...] = ()

    def __call__(self, profile_dir, browser_channel, headless, proxy_server):
        self.session_count += 1
        return FakeSession(
            self.calls,
            self.image_calls,
            self.session_count,
            self.reply_images,
        )


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


class FakeReplyItem:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, timeout):
        return self.text


class FakeReplyList:
    def __init__(self, text: str) -> None:
        self.item = FakeReplyItem(text)

    def count(self):
        return 1

    def nth(self, index):
        return self.item


class FakeVisibility:
    @property
    def first(self):
        return self

    def is_visible(self, timeout):
        return False


class FakeReplyPage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def locator(self, selector):
        if selector != STOP_SELECTOR:
            raise AssertionError(f"不应依赖其他按钮判断回复完成：{selector}")
        return FakeVisibility()

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
    def test_reply_image_paths_are_exposed_on_chat_event(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reply_image = root / "chatgpt-reply.png"
            reply_image.write_bytes(b"reply-image")
            factory = FakeSessionFactory()
            factory.reply_images = (reply_image,)
            runner = make_runner(root, factory)

            self.assertTrue(runner.begin_chat("friend:测试", "生成图片")[0])
            wait_until_idle(runner)
            events = runner.drain_events()
            runner.close()

            self.assertEqual(events[0].text, "回复1")
            self.assertEqual(events[0].image_paths, (str(reply_image),))

    def test_image_path_is_forwarded_to_browser_session(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "wechat.png"
            image.write_bytes(b"image")
            factory = FakeSessionFactory()
            runner = make_runner(root, factory)

            self.assertTrue(
                runner.begin_chat("friend:测试", "分析图片", image_paths=(image,))[0]
            )
            wait_until_idle(runner)
            runner.close()

            self.assertEqual(factory.image_calls, [(image.resolve(),)])

    def test_hidden_browser_uses_native_hidden_window_on_windows(self) -> None:
        options, hide_native_window = _persistent_context_options(
            Path("profile"),
            "chrome",
            "socks5://127.0.0.1:7890",
            hide_window=True,
        )

        if os.name == "nt":
            self.assertFalse(options["headless"])
            self.assertTrue(hide_native_window)
            self.assertIn("--window-position=-32000,-32000", options["args"])
        else:
            self.assertTrue(options["headless"])
            self.assertFalse(hide_native_window)
        self.assertEqual(
            options["proxy"], {"server": "socks5://127.0.0.1:7890"}
        )

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

    def test_stable_reply_does_not_require_send_button(self) -> None:
        page = FakeReplyPage()
        replies = FakeReplyList("完整回复")
        clock = [0.0, 0.1, 0.2, 2.0, 2.1]

        with patch(
            "wechat_codex.chatgpt_runner.time.monotonic",
            side_effect=clock,
        ):
            result = PlaywrightChatSession._wait_for_reply(
                page,
                replies,
                previous_count=0,
                timeout_seconds=30,
                stopped=lambda: False,
            )

        self.assertEqual(result, "完整回复")

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
            self.assertEqual(
                factory.calls,
                [(None, "第一问", None), (url, "第二问", None)],
            )
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
            self.assertEqual(
                factory.calls,
                [(None, "第一问", None), (None, "第二问", None)],
            )

    def test_conversation_title_is_used_for_new_and_existing_web_chat(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            runner = make_runner(Path(directory), factory)

            self.assertTrue(
                runner.begin_chat(
                    "friend:無惧",
                    "第一问",
                    conversation_title="微信無惧",
                )[0]
            )
            wait_until_idle(runner)
            runner.drain_events()
            self.assertTrue(
                runner.begin_chat(
                    "friend:無惧",
                    "第二问",
                    conversation_title="微信無惧",
                )[0]
            )
            wait_until_idle(runner)
            runner.close()

            url = "https://chatgpt.com/c/web-conversation-1"
            self.assertEqual(
                factory.calls,
                [
                    (None, "第一问", "微信無惧"),
                    (url, "第二问", "微信無惧"),
                ],
            )

    def test_only_chatgpt_conversation_urls_are_persisted(self) -> None:
        self.assertTrue(_is_chat_url("https://chatgpt.com/c/abc"))
        self.assertTrue(_is_chat_url("https://chatgpt.com/g/custom/c/abc"))
        self.assertFalse(_is_chat_url("https://example.com/c/abc"))


if __name__ == "__main__":
    unittest.main()
