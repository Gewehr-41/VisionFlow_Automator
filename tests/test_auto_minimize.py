# -*- coding: utf-8 -*-
"""脚本执行时自动最小化主窗口，执行结束恢复。"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from execution_control import ExecutionControlMixin


class _Host(ExecutionControlMixin):
    def __init__(self, state=Qt.WindowState.WindowNoState, minimized=False):
        self.state = state
        self.minimized = minimized
        self.calls = []

    def windowState(self):
        return self.state

    def isMinimized(self):
        return self.minimized

    def showMinimized(self):
        self.calls.append("minimize")
        self.minimized = True

    def showNormal(self):
        self.calls.append("normal")
        self.minimized = False

    def showMaximized(self):
        self.calls.append("maximized")
        self.minimized = False

    def showFullScreen(self):
        self.calls.append("fullscreen")
        self.minimized = False

    def raise_(self):
        self.calls.append("raise")

    def activateWindow(self):
        self.calls.append("activate")


class AutoMinimizeTests(unittest.TestCase):
    def test_minimizes_on_start_and_restores_normal(self):
        host = _Host()
        host._minimize_for_run()
        self.assertEqual(host.calls[0], "minimize")
        self.assertTrue(host._auto_minimized)

        host._restore_after_run()
        self.assertIn("normal", host.calls)
        self.assertIn("activate", host.calls)
        self.assertFalse(host._auto_minimized)

    def test_restores_maximized_state(self):
        host = _Host(state=Qt.WindowState.WindowMaximized)
        host._minimize_for_run()
        host._restore_after_run()
        self.assertIn("maximized", host.calls)

    def test_does_not_touch_window_if_already_minimized(self):
        host = _Host(minimized=True)
        host._minimize_for_run()
        self.assertFalse(host._auto_minimized)
        host._restore_after_run()
        self.assertEqual(host.calls, [], "本来就最小化的窗口不应被额外恢复/激活")

    def test_restore_is_noop_without_auto_minimize(self):
        host = _Host()
        host._restore_after_run()
        self.assertEqual(host.calls, [])

    def test_start_worker_and_finish_are_wired(self):
        import pathlib
        execution = pathlib.Path("execution_control.py").read_text(encoding="utf-8")
        gui = pathlib.Path("gui_pyside6.py").read_text(encoding="utf-8")
        self.assertIn("self._minimize_for_run()", execution)
        self.assertIn("self._restore_after_run()", execution)
        self.assertIn("self._restore_after_run()", gui)


if __name__ == "__main__":
    unittest.main()
