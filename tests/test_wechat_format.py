from __future__ import annotations

import unittest

from wechat_codex.wechat_format import format_wechat_text


class WeChatFormatTests(unittest.TestCase):
    def test_formats_mobile_headings_lists_quotes_and_spacing(self) -> None:
        source = """# 部署结果

## 执行情况
- **服务**已启动
  - 子项目
- [x] 健康检查
1) 第一项
> 一切正常
---"""

        self.assertEqual(
            format_wechat_text(source),
            """【部署结果】

▌执行情况

• 服务已启动
  • 子项目
☑ 健康检查
1. 第一项
│ 一切正常

────────────""",
        )

    def test_preserves_clickable_links_and_formats_inline_markdown(self) -> None:
        source = (
            "参考 [**官方文档**](https://example.com/docs?q=1)；"
            "查看 ![架构图](https://example.com/a.png)。\n"
            "支持 *斜体*、~~删除~~、`code_value` 和 https://example.com/a__b。"
        )

        self.assertEqual(
            format_wechat_text(source),
            "参考 官方文档：https://example.com/docs?q=1；"
            "查看 图片：架构图（https://example.com/a.png）。\n"
            "支持 斜体、删除、「code_value」 和 https://example.com/a__b。",
        )

    def test_fenced_code_uses_matching_marker_and_preserves_whitespace(self) -> None:
        source = "~~~~python\nprint('before')  \n```\n# still code\n~~~~\n## 后续"

        self.assertEqual(
            format_wechat_text(source),
            "【代码 · python】\nprint('before')  \n```\n# still code\n\n▌后续",
        )

    def test_shorter_backtick_fence_does_not_close_longer_fence(self) -> None:
        source = "````markdown\n```\ninside\n```\n````"

        self.assertEqual(
            format_wechat_text(source),
            "【代码 · markdown】\n```\ninside\n```",
        )

    def test_turns_wide_table_into_vertical_mobile_cards(self) -> None:
        source = """| 服务 | 状态 | 示例 |
| --- | :---: | ---: |
| API | 正常 | `a|b` |
| Web | 维护 | a\\|b |"""

        self.assertEqual(
            format_wechat_text(source),
            """1. API
  状态：正常
  示例：「a|b」

2. Web
  状态：维护
  示例：a|b""",
        )

    def test_does_not_convert_an_ordinary_pipe_line_to_a_table(self) -> None:
        self.assertEqual(format_wechat_text("选择 A | B 即可"), "选择 A | B 即可")

    def test_keeps_extra_table_cells_instead_of_dropping_content(self) -> None:
        source = "| 名称 | 状态 |\n| --- | --- |\n| API | 正常 | 额外说明 |"

        self.assertEqual(
            format_wechat_text(source),
            "• API\n  状态：正常\n  第 3 列：额外说明",
        )

    def test_private_use_characters_do_not_collide_with_inline_placeholders(self) -> None:
        source = "原文 \ue0000\ue001 和 [文档](https://example.com)"

        self.assertEqual(
            format_wechat_text(source),
            "原文 \ue0000\ue001 和 文档：https://example.com",
        )

    def test_normalizes_newlines_and_rejects_non_string_input(self) -> None:
        self.assertEqual(
            format_wechat_text("\r\n内容\r\n\r\n\r\n结尾\r\n"), "内容\n\n结尾"
        )
        self.assertEqual(format_wechat_text(None), "")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
