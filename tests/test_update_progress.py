# -*- coding: utf-8 -*-
"""下载更新包时，底部进度条显示「下载进度」而不是「步骤进度」（2026-10-28）。"""

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


class UpdateDownloadProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(cls._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(cls._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        cls._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = [
            {"id": "A", "type": "delay", "description": "第一步", "duration": 0, "enabled": True},
            {"id": "B", "type": "delay", "description": "第二步", "duration": 0, "enabled": True},
        ]
        cls.window = g.PySide6ScriptWindow()
        cls.window.show()
        _APP.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        g.TASKS[:] = cls._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = cls._orig
        cls._tmp.cleanup()

    def setUp(self):
        self.window._refresh_idle_progress()
        self.window.progress_bar.setFormat("%v / %m 步")
        _APP.processEvents()

    def test_known_total_shows_percent(self):
        self.window._set_update_download_progress(50 * 1024 * 1024, 200 * 1024 * 1024)
        bar = self.window.progress_bar
        self.assertEqual(bar.maximum(), 100)
        self.assertEqual(bar.value(), 25)
        self.assertIn("更新包下载 25%", bar.format())
        self.assertIn("50.0/200.0 MB", bar.format())

    def test_unknown_total_shows_indeterminate_megabytes(self):
        self.window._set_update_download_progress(8 * 1024 * 1024, 0)
        bar = self.window.progress_bar
        self.assertEqual(bar.minimum(), 0)
        self.assertEqual(bar.maximum(), 0, "总大小未知时应显示不确定进度动画")
        self.assertIn("更新包下载中", bar.format())
        self.assertIn("8.0 MB", bar.format())

    def test_restore_after_download_goes_back_to_steps(self):
        self.window._set_update_download_progress(10, 10)
        self.window._restore_step_progress()
        bar = self.window.progress_bar
        self.assertEqual(bar.maximum(), 2, "恢复成已启用步骤数")
        self.assertEqual(bar.value(), 0)
        self.assertIn("%v / %m 步", bar.format())

    def test_download_failure_restores_step_progress(self):
        self.window._set_update_download_progress(10, 100)
        with mock.patch.object(g.QMessageBox, "warning") as warning:
            self.window._on_update_download_finished({"path": None, "error": "断网"})
        _APP.processEvents()
        self.assertTrue(warning.called)
        self.assertEqual(self.window.progress_bar.maximum(), 2)
        self.assertIn("%v / %m 步", self.window.progress_bar.format())


class UpdateProgressGuardTests(unittest.TestCase):
    def test_gui_markers(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("def _set_update_download_progress", source)
        self.assertIn("更新包下载 ", source)
        self.assertIn("更新包下载中", source)
        self.assertIn("def _restore_step_progress", source)
        self.assertIn("_set_update_download_progress(0, 0)", source)


if __name__ == "__main__":
    unittest.main()
