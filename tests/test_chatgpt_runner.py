import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from wechat_codex.chatgpt_runner import (
    ACCOUNT_SELECTOR,
    BrowserSessionUnavailable,
    ChatGPTRunner,
    LOGIN_SELECTOR,
    PlaywrightChatSession,
    STOP_SELECTOR,
    _extract_reply_text,
    _filter_reply_text,
    _is_usable_generated_image,
    _is_chat_url,
    _persistent_context_options,
    _read_account_state,
)


class FakeSession:
    def __init__(
        self,
        calls: list[tuple[str | None, str, str | None]],
        image_calls: list[tuple[Path, ...]],
        image_expectations: list[bool | None],
        archive_calls: list[str],
        rename_calls: list[tuple[str, str]],
        export_calls: list[tuple[str, str]],
        reply_number: int,
        reply_images: tuple[Path, ...],
        ask_errors: list[Exception],
        enter_errors: list[Exception],
        exit_calls: list[int],
    ) -> None:
        self.calls = calls
        self.image_calls = image_calls
        self.image_expectations = image_expectations
        self.archive_calls = archive_calls
        self.rename_calls = rename_calls
        self.export_calls = export_calls
        self.reply_number = reply_number
        self.reply_images = reply_images
        self.ask_errors = ask_errors
        self.enter_errors = enter_errors
        self.exit_calls = exit_calls

    def __enter__(self):
        if self.enter_errors:
            raise self.enter_errors.pop(0)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.exit_calls.append(self.reply_number)
        return None

    def ask(
        self,
        conversation_url,
        prompt,
        conversation_title,
        timeout_seconds,
        stopped,
        image_paths,
        expect_image=None,
    ):
        self.calls.append((conversation_url, prompt, conversation_title))
        self.image_calls.append(image_paths)
        self.image_expectations.append(expect_image)
        if self.ask_errors:
            raise self.ask_errors.pop(0)
        return f"回复{self.reply_number}", (
            conversation_url or "https://chatgpt.com/c/web-conversation-1"
        ), self.reply_images

    def archive(self, conversation_url, timeout_seconds):
        self.archive_calls.append(conversation_url)

    def rename(self, conversation_url, title, timeout_seconds):
        self.rename_calls.append((conversation_url, title))

    def export_markdown(self, conversation_url, title, timeout_seconds):
        self.export_calls.append((conversation_url, title))
        return f"# {title}\n\n## 用户\n\n测试\n"


class FakeSessionFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, str | None]] = []
        self.image_calls: list[tuple[Path, ...]] = []
        self.image_expectations: list[bool | None] = []
        self.archive_calls: list[str] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.export_calls: list[tuple[str, str]] = []
        self.session_count = 0
        self.reply_images: tuple[Path, ...] = ()
        self.ask_errors: list[Exception] = []
        self.enter_errors: list[Exception] = []
        self.exit_calls: list[int] = []

    def __call__(self, profile_dir, browser_channel, headless, proxy_server):
        self.session_count += 1
        return FakeSession(
            self.calls,
            self.image_calls,
            self.image_expectations,
            self.archive_calls,
            self.rename_calls,
            self.export_calls,
            self.session_count,
            self.reply_images,
            self.ask_errors,
            self.enter_errors,
            self.exit_calls,
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
        self.timeouts: list[int] = []
        self.waits: list[int] = []
        self.url = "about:blank"

    def goto(self, url, wait_until, timeout):
        self.timeouts.append(timeout)
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


class ChangingReplyItem:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.index = 0

    def inner_text(self, timeout):
        text = self.texts[min(self.index, len(self.texts) - 1)]
        self.index += 1
        return text


class ChangingReplyList:
    def __init__(self, texts: list[str]) -> None:
        self.item = ChangingReplyItem(texts)

    def count(self):
        return 1

    def nth(self, index):
        return self.item


class EmptyReplyList:
    def count(self):
        return 0

    def nth(self, index):
        raise AssertionError(f"unexpected reply index: {index}")


class FakeVisibility:
    def __init__(self, visible: bool = False) -> None:
        self.visible = visible
        self.clicks = 0

    @property
    def first(self):
        return self

    def is_visible(self, timeout):
        return self.visible

    def click(self, timeout):
        self.clicks += 1


class FakeReplyPage:
    def __init__(self, stop_visible: bool = False) -> None:
        self.waits: list[int] = []
        self.stop_button = FakeVisibility(stop_visible)

    def locator(self, selector):
        if selector != STOP_SELECTOR:
            raise AssertionError(f"不应依赖其他按钮判断回复完成：{selector}")
        return self.stop_button

    def wait_for_timeout(self, timeout):
        self.waits.append(timeout)


def wait_until_idle(runner: ChatGPTRunner) -> None:
    deadline = time.monotonic() + 2
    while runner.active and time.monotonic() < deadline:
        time.sleep(0.01)
    if runner.active:
        raise AssertionError("ChatGPT runner did not become idle")


def wait_until_browser_released(runner: ChatGPTRunner) -> None:
    deadline = time.monotonic() + 2
    while runner.browser_running and time.monotonic() < deadline:
        time.sleep(0.01)
    if runner.browser_running:
        raise AssertionError("ChatGPT browser session was not released")


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
    def test_reply_text_filters_web_citations_links_and_sources(self) -> None:
        reply = (
            "第一段结论。\ue200cite\ue202turn0search0\ue202turn0search1\ue201\n\n"
            "详情可查看 [官方说明](https://example.com/guide)。\n"
            "turn0search2\n\n"
            "Sources:\n- 示例来源 https://example.com/source\n"
        )

        self.assertEqual(
            _filter_reply_text(reply),
            "第一段结论。\n\n详情可查看 官方说明。",
        )

    def test_reply_text_uses_dom_content_with_citations_removed(self) -> None:
        class Reply:
            def evaluate(self, _script):
                return "保留的正文\n\n来源：\nhttps://example.com"

            def inner_text(self, timeout):
                raise AssertionError(f"unexpected fallback: {timeout}")

        self.assertEqual(_extract_reply_text(Reply()), "保留的正文")

    def test_citation_icon_is_not_treated_as_generated_reply_image(self) -> None:
        citation_icon = {
            # Simulate a proxy URL that otherwise looks like an OpenAI image.
            "usable": True,
            "generated": True,
            "naturalWidth": 128,
            "naturalHeight": 128,
            "renderedWidth": 12,
            "renderedHeight": 13,
        }
        large_non_generated_image = {
            **citation_icon,
            "generated": False,
            "renderedWidth": 256,
            "renderedHeight": 256,
        }

        self.assertFalse(_is_usable_generated_image(citation_icon))
        self.assertFalse(_is_usable_generated_image(large_non_generated_image))

    def test_visible_openai_generated_image_is_accepted(self) -> None:
        generated_image = {
            "usable": True,
            "generated": True,
            "naturalWidth": 1024,
            "naturalHeight": 1024,
            "renderedWidth": 512,
            "renderedHeight": 512,
        }

        self.assertTrue(_is_usable_generated_image(generated_image))

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

    def test_socket_disconnect_requests_a_fresh_browser_session(self) -> None:
        page = FakeNavigationPage(
            [RuntimeError("net::ERR_SOCKET_NOT_CONNECTED")]
        )

        with self.assertRaises(BrowserSessionUnavailable):
            PlaywrightChatSession._navigate(page, "https://chatgpt.com/", 30)

        self.assertEqual(page.goto_count, 1)
        self.assertEqual(page.waits, [])

    def test_navigation_honors_stop_before_starting_a_blocking_goto(self) -> None:
        page = FakeNavigationPage([])

        with self.assertRaisesRegex(RuntimeError, "已停止"):
            PlaywrightChatSession._navigate(
                page,
                "https://chatgpt.com/",
                30,
                stopped=lambda: True,
            )

        self.assertEqual(page.goto_count, 0)

    def test_control_navigation_uses_runner_stop_checker(self) -> None:
        page = FakeNavigationPage([])
        session = PlaywrightChatSession(Path("unused"), "msedge", True, None)
        session._page = page
        session.set_stop_checker(lambda: True)

        with self.assertRaisesRegex(RuntimeError, "已停止"):
            session.archive("https://chatgpt.com/c/example", 30)

        self.assertEqual(page.goto_count, 0)

    def test_navigation_retries_share_one_absolute_deadline(self) -> None:
        clock = [0.0]

        class DeadlineNavigationPage(FakeNavigationPage):
            def goto(self, url, wait_until, timeout):
                super().goto(url, wait_until, timeout)

            def wait_for_timeout(self, timeout):
                super().wait_for_timeout(timeout)
                clock[0] += timeout / 1000

        page = DeadlineNavigationPage(
            [RuntimeError("net::ERR_CONNECTION_RESET")]
        )

        with (
            patch(
                "wechat_codex.chatgpt_runner.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            self.assertRaises(TimeoutError),
        ):
            PlaywrightChatSession._navigate(page, "https://chatgpt.com/", 1)

        self.assertEqual(page.goto_count, 1)
        self.assertLessEqual(sum(page.waits), 1000)
        self.assertTrue(all(timeout <= 1000 for timeout in page.timeouts))

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

    def test_reply_activity_extends_timeout_until_generation_settles(self) -> None:
        page = FakeReplyPage()
        replies = ChangingReplyList(["第一段", "第一段\n第二段", "第一段\n第二段"])
        # The final check happens after the original 10-second deadline. The
        # update at 10.5 seconds must extend the wait long enough to finish.
        clock = [0.0, 1.0, 10.5, 12.1]

        with patch(
            "wechat_codex.chatgpt_runner.time.monotonic",
            side_effect=clock,
        ):
            result = PlaywrightChatSession._wait_for_reply(
                page,
                replies,
                previous_count=0,
                timeout_seconds=10,
                stopped=lambda: False,
            )

        self.assertEqual(result, "第一段\n第二段")

    def test_reply_activity_cannot_extend_past_absolute_deadline(self) -> None:
        page = FakeReplyPage()
        replies = ChangingReplyList(["第一段", "第一段\n第二段"])
        clock = [0.0, 1.0, 3.0]

        with (
            patch(
                "wechat_codex.chatgpt_runner.time.monotonic",
                side_effect=clock,
            ),
            self.assertRaises(TimeoutError),
        ):
            PlaywrightChatSession._wait_for_reply(
                page,
                replies,
                previous_count=0,
                timeout_seconds=10,
                stopped=lambda: False,
                absolute_deadline=3.0,
            )

    def test_tool_activity_extends_timeout_before_final_reply_appears(self) -> None:
        page = FakeReplyPage()
        final_reply = FakeReplyItem("搜索完成后的最终回复")
        clock = [0.0, 1.0, 10.5, 12.0, 13.6]
        activity = [
            (("conversation-turn-11", "正在搜索", 2),),
            (("conversation-turn-11", "已搜索 3 个网页", 4),),
            (("conversation-turn-12", "搜索完成后的最终回复", 3),),
            (("conversation-turn-12", "搜索完成后的最终回复", 3),),
        ]

        with (
            patch(
                "wechat_codex.chatgpt_runner.time.monotonic",
                side_effect=clock,
            ),
            patch.object(
                PlaywrightChatSession,
                "_reply_activity_key",
                side_effect=activity,
            ),
            patch.object(
                PlaywrightChatSession,
                "_current_reply_assistant",
                side_effect=[None, None, final_reply, final_reply],
            ),
        ):
            result = PlaywrightChatSession._wait_for_reply(
                page,
                EmptyReplyList(),
                previous_count=0,
                timeout_seconds=10,
                stopped=lambda: False,
            )

        self.assertEqual(result, "搜索完成后的最终回复")

    def test_new_turn_reply_is_found_when_virtualized_node_count_is_unchanged(self) -> None:
        page = FakeReplyPage()
        final_reply = FakeReplyItem("虚拟列表中的新回复")
        clock = [0.0, 0.1, 2.0]
        activity_key = (("conversation-turn-21", "虚拟列表中的新回复", 3),)

        with (
            patch(
                "wechat_codex.chatgpt_runner.time.monotonic",
                side_effect=clock,
            ),
            patch.object(
                PlaywrightChatSession,
                "_reply_activity_key",
                return_value=activity_key,
            ),
            patch.object(
                PlaywrightChatSession,
                "_current_reply_assistant",
                return_value=final_reply,
            ),
        ):
            result = PlaywrightChatSession._wait_for_reply(
                page,
                EmptyReplyList(),
                previous_count=0,
                timeout_seconds=30,
                stopped=lambda: False,
            )

        self.assertEqual(result, "虚拟列表中的新回复")

    def test_image_prompt_can_finish_with_text_only_reply(self) -> None:
        page = FakeReplyPage()
        replies = FakeReplyList("当前无法生成图片，请补充细节")
        clock = [0.0, 0.1, 0.2, 1.0, 9.0]

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
                expect_image=True,
            )

        self.assertEqual(result, "当前无法生成图片，请补充细节")

    def test_stopping_wait_clicks_web_stop_button(self) -> None:
        page = FakeReplyPage(stop_visible=True)

        with self.assertRaisesRegex(RuntimeError, "已停止"):
            PlaywrightChatSession._wait_for_reply(
                page,
                FakeReplyList(""),
                previous_count=0,
                timeout_seconds=30,
                stopped=lambda: True,
            )

        self.assertEqual(page.stop_button.clicks, 1)

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

    def test_start_is_lazy_until_the_first_chat_job(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            runner = make_runner(Path(directory), factory)

            runner.start()

            self.assertFalse(runner.browser_running)
            self.assertEqual(factory.session_count, 0)

            self.assertTrue(runner.begin_chat("friend:测试", "首个问题")[0])
            wait_until_idle(runner)

            self.assertTrue(runner.browser_running)
            self.assertEqual(factory.session_count, 1)
            self.assertEqual(factory.calls, [(None, "首个问题", None)])
            runner.close()

    def test_close_before_first_job_does_not_open_browser(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            runner = make_runner(Path(directory), factory)

            runner.start()
            runner.close()

            self.assertFalse(runner.browser_running)
            self.assertEqual(factory.session_count, 0)

    def test_idle_browser_session_is_released_and_lazily_reopened(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            runner = make_runner(Path(directory), factory)
            runner._browser_max_idle_seconds = 0.02

            runner.start()
            self.assertFalse(runner.browser_running)
            self.assertEqual(factory.session_count, 0)

            self.assertTrue(runner.begin_chat("friend:测试", "释放前的问题")[0])
            wait_until_idle(runner)
            wait_until_browser_released(runner)

            self.assertEqual(factory.session_count, 1)
            self.assertEqual(factory.exit_calls, [1])
            self.assertTrue(runner.begin_chat("friend:测试", "空闲后的问题")[0])
            wait_until_idle(runner)
            runner.close()

            self.assertEqual(factory.session_count, 2)
            self.assertEqual(
                factory.calls,
                [
                    (None, "释放前的问题", None),
                    (
                        "https://chatgpt.com/c/web-conversation-1",
                        "空闲后的问题",
                        None,
                    ),
                ],
            )

    def test_stale_browser_is_rebuilt_before_processing_new_job(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            runner = make_runner(Path(directory), factory)
            moments = iter((0.0, 0.0, 0.0, 0.0, 3600.0, 3600.0, 3600.0))
            runner._monotonic = lambda: next(moments, 3600.0)

            self.assertTrue(runner.begin_chat("friend:测试", "第一问")[0])
            wait_until_idle(runner)
            runner.drain_events()
            self.assertTrue(runner.begin_chat("friend:测试", "隔夜后的问题")[0])
            wait_until_idle(runner)
            events = runner.drain_events()
            runner.close()

            self.assertEqual(factory.session_count, 2)
            self.assertEqual(
                factory.calls,
                [
                    (None, "第一问", None),
                    (
                        "https://chatgpt.com/c/web-conversation-1",
                        "隔夜后的问题",
                        None,
                    ),
                ],
            )
            self.assertEqual([event.text for event in events], ["回复2"])

    def test_browser_disconnect_rebuilds_and_replays_original_job(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            factory.ask_errors.append(
                BrowserSessionUnavailable("net::ERR_SOCKET_NOT_CONNECTED")
            )
            runner = make_runner(Path(directory), factory)

            self.assertTrue(runner.begin_chat("friend:测试", "不应让用户重试")[0])
            wait_until_idle(runner)
            events = runner.drain_events()
            runner.close()

            self.assertEqual(factory.session_count, 2)
            self.assertEqual(
                factory.calls,
                [
                    (None, "不应让用户重试", None),
                    (None, "不应让用户重试", None),
                ],
            )
            self.assertEqual([event.text for event in events], ["回复2"])

    def test_browser_startup_recovers_from_transient_network_failure(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            factory.enter_errors.append(
                BrowserSessionUnavailable("net::ERR_SOCKET_NOT_CONNECTED")
            )
            runner = make_runner(Path(directory), factory)
            runner._sleep = lambda _seconds: None

            runner.start()
            self.assertFalse(runner.browser_running)
            self.assertEqual(factory.session_count, 0)

            self.assertTrue(runner.begin_chat("friend:测试", "触发浏览器启动")[0])
            wait_until_idle(runner)
            running = runner.browser_running
            events = runner.drain_events()
            runner.close()

            self.assertTrue(running)
            self.assertEqual(factory.session_count, 2)
            self.assertEqual([event.text for event in events], ["回复2"])

    def test_browser_recovery_has_a_bounded_failure_result(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            factory.ask_errors.extend(
                BrowserSessionUnavailable("net::ERR_SOCKET_NOT_CONNECTED")
                for _ in range(4)
            )
            runner = make_runner(Path(directory), factory)

            self.assertTrue(runner.begin_chat("friend:测试", "测试恢复上限")[0])
            wait_until_idle(runner)
            events = runner.drain_events()
            runner.close()

            self.assertEqual(factory.session_count, 4)
            self.assertEqual(len(events), 1)
            self.assertIn("自动重建多次后仍无法恢复", events[0].text)

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

    def test_first_control_job_opens_browser_on_demand(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            url = "https://chatgpt.com/c/existing-conversation"
            (runtime_dir / "chatgpt_web_conversations.json").write_text(
                json.dumps({"friend:测试": url}),
                encoding="utf-8",
            )
            factory = FakeSessionFactory()
            runner = make_runner(runtime_dir, factory)

            runner.start()
            self.assertFalse(runner.browser_running)
            self.assertEqual(factory.session_count, 0)

            self.assertTrue(runner.begin_archive("friend:测试")[0])
            wait_until_idle(runner)

            self.assertTrue(runner.browser_running)
            self.assertEqual(factory.session_count, 1)
            self.assertEqual(factory.archive_calls, [url])
            runner.close()

    def test_archive_runs_in_browser_and_removes_mapping(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            factory = FakeSessionFactory()
            runner = make_runner(runtime_dir, factory)
            runner.begin_chat("friend:测试", "第一问")
            wait_until_idle(runner)
            runner.drain_events()

            self.assertTrue(runner.begin_archive("friend:测试")[0])
            wait_until_idle(runner)
            events = runner.drain_events()
            runner.close()

            url = "https://chatgpt.com/c/web-conversation-1"
            self.assertEqual(factory.archive_calls, [url])
            self.assertIn("已归档", events[0].text)
            saved = json.loads(
                (runtime_dir / "chatgpt_web_conversations.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(saved, {})

    def test_rename_persists_title_for_future_chat(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            factory = FakeSessionFactory()
            runner = make_runner(runtime_dir, factory)
            runner.begin_chat("friend:测试", "第一问", conversation_title="默认标题")
            wait_until_idle(runner)
            runner.drain_events()

            self.assertTrue(runner.begin_rename("friend:测试", "发布方案")[0])
            wait_until_idle(runner)
            self.assertIn("发布方案", runner.drain_events()[0].text)
            self.assertIn(
                "标题：发布方案",
                runner.conversation_info("friend:测试", "默认标题"),
            )
            self.assertTrue(
                runner.begin_chat(
                    "friend:测试",
                    "第二问",
                    conversation_title="默认标题",
                )[0]
            )
            wait_until_idle(runner)
            runner.close()

            url = "https://chatgpt.com/c/web-conversation-1"
            self.assertEqual(factory.rename_calls, [(url, "发布方案")])
            self.assertEqual(factory.calls[-1], (url, "第二问", "发布方案"))
            saved = json.loads(
                (runtime_dir / "chatgpt_conversation_titles.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(saved, {"friend:测试": "发布方案"})

    def test_retry_reuses_conversation_and_records_last_reply(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            runner = make_runner(Path(directory), factory)
            runner.begin_chat("friend:测试", "生成图片")
            wait_until_idle(runner)
            runner.drain_events()

            self.assertTrue(runner.begin_retry("friend:测试")[0])
            wait_until_idle(runner)
            events = runner.drain_events()
            last_reply = runner.last_reply("friend:测试")
            runner.close()

            self.assertIn("重新处理我上一条消息", factory.calls[-1][1])
            self.assertEqual(
                factory.calls[-1][0],
                "https://chatgpt.com/c/web-conversation-1",
            )
            self.assertEqual(last_reply, events[0])
            self.assertEqual(factory.image_expectations, [True, True])

    def test_text_retry_is_not_misclassified_by_retry_prompt_wording(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            runner = make_runner(Path(directory), factory)
            runner.begin_chat("friend:测试", "分析最近的市场波动")
            wait_until_idle(runner)
            runner.drain_events()

            self.assertTrue(runner.begin_retry("friend:测试")[0])
            wait_until_idle(runner)
            runner.close()

            self.assertEqual(factory.image_expectations, [False, False])

    def test_history_list_and_switch_are_isolated_by_session(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            history = {
                "friend:甲": [
                    {
                        "url": "https://chatgpt.com/c/newer",
                        "title": "较新对话",
                        "updated_at": 200,
                        "archived": False,
                    },
                    {
                        "url": "https://chatgpt.com/c/older",
                        "title": "较早对话",
                        "updated_at": 100,
                        "archived": True,
                    },
                ],
                "friend:乙": [{
                    "url": "https://chatgpt.com/c/private",
                    "title": "其他用户对话",
                    "updated_at": 300,
                    "archived": False,
                }],
            }
            (runtime_dir / "chatgpt_conversation_history.json").write_text(
                json.dumps(history, ensure_ascii=False),
                encoding="utf-8",
            )
            runner = make_runner(runtime_dir, FakeSessionFactory())

            listing = runner.conversation_list("friend:甲", "默认标题")
            ok, response = runner.switch_conversation(
                "friend:甲",
                2,
                "默认标题",
            )
            runner.close()

            self.assertIn("1. 较新对话", listing)
            self.assertIn("2. 较早对话", listing)
            self.assertNotIn("其他用户对话", listing)
            self.assertTrue(ok)
            self.assertIn("较早对话", response)
            saved = json.loads(
                (runtime_dir / "chatgpt_web_conversations.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(saved["friend:甲"], "https://chatgpt.com/c/older")

    def test_export_creates_markdown_file_event(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            factory = FakeSessionFactory()
            runner = make_runner(runtime_dir, factory)
            runner.begin_chat("friend:测试", "第一问", conversation_title="测试对话")
            wait_until_idle(runner)
            runner.drain_events()

            self.assertTrue(runner.begin_export("friend:测试", "测试对话")[0])
            wait_until_idle(runner)
            event = runner.drain_events()[0]
            runner.close()

            self.assertEqual(
                factory.export_calls,
                [("https://chatgpt.com/c/web-conversation-1", "测试对话")],
            )
            self.assertEqual(len(event.file_paths), 1)
            export = Path(event.file_paths[0])
            self.assertTrue(export.is_file())
            self.assertIn("# 测试对话", export.read_text(encoding="utf-8"))

    def test_summary_keeps_history_and_opens_new_conversation(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            factory = FakeSessionFactory()
            runner = make_runner(runtime_dir, factory)
            runner.begin_chat("friend:测试", "第一问", conversation_title="测试对话")
            wait_until_idle(runner)
            runner.drain_events()

            self.assertTrue(runner.begin_summary("friend:测试")[0])
            wait_until_idle(runner)
            event = runner.drain_events()[0]
            info = runner.conversation_info("friend:测试", "默认标题")
            listing = runner.conversation_list("friend:测试", "默认标题")
            runner.close()

            self.assertIn("对话总结", event.text)
            self.assertIn("已开启新对话", event.text)
            self.assertIn("当前是新的", info)
            self.assertIn("测试对话", listing)
            self.assertIn("总结我们当前整个对话", factory.calls[-1][1])

    def test_idle_browser_can_restart(self) -> None:
        with TemporaryDirectory() as directory:
            factory = FakeSessionFactory()
            runner = make_runner(Path(directory), factory)
            self.assertTrue(runner.begin_chat("friend:测试", "重启前")[0])
            wait_until_idle(runner)

            self.assertTrue(runner.browser_running)
            self.assertEqual(factory.session_count, 1)

            ok, response = runner.restart()

            self.assertTrue(ok)
            self.assertIn("已重启", response)
            self.assertFalse(runner.browser_running)
            self.assertEqual(factory.session_count, 1)

            self.assertTrue(runner.begin_chat("friend:测试", "重启后")[0])
            wait_until_idle(runner)
            runner.close()

            self.assertEqual(factory.session_count, 2)

    def test_restart_fails_when_old_browser_worker_does_not_close(self) -> None:
        class StuckWorker:
            def __init__(self) -> None:
                self.join_calls: list[float | None] = []

            def is_alive(self) -> bool:
                return True

            def join(self, timeout: float | None = None) -> None:
                self.join_calls.append(timeout)

        with TemporaryDirectory() as directory:
            runner = make_runner(Path(directory), FakeSessionFactory())
            worker = StuckWorker()
            runner._worker = worker  # type: ignore[assignment]

            ok, response = runner.restart()

            self.assertFalse(ok)
            self.assertIn("关闭超时", response)
            self.assertEqual(worker.join_calls, [10])

    def test_only_chatgpt_conversation_urls_are_persisted(self) -> None:
        self.assertTrue(_is_chat_url("https://chatgpt.com/c/abc"))
        self.assertTrue(_is_chat_url("https://chatgpt.com/g/custom/c/abc"))
        self.assertFalse(_is_chat_url("https://example.com/c/abc"))


if __name__ == "__main__":
    unittest.main()
