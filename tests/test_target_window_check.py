# -*- coding: utf-8 -*-
"""H1：开始执行前校验目标窗口是否存在（2026-10-27）。

用户反馈的安全问题：目标窗口填错/已关闭时，`core/screen.get_window_rect()` 会退回
"当前前台窗口"，脚本可能对错误窗口点击/按键。现在在 `_start_worker` 里先校验：
目标窗口标题非空且 `getWindowsWithTitle()` 找不到 → 明确提示并阻止启动。
（空标题 = 明确使用当前前台窗口，不校验。）
"""

import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import execution_control  # noqa: E402
import gui_pyside6 as g  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class TargetWindowCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = [{"id": "win-fixture", "type": "delay", "description": "窗口校验夹具",
                       "enabled": True, "duration": 0}]
        self.window = g.PySide6ScriptWindow()
        self.window.show()
        _APP.processEvents()
        self.window._start_stop_hotkey = lambda: None
        self.window._stop_stop_hotkey = lambda: None
        # 这些用例只验证"窗口校验"这一步；预检一律直接失败，避免弹真实模态框
        self.window._preflight_before_run = lambda: False

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def test_missing_target_window_blocks_start_with_clear_message(self):
        worker_created = {"value": False}

        class _ExplodingWorker:
            def __init__(self, *args, **kwargs):
                worker_created["value"] = True
                raise AssertionError("目标窗口不存在时不应启动 worker")

        self.window.window_combo.clear()
        self.window.window_combo.setEditText("绝对不存在的窗口_ABCXYZ_123")
        with mock.patch.object(execution_control, "TaskWorker", _ExplodingWorker), \
                mock.patch.object(g.QMessageBox, "warning") as warning:
            self.window._start_worker(None)
        _APP.processEvents()
        self.assertFalse(worker_created["value"], "目标窗口不存在时必须阻止启动")
        self.assertIsNone(self.window.worker_thread, "不应创建线程")
        self.assertTrue(warning.called, "必须弹出明确提示")
        args = warning.call_args[0]
        self.assertIn("目标窗口不存在", args[1])
        self.assertIn("绝对不存在的窗口_ABCXYZ_123", args[2])
        self.assertIn("启动已取消", self.window.log_box.toPlainText())

    def test_start_script_button_also_checks_window(self):
        self.window.window_combo.clear()
        self.window.window_combo.setEditText("也不存在的窗口_QQQ")
        with mock.patch.object(g.QMessageBox, "warning") as warning:
            self.window.start_script()
        _APP.processEvents()
        self.assertTrue(warning.called)
        self.assertIsNone(self.window.worker_thread)

    def test_existing_window_passes_validation_and_goes_to_preflight(self):
        self.window.window_combo.clear()
        self.window.window_combo.setEditText("存在的窗口")
        preflight_called = {"value": False}

        def _fake_preflight():
            preflight_called["value"] = True
            return False          # 停在这里，不真的启动 worker

        with mock.patch.object(self.window, "_target_window_exists", lambda title: True), \
                mock.patch.object(self.window, "_preflight_before_run", _fake_preflight), \
                mock.patch.object(g.QMessageBox, "warning") as warning:
            self.window._start_worker(None)
        _APP.processEvents()
        self.assertTrue(preflight_called["value"], "窗口存在时应继续走预检")
        self.assertFalse(warning.called, "窗口存在时不应弹窗口不存在的提示")

    def test_empty_title_means_foreground_mode_and_skips_window_check(self):
        self.window.window_combo.clear()
        self.window.window_combo.setEditText("")

        def _should_not_be_called(title):
            raise AssertionError("空标题（前台窗口模式）不应做窗口存在性校验")

        with mock.patch.object(self.window, "_target_window_exists", _should_not_be_called), \
                mock.patch.object(self.window, "_preflight_before_run", lambda: False):
            self.window._start_worker(None)
        _APP.processEvents()
        self.assertIsNone(self.window.worker_thread)


class TargetWindowExistsHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = []
        self.window = g.PySide6ScriptWindow()
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def test_empty_title_is_treated_as_available(self):
        self.assertTrue(self.window._target_window_exists(""))

    def test_helper_uses_pygetwindow_result(self):
        with mock.patch.object(execution_control.gw, "getWindowsWithTitle", lambda title: []):
            self.assertFalse(self.window._target_window_exists("某窗口"))
        with mock.patch.object(execution_control.gw, "getWindowsWithTitle", lambda title: [object()]):
            self.assertTrue(self.window._target_window_exists("某窗口"))

    def test_helper_survives_pygetwindow_exception(self):
        def _boom(title):
            raise RuntimeError("window api failed")

        with mock.patch.object(execution_control.gw, "getWindowsWithTitle", _boom):
            self.assertFalse(self.window._target_window_exists("某窗口"), "查询异常时应视为不存在（安全侧）")


class TargetWindowCheckGuardTests(unittest.TestCase):
    def test_start_worker_validates_target_window(self):
        source = open("execution_control.py", encoding="utf-8").read()
        self.assertIn("def _target_window_exists(self, title):", source)
        self.assertIn("目标窗口不存在", source)
        self.assertIn("if target_title and not self._target_window_exists(target_title):", source)
        self.assertIn("启动已取消", source)


if __name__ == "__main__":
    unittest.main()
