import unittest

from wechat_codex.router import RouteKind, route_message


class RouterTests(unittest.TestCase):
    def test_plain_message_is_chat(self) -> None:
        route = route_message("你好")
        self.assertEqual(route.kind, RouteKind.CHAT)
        self.assertEqual(route.prompt, "你好")

    def test_work_with_project(self) -> None:
        route = route_message("干活 control：运行测试")
        self.assertEqual(route.kind, RouteKind.WORK)
        self.assertEqual(route.project, "control")
        self.assertEqual(route.prompt, "运行测试")

    def test_work_uses_default_when_project_missing(self) -> None:
        route = route_message("干活：修复错误")
        self.assertEqual(route.kind, RouteKind.WORK)
        self.assertIsNone(route.project)

    def test_control_commands(self) -> None:
        self.assertEqual(route_message("状态").kind, RouteKind.STATUS)
        self.assertEqual(route_message("停止").kind, RouteKind.STOP)
        self.assertEqual(route_message("继续：补充测试").kind, RouteKind.CONTINUE)
        self.assertEqual(route_message("新对话").kind, RouteKind.NEW_CHAT)


if __name__ == "__main__":
    unittest.main()
