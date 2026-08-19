import queue
import unittest

from wechat_codex.chatgpt_runner import ChatEvent
from wechat_codex.shared_chatgpt import SharedChatGPTPool


class FakeRunner:
    def __init__(self) -> None:
        self.events: queue.Queue[ChatEvent] = queue.Queue()
        self.active = False
        self.active_session_key = None
        self.browser_running = False
        self.stops = 0
        self.closed = 0

    def drain_events(self):
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result

    def stop(self):
        self.stops += 1
        return "stopped"

    def stop_if_session_owned(self, session_keys):
        if self.active_session_key not in session_keys:
            return "其他微信账号的 ChatGPT 请求正在执行，不能从当前账号停止"
        self.stops += 1
        return "stopped"

    def close(self, _timeout=10):
        self.closed += 1


class SharedChatGPTPoolTests(unittest.TestCase):
    def test_completion_events_are_delivered_only_to_the_owning_account(self) -> None:
        runner = FakeRunner()
        pool = SharedChatGPTPool(runner)  # type: ignore[arg-type]
        first = pool.for_account("bot-a")
        second = pool.for_account("bot-b")
        runner.events.put(ChatEvent("A", "ilink:bot-a:user-1"))
        runner.events.put(ChatEvent("B", "ilink:bot-b:user-2"))

        self.assertEqual([event.text for event in second.drain_events()], ["B"])
        self.assertEqual([event.text for event in first.drain_events()], ["A"])

    def test_account_cannot_stop_another_accounts_request(self) -> None:
        runner = FakeRunner()
        pool = SharedChatGPTPool(runner)  # type: ignore[arg-type]
        first = pool.for_account("bot-a")
        runner.active = True
        runner.active_session_key = "ilink:bot-b:user-2"

        result = first.stop()

        self.assertIn("其他微信账号", result)
        self.assertEqual(runner.stops, 0)

    def test_pool_not_proxy_owns_runner_shutdown(self) -> None:
        runner = FakeRunner()
        pool = SharedChatGPTPool(runner)  # type: ignore[arg-type]
        account = pool.for_account("bot-a")

        account.close()
        pool.close()

        self.assertEqual(runner.closed, 1)


if __name__ == "__main__":
    unittest.main()
