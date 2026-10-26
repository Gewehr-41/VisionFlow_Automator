# -*- coding: utf-8 -*-
"""执行前体检对话框：全中文、详情可折叠、三个决策。

背景（所有者 2026-10-26 反馈）：原实现用 `QMessageBox.setDetailedText()`，
它的"展开/收起详细选项"按钮文字来自 Qt 自带翻译文件（本项目没加载），于是显示
英文 "Show Details..."。现在改为自绘对话框，并加一条静态守卫防止退回。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import preflight  # noqa: E402
from preflight_dialog import PreflightDialog  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _issue(level=preflight.FATAL, code="template_unset", fixable=False):
    return preflight.Issue(level=level, code=code, step="步骤 1「测试」",
                           message="示例问题", fixable=fixable)


class PreflightDialogTests(unittest.TestCase):
    def setUp(self):
        self.issues = [
            _issue(preflight.FATAL, "template_unset"),
            _issue(preflight.WARNING, "next_template_placeholder", fixable=True),
        ]
        self.dialog = PreflightDialog(self.issues)

    def tearDown(self):
        self.dialog.close()

    def test_is_a_real_dialog(self):
        self.assertTrue(self.dialog.isWindow(), "必须是独立对话框")
        self.assertEqual(self.dialog.windowTitle(), "执行前体检")

    def test_all_buttons_are_chinese(self):
        self.assertEqual(self.dialog.details_button.text(), "显示详情")
        self.assertEqual(self.dialog.continue_button.text(), "仍然开始")
        self.assertEqual(self.dialog.cancel_button.text(), "取消")
        self.assertIsNotNone(self.dialog.fix_button)
        self.assertEqual(self.dialog.fix_button.text(), "自动修复可安全项并开始")
        for text in (self.dialog.details_button.text(), self.dialog.continue_button.text(),
                     self.dialog.cancel_button.text(), self.dialog.fix_button.text()):
            self.assertFalse(text.isascii(), f"按钮文字应为中文：{text!r}")

    def test_summary_mentions_counts(self):
        text = self.dialog.summary_label.text()
        self.assertIn("2 个配置问题", text)
        self.assertIn("1 个会导致执行失败", text)

    def test_details_are_collapsed_by_default(self):
        self.assertFalse(self.dialog.details_view.isVisible())

    def test_details_toggle_uses_chinese_labels(self):
        self.dialog.show()
        self.dialog.details_button.click()
        self.assertTrue(self.dialog.details_view.isVisible())
        self.assertEqual(self.dialog.details_button.text(), "收起详情")
        self.dialog.details_button.click()
        self.assertFalse(self.dialog.details_view.isVisible())
        self.assertEqual(self.dialog.details_button.text(), "显示详情")

    def test_details_view_lists_every_issue(self):
        content = self.dialog.details_view.toPlainText()
        for issue in self.issues:
            self.assertIn(issue.describe(), content)

    def test_fix_button_hidden_when_nothing_fixable(self):
        dialog = PreflightDialog([_issue()])
        try:
            self.assertIsNone(dialog.fix_button)
        finally:
            dialog.close()

    def test_default_button_is_cancel(self):
        self.assertTrue(self.dialog.cancel_button.isDefault(),
                        "误按回车不应该带着已知问题开跑")

    def test_decisions(self):
        self.assertEqual(self.dialog.decision(), PreflightDialog.CANCEL)

        self.dialog.cancel_button.click()
        self.assertEqual(self.dialog.decision(), PreflightDialog.CANCEL)

        self.dialog.continue_button.click()
        self.assertEqual(self.dialog.decision(), PreflightDialog.CONTINUE)

        fix_dialog = PreflightDialog(self.issues)
        try:
            fix_dialog.fix_button.click()
            self.assertEqual(fix_dialog.decision(), PreflightDialog.FIX)
        finally:
            fix_dialog.close()


class NoEnglishDetailsGuardTests(unittest.TestCase):
    """静态守卫：不允许再**调用** `setDetailedText`（那个折叠按钮是英文的）。

    只匹配带括号的调用，文档里提到这个方法名不算犯规。
    """

    def test_engine_does_not_call_set_detailed_text(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if ".setDetailedText(" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], "详情折叠必须是自绘的中文对话框，不能调用 setDetailedText")


if __name__ == "__main__":
    unittest.main()
