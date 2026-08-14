import unittest

from wechat_codex.router import RouteKind, help_text, route_message


class RouterTests(unittest.TestCase):
    def test_plain_message_is_chat(self) -> None:
        route = route_message("你好")
        self.assertEqual(route.kind, RouteKind.CHAT)
        self.assertEqual(route.prompt, "你好")

    def test_work_with_project(self) -> None:
        route = route_message("/干活 control：运行测试")
        self.assertEqual(route.kind, RouteKind.WORK)
        self.assertEqual(route.project, "control")
        self.assertEqual(route.prompt, "运行测试")

        chinese = route_message("/干活 演示项目：运行测试")
        self.assertEqual(chinese.project, "演示项目")

    def test_work_uses_default_when_project_missing(self) -> None:
        route = route_message("/干活：修复错误")
        self.assertEqual(route.kind, RouteKind.WORK)
        self.assertIsNone(route.project)

    def test_control_commands(self) -> None:
        self.assertEqual(route_message("/发送帮助").kind, RouteKind.HELP)
        self.assertEqual(route_message("/状态").kind, RouteKind.STATUS)
        self.assertEqual(route_message("/停止").kind, RouteKind.STOP)
        self.assertEqual(route_message("/继续：补充测试").kind, RouteKind.CONTINUE)
        self.assertEqual(route_message("/新对话").kind, RouteKind.NEW_CHAT)

    def test_conversation_and_maintenance_commands(self) -> None:
        self.assertEqual(route_message("/当前对话").kind, RouteKind.CURRENT_CHAT)
        self.assertEqual(route_message("/对话列表").kind, RouteKind.CONVERSATION_LIST)
        self.assertEqual(route_message("/归档对话").kind, RouteKind.ARCHIVE_CHAT)
        self.assertEqual(route_message("/导出对话").kind, RouteKind.EXPORT_CHAT)
        self.assertEqual(route_message("/总结对话").kind, RouteKind.SUMMARIZE_CHAT)
        self.assertEqual(route_message("/重试").kind, RouteKind.RETRY)
        self.assertEqual(route_message("/重发").kind, RouteKind.RESEND)
        self.assertEqual(route_message("/最近任务").kind, RouteKind.RECENT_TASKS)
        self.assertEqual(route_message("/项目列表").kind, RouteKind.PROJECT_LIST)
        self.assertEqual(route_message("/缓存状态").kind, RouteKind.CACHE_STATUS)
        self.assertEqual(route_message("/健康检查").kind, RouteKind.DOCTOR)
        self.assertEqual(route_message("/重连微信").kind, RouteKind.RECONNECT_WECHAT)
        self.assertEqual(route_message("/重启浏览器").kind, RouteKind.RESTART_BROWSER)

        rename = route_message("/重命名对话：发布方案")
        self.assertEqual(rename.kind, RouteKind.RENAME_CHAT)
        self.assertEqual(rename.prompt, "发布方案")

        default_clear = route_message("/清理缓存")
        self.assertEqual(default_clear.kind, RouteKind.CACHE_CLEAR)
        self.assertEqual(default_clear.days, 7)

        clear = route_message("/清理缓存：30天")
        self.assertEqual(clear.kind, RouteKind.CACHE_CLEAR)
        self.assertEqual(clear.days, 30)

        switch_chat = route_message("/切换对话：2")
        self.assertEqual(switch_chat.kind, RouteKind.SWITCH_CHAT)
        self.assertEqual(switch_chat.number, 2)

        switch_project = route_message("/切换项目：control")
        self.assertEqual(switch_project.kind, RouteKind.SWITCH_PROJECT)
        self.assertEqual(switch_project.project, "control")

        logs = route_message("/查看日志：50")
        self.assertEqual(logs.kind, RouteKind.VIEW_LOGS)
        self.assertEqual(logs.number, 50)

    def test_command_words_inside_a_sentence_remain_chat(self) -> None:
        route = route_message("请帮我归档对话并总结内容")
        self.assertEqual(route.kind, RouteKind.CHAT)

    def test_commands_without_slash_remain_chat(self) -> None:
        for text in ("状态", "停止", "重试", "归档对话", "干活：运行测试"):
            with self.subTest(text=text):
                self.assertEqual(route_message(text).kind, RouteKind.CHAT)

    def test_prefixed_commands_must_match_the_whole_message(self) -> None:
        self.assertEqual(route_message("/状态 请快点").kind, RouteKind.CHAT)
        self.assertEqual(route_message("请执行 /重试").kind, RouteKind.CHAT)

    def test_help_lists_second_stage_commands(self) -> None:
        text = help_text(["control", "demo"], "control")
        for command in (
            "/帮助",
            "/对话列表",
            "/切换对话：2",
            "/导出对话",
            "/总结对话",
            "/最近任务",
            "/项目列表",
            "/切换项目：项目名",
            "/查看日志：50",
            "/重连微信",
            "/重启浏览器",
        ):
            with self.subTest(command=command):
                self.assertIn(command, text)


if __name__ == "__main__":
    unittest.main()
