import unittest

from wechat_codex.router import RouteKind, help_text, route_message, suggest_command


class RouterTests(unittest.TestCase):
    def test_plain_message_is_chat(self) -> None:
        route = route_message("你好")
        self.assertEqual(route.kind, RouteKind.CHAT)
        self.assertEqual(route.prompt, "你好")

    def test_work_with_project(self) -> None:
        route = route_message("@干活 control：运行测试")
        self.assertEqual(route.kind, RouteKind.WORK)
        self.assertEqual(route.project, "control")
        self.assertEqual(route.prompt, "运行测试")

        chinese = route_message("@干活 演示项目：运行测试")
        self.assertEqual(chinese.project, "演示项目")

    def test_work_uses_default_when_project_missing(self) -> None:
        route = route_message("@干活：修复错误")
        self.assertEqual(route.kind, RouteKind.WORK)
        self.assertIsNone(route.project)

    def test_control_commands(self) -> None:
        self.assertEqual(route_message("@发送帮助").kind, RouteKind.HELP)
        self.assertEqual(route_message("@状态").kind, RouteKind.STATUS)
        self.assertEqual(route_message("@停止").kind, RouteKind.STOP)
        self.assertEqual(route_message("@继续：补充测试").kind, RouteKind.CONTINUE)
        self.assertEqual(route_message("@新对话").kind, RouteKind.NEW_CHAT)

    def test_conversation_and_maintenance_commands(self) -> None:
        self.assertEqual(route_message("@当前对话").kind, RouteKind.CURRENT_CHAT)
        self.assertEqual(route_message("@对话列表").kind, RouteKind.CONVERSATION_LIST)
        self.assertEqual(route_message("@归档对话").kind, RouteKind.ARCHIVE_CHAT)
        self.assertEqual(route_message("@导出对话").kind, RouteKind.EXPORT_CHAT)
        self.assertEqual(route_message("@总结对话").kind, RouteKind.SUMMARIZE_CHAT)
        self.assertEqual(route_message("@重试").kind, RouteKind.RETRY)
        self.assertEqual(route_message("@重发").kind, RouteKind.RESEND)
        self.assertEqual(route_message("@最近任务").kind, RouteKind.RECENT_TASKS)
        self.assertEqual(route_message("@项目列表").kind, RouteKind.PROJECT_LIST)
        self.assertEqual(route_message("@缓存状态").kind, RouteKind.CACHE_STATUS)
        self.assertEqual(route_message("@健康检查").kind, RouteKind.DOCTOR)
        self.assertEqual(route_message("@重连微信").kind, RouteKind.RECONNECT_WECHAT)
        self.assertEqual(route_message("@重启浏览器").kind, RouteKind.RESTART_BROWSER)

        rename = route_message("@重命名对话：发布方案")
        self.assertEqual(rename.kind, RouteKind.RENAME_CHAT)
        self.assertEqual(rename.prompt, "发布方案")

        default_clear = route_message("@清理缓存")
        self.assertEqual(default_clear.kind, RouteKind.CACHE_CLEAR)
        self.assertEqual(default_clear.days, 7)

        clear = route_message("@清理缓存：30天")
        self.assertEqual(clear.kind, RouteKind.CACHE_CLEAR)
        self.assertEqual(clear.days, 30)

        switch_chat = route_message("@切换对话：2")
        self.assertEqual(switch_chat.kind, RouteKind.SWITCH_CHAT)
        self.assertEqual(switch_chat.number, 2)

        switch_project = route_message("@切换项目：control")
        self.assertEqual(switch_project.kind, RouteKind.SWITCH_PROJECT)
        self.assertEqual(switch_project.project, "control")

        logs = route_message("@查看日志：50")
        self.assertEqual(logs.kind, RouteKind.VIEW_LOGS)
        self.assertEqual(logs.number, 50)

    def test_command_words_inside_a_sentence_remain_chat(self) -> None:
        route = route_message("请帮我归档对话并总结内容")
        self.assertEqual(route.kind, RouteKind.CHAT)

    def test_commands_without_at_sign_remain_chat(self) -> None:
        for text in ("状态", "停止", "重试", "归档对话", "干活：运行测试"):
            with self.subTest(text=text):
                self.assertEqual(route_message(text).kind, RouteKind.CHAT)

    def test_prefixed_commands_must_match_the_whole_message(self) -> None:
        self.assertEqual(
            route_message("@状态 请快点").kind,
            RouteKind.UNKNOWN_COMMAND,
        )
        self.assertEqual(route_message("请执行 @重试").kind, RouteKind.CHAT)

    def test_unknown_at_message_never_falls_back_to_chat(self) -> None:
        route = route_message("@归档")

        self.assertEqual(route.kind, RouteKind.UNKNOWN_COMMAND)
        self.assertEqual(route.prompt, "@归档")
        self.assertEqual(suggest_command("@归档"), "@归档对话")

    def test_similar_unknown_command_gets_a_suggestion(self) -> None:
        self.assertEqual(suggest_command("@归挡对话"), "@归档对话")
        self.assertEqual(suggest_command("@完全不存在的命令"), None)

    def test_help_lists_second_stage_commands(self) -> None:
        text = help_text(["control", "demo"], "control")
        for heading in ("【ChatGPT 对话命令】", "【Codex 开发命令】", "【通用与维护命令】"):
            self.assertIn(heading, text)
        for command in (
            "@帮助",
            "@对话列表",
            "@切换对话：2",
            "@导出对话",
            "@总结对话",
            "@最近任务",
            "@项目列表",
            "@切换项目：项目名",
            "@查看日志：50",
            "@重连微信",
            "@重启浏览器",
        ):
            with self.subTest(command=command):
                self.assertIn(command, text)

    def test_pinyin_initials_are_case_insensitive(self) -> None:
        commands = {
            "BZ": RouteKind.HELP,
            "FSBZ": RouteKind.HELP,
            "XDH": RouteKind.NEW_CHAT,
            "QLSXW": RouteKind.NEW_CHAT,
            "DQDH": RouteKind.CURRENT_CHAT,
            "DHLB": RouteKind.CONVERSATION_LIST,
            "GDDH": RouteKind.ARCHIVE_CHAT,
            "DCDH": RouteKind.EXPORT_CHAT,
            "ZJDH": RouteKind.SUMMARIZE_CHAT,
            "CS": RouteKind.RETRY,
            "CF": RouteKind.RESEND,
            "ZJRW": RouteKind.RECENT_TASKS,
            "XMLB": RouteKind.PROJECT_LIST,
            "ZT": RouteKind.STATUS,
            "TZ": RouteKind.STOP,
            "HCZT": RouteKind.CACHE_STATUS,
            "JKJC": RouteKind.DOCTOR,
            "CLWX": RouteKind.RECONNECT_WECHAT,
            "CQLLQ": RouteKind.RESTART_BROWSER,
        }
        for command, kind in commands.items():
            with self.subTest(command=command):
                self.assertEqual(route_message(f"@{command}").kind, kind)

        self.assertEqual(route_message("@cs").kind, RouteKind.RETRY)

        work = route_message("@GH control：运行测试")
        self.assertEqual(work.kind, RouteKind.WORK)
        self.assertEqual(work.project, "control")
        self.assertEqual(work.prompt, "运行测试")

        switch = route_message("@QHXM：demo")
        self.assertEqual(switch.kind, RouteKind.SWITCH_PROJECT)
        self.assertEqual(switch.project, "demo")

        self.assertEqual(route_message("@JX：补充测试").kind, RouteKind.CONTINUE)
        self.assertEqual(route_message("@QHDH：2").number, 2)
        self.assertEqual(route_message("@CMMDH：标题").prompt, "标题")
        self.assertEqual(route_message("@QLHC：30天").days, 30)
        self.assertEqual(route_message("@CKRZ：20").number, 20)

    def test_old_slash_prefix_is_plain_chat(self) -> None:
        route = route_message("/重试")
        self.assertEqual(route.kind, RouteKind.CHAT)
        self.assertEqual(route.prompt, "/重试")


if __name__ == "__main__":
    unittest.main()
