import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest

from wechat_codex.chatgpt_runner import CHAT_INSTRUCTIONS, ChatGPTRunner


class FakeConversations:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[str] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=f"conv-{len(self.created)}")

    def delete(self, conversation_id: str):
        self.deleted.append(conversation_id)
        return SimpleNamespace(id=conversation_id, deleted=True)


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=f"回复{len(self.calls)}",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class FakeClient:
    def __init__(self) -> None:
        self.conversations = FakeConversations()
        self.responses = FakeResponses()
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def wait_until_idle(runner: ChatGPTRunner) -> None:
    deadline = time.monotonic() + 2
    while runner.active and time.monotonic() < deadline:
        time.sleep(0.01)
    if runner.active:
        raise AssertionError("ChatGPT runner did not become idle")


def make_runner(runtime_dir: Path, client: FakeClient) -> ChatGPTRunner:
    return ChatGPTRunner(
        model="gpt-5.6-terra",
        reasoning_effort="low",
        max_output_tokens=1200,
        timeout_seconds=30,
        runtime_dir=runtime_dir,
        client_factory=lambda: client,
    )


class ChatGPTRunnerTests(unittest.TestCase):
    def test_conversation_id_survives_runner_restart(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            client = FakeClient()

            first = make_runner(runtime_dir, client)
            self.assertTrue(first.begin_chat("friend:测试", "第一问")[0])
            wait_until_idle(first)
            self.assertEqual([event.text for event in first.drain_events()], ["回复1"])

            second = make_runner(runtime_dir, client)
            self.assertTrue(second.begin_chat("friend:测试", "第二问")[0])
            wait_until_idle(second)

            self.assertEqual(len(client.conversations.created), 1)
            self.assertEqual(client.responses.calls[0]["conversation"], "conv-1")
            self.assertEqual(client.responses.calls[1]["conversation"], "conv-1")
            self.assertEqual(client.responses.calls[1]["model"], "gpt-5.6-terra")
            self.assertEqual(client.responses.calls[1]["reasoning"], {"effort": "low"})
            self.assertEqual(client.responses.calls[1]["instructions"], CHAT_INSTRUCTIONS)
            self.assertEqual(client.responses.calls[1]["max_output_tokens"], 1200)

            saved = json.loads(
                (runtime_dir / "chat_conversations.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved, {"friend:测试": "conv-1"})

    def test_new_chat_deletes_remote_conversation_and_local_mapping(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            client = FakeClient()
            runner = make_runner(runtime_dir, client)
            runner.begin_chat("friend:测试", "你好")
            wait_until_idle(runner)
            runner.drain_events()

            self.assertTrue(runner.begin_reset("friend:测试")[0])
            wait_until_idle(runner)

            self.assertEqual(client.conversations.deleted, ["conv-1"])
            saved = json.loads(
                (runtime_dir / "chat_conversations.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved, {})
            self.assertIn("新的上下文", runner.drain_events()[0].text)


if __name__ == "__main__":
    unittest.main()
