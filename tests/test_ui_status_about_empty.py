# -*- coding: utf-8 -*-
"""L2/L3/V4/V5/O8：状态栏、状态胶囊、运行进度与行标记、空状态、关于/使用说明（2026-10-27）。

  · L2：目标窗口选择从控制行移到**底部状态栏**（右侧带「刷新窗口」）；
  · L3：底部状态胶囊（待机/运行中/已暂停/异常/已完成/已停止，颜色区分）+ 当前预设 + 最后保存时间；
  · V4：运行中显示进度条、当前步骤行加粗蓝底、结果行绿字/红底、完成后收起进度；
  · V5：列表为空、编辑器未选步骤时显示引导文案；
  · O8：状态栏右侧「使用说明」「关于」（版本/程序目录/数据目录）。
"""

import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(task_id, description=None):
    return {"id": task_id, "type": "normal", "description": description or task_id,
            "template": "1", "enabled": True}


class UiStateBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(cls._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(cls._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        cls._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = []
        cls.window = g.PySide6ScriptWindow()
        cls.window.show()
        _APP.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        g.TASKS[:] = cls._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = cls._orig
        cls._tmp.cleanup()

    def tearDown(self):
        g.TASKS[:] = []
        self.window.execution_states.clear()
        # 耗时状态是窗口级字段，用例之间要复位，否则后续用例会读到上一次结果
        self.window._run_started_at = None
        self.window._run_final_elapsed = None
        timer = getattr(self.window, "_run_elapsed_timer", None)
        if timer is not None:
            timer.stop()
        self.window._update_run_elapsed()
        self.window.refresh_task_list()
        _APP.processEvents()


class StatusBarTests(UiStateBase):
    def test_target_window_controls_back_in_control_row(self):
        """L2 之后按所有者要求把目标窗口放回控制行（不在状态栏里）。"""
        self.assertIsNotNone(getattr(self.window, "status_bar_widget", None))
        self.assertTrue(self.window.status_bar_widget.isVisible())
        self.assertFalse(self.window.status_bar_widget.isAncestorOf(self.window.window_combo),
                         "目标窗口应回到控制行")
        self.assertFalse(self.window.status_bar_widget.isAncestorOf(self.window.refresh_window_button))
        combo_y = self.window.window_combo.mapTo(self.window, self.window.window_combo.rect().topLeft()).y()
        self.assertLess(combo_y, self.window.status_bar_widget.y(), "目标窗口应在状态栏上方")
        self.assertEqual(self.window.refresh_window_button.text(), "刷新窗口")

    def test_status_capsule_is_compact_and_progress_fills_the_row(self):
        """状态胶囊按最长文案定宽（不拉长），剩余空间给进度条。"""
        from PySide6.QtWidgets import QSizePolicy
        self.assertEqual(self.window.status_label.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Fixed)
        self.assertGreater(self.window.status_label.width(), 120)
        self.assertLessEqual(self.window.status_label.width(), 360, "胶囊不应再吃满整行")
        self.assertEqual(self.window.progress_bar.sizePolicy().horizontalPolicy(),
                         QSizePolicy.Policy.Expanding)
        self.assertGreater(self.window.progress_bar.width(), self.window.status_label.width(),
                           "进度条应占满剩余空间")
        self.assertGreaterEqual(self.window.progress_bar.height(), 18, "进度条不能细成一根线")

    def test_status_capsule_defaults_to_idle(self):
        self.assertEqual(self.window.status_label.property("statusKind"), "idle")
        self.assertIn("待机", self.window.status_label.text())

    def test_status_capsule_kinds(self):
        self.window._set_status("running", "第 3/12 步")
        self.assertEqual(self.window.status_label.property("statusKind"), "running")
        self.assertIn("运行中", self.window.status_label.text())
        self.assertIn("3/12", self.window.status_label.text())
        self.window._set_status("paused")
        self.assertEqual(self.window.status_label.property("statusKind"), "paused")
        self.window._set_status("error", "脚本异常")
        self.assertEqual(self.window.status_label.property("statusKind"), "error")
        self.window._set_status("done")
        self.assertEqual(self.window.status_label.property("statusKind"), "done")

    def test_preset_info_and_saved_time_labels(self):
        self.assertEqual(self.window.preset_info_label.text(), "预设: custom")
        g.TASKS[:] = [_task("A")]
        self.window.refresh_task_list()
        self.window.save_current_tasks()
        _APP.processEvents()
        self.assertIn("本次耗时", self.window.last_save_label.text())


class RunElapsedTests(UiStateBase):
    def test_initial_label_is_idle(self):
        self.assertIn("本次耗时", self.window.last_save_label.text())
        self.assertIn("--", self.window.last_save_label.text())

    def test_elapsed_is_frozen_after_run(self):
        with mock.patch.object(g.time, "monotonic", side_effect=[100.0, 100.0, 102.5]):
            self.window._begin_run_timing()
            self.window._end_run_timing()
        self.assertIn("2.5 秒", self.window.last_save_label.text())

    def test_elapsed_formats_minutes(self):
        self.assertEqual(self.window._format_run_elapsed(83.4), "1 分 23 秒")
        self.assertEqual(self.window._format_run_elapsed(9.25), "9.2 秒")


class RunProgressTests(UiStateBase):
    def setUp(self):
        super().setUp()
        g.TASKS[:] = [_task("A"), _task("B")]
        self.window.refresh_task_list()
        _APP.processEvents()

    def test_progress_and_row_highlight_while_running(self):
        self.window._begin_run_progress()
        self.window.on_execution_started(g.TASKS[0])
        _APP.processEvents()
        self.assertTrue(self.window.progress_bar.isVisible())
        self.assertEqual(self.window.progress_bar.maximum(), 2)
        self.assertEqual(self.window.progress_bar.value(), 1)
        item = self.window._find_task_item(0)
        self.assertTrue(item.font(0).bold(), "当前执行步骤应加粗")
        self.assertEqual(item.background(0).color().name(), "#dbeafe", "当前执行步骤应有蓝底")

    def test_result_marks_success_and_failure(self):
        self.window.execution_states["A"] = "success"
        self.window.execution_states["B"] = "failed"
        self.window._apply_execution_marks()
        item_a = self.window._find_task_item(0)
        item_b = self.window._find_task_item(1)
        self.assertEqual(item_a.foreground(0).color().name(), "#15803d", "成功步骤绿字")
        self.assertEqual(item_b.background(0).color().name(), "#fee2e2", "失败步骤红底")
        self.assertEqual(item_b.foreground(0).color().name(), "#b91c1c")

    def test_result_updates_progress_and_error_status(self):
        self.window._begin_run_progress()
        self.window.on_execution_started(g.TASKS[0])
        self.window.on_execution_result(g.TASKS[0], "failed")
        _APP.processEvents()
        self.assertEqual(self.window.progress_bar.value(), 1)
        self.assertEqual(self.window.status_label.property("statusKind"), "error")
        self.assertIn("failed" if False else "失败", self.window.status_label.text())

    def test_completion_keeps_final_progress(self):
        self.window._begin_run_progress()
        self.window.on_execution_started(g.TASKS[0])
        self.window.on_execution_result(g.TASKS[0], "success")
        with mock.patch.object(g.QMessageBox, "information", return_value=None):
            self.window.on_execution_completed("finished")
        self.assertTrue(self.window.progress_bar.isVisible(), "进度条常驻状态栏，完成后保留最终值")
        self.assertEqual(self.window.progress_bar.value(), 1)
        self.assertEqual(self.window.status_label.property("statusKind"), "done")

    def test_idle_progress_shows_zero_of_enabled_steps(self):
        self.window.refresh_task_list()
        _APP.processEvents()
        self.assertTrue(self.window.progress_bar.isVisible())
        self.assertEqual(self.window.progress_bar.value(), 0)
        self.assertGreaterEqual(self.window.progress_bar.maximum(), 1)


class EmptyStateTests(UiStateBase):
    def test_empty_list_shows_guidance(self):
        self.assertEqual(self.window.task_stack.currentIndex(), 1, "空列表应显示引导页")
        self.assertIn("右键", self.window.task_empty_page.text())

    def test_list_page_returns_when_tasks_exist(self):
        g.TASKS[:] = [_task("A")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self.assertEqual(self.window.task_stack.currentIndex(), 0)

    def test_empty_list_right_click_opens_create_menu(self):
        """空预设时右键引导页要能呼出「新增步骤/新增组」菜单（不真的弹模态框）。"""
        from PySide6.QtCore import Qt

        self.assertEqual(self.window.task_empty_page.contextMenuPolicy(),
                         Qt.ContextMenuPolicy.CustomContextMenu,
                         "空状态页必须接受右键菜单事件")
        self.assertTrue(hasattr(self.window, "_show_empty_list_menu"))
        menu = self.window._build_task_list_menu(None)
        labels = [action.text() for action in menu.actions() if action.text()]
        self.assertIn("新增步骤", labels)
        self.assertIn("新增组", labels)
        # 触发「新增步骤」应能在空预设里创建第一个步骤并切回列表页
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("normal", True))):
            next(action for action in menu.actions() if action.text() == "新增步骤").trigger()
            _APP.processEvents()
        self.assertEqual(len(g.TASKS), 1, "空列表右键「新增步骤」应能创建第一个步骤")
        self.assertEqual(self.window.task_stack.currentIndex(), 0, "创建后应切回列表页")

    def test_empty_list_menu_handler_executes_menu(self):
        """右键事件处理确实会弹出菜单（用假菜单替换 exec，避免模态阻塞）。"""
        from PySide6.QtCore import QPoint

        class FakeMenu:
            def __init__(self):
                self.exec_positions = []

            def exec(self, position):
                self.exec_positions.append(position)

        fake = FakeMenu()
        original = self.window._build_task_list_menu
        self.window._build_task_list_menu = lambda context_item=None: fake
        try:
            self.window._show_empty_list_menu(QPoint(3, 4))
        finally:
            self.window._build_task_list_menu = original
        self.assertEqual(len(fake.exec_positions), 1, "空列表右键应执行菜单")

    def test_editor_empty_page_toggles(self):
        g.TASKS[:] = [_task("A")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self.window.clear_editor()
        self.assertTrue(self.window.editor_empty_hint.isVisible(), "未选步骤时显示引导提示")
        item = self.window._find_task_item(0)
        self.window.load_selected_task(item, None)
        self.assertFalse(self.window.editor_empty_hint.isVisible(), "选中步骤后收起引导提示")


class AboutHelpTests(UiStateBase):
    def test_buttons_exist(self):
        self.assertEqual(self.window.about_button.text(), "关于")
        self.assertEqual(self.window.help_button.text(), "使用说明")
        self.assertTrue(self.window.status_bar_widget.isAncestorOf(self.window.about_button))
        self.assertTrue(self.window.status_bar_widget.isAncestorOf(self.window.help_button))

    def test_about_shows_version_and_dirs(self):
        with mock.patch.object(g.QMessageBox, "about") as about:
            self.window.about_button.click()
        self.assertTrue(about.called)
        text = str(about.call_args)
        self.assertIn(config_version(), text)
        self.assertIn("数据目录", text)

    def test_help_opens_readme(self):
        with mock.patch.object(g.QDesktopServices, "openUrl") as open_url:
            self.window.help_button.click()
        self.assertTrue(open_url.called, "应打开本地 README")


def config_version():
    import config
    return config.APP_VERSION


class UiStateGuardTests(unittest.TestCase):
    def test_implementation_markers(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        for marker in ("def _set_status", "def _apply_execution_marks", "status_bar_widget",
                       "task_empty_page", "editor_empty_hint", "def show_about", "def open_help"):
            self.assertIn(marker, source, f"缺少 {marker}")

    def test_execution_control_uses_status_helper(self):
        source = open("execution_control.py", encoding="utf-8").read()
        self.assertIn('self._set_status("running"', source)
        self.assertIn("self._begin_run_progress()", source)
        self.assertNotIn('self.status_label.setText("状态:', source)


if __name__ == "__main__":
    unittest.main()
