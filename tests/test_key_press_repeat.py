# -*- coding: utf-8 -*-
"""key_press：按下次数 + 每次间隔。"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import main
import gui_pyside6 as g
from capture_overlay import CaptureOverlayMixin
from tasks import make_key_press_task

_APP = QApplication.instance() or QApplication([])


class KeyPressRepeatTests(unittest.TestCase):
    def _run(self, task, stop_at_interval=False):
        events = {"down": 0, "up": 0, "sleeps": []}

        def fake_sleep(seconds, flag=None):
            events["sleeps"].append(seconds)
            if stop_at_interval and seconds == 0.5:
                return True
            return False

        with mock.patch.object(main, "key_down", lambda key: events.__setitem__("down", events["down"] + 1)), \
                mock.patch.object(main, "key_up", lambda key: events.__setitem__("up", events["up"] + 1)), \
                mock.patch.object(main, "interruptible_sleep", fake_sleep), \
                mock.patch.object(main, "wait_for_step_result", lambda *a, **k: True):
            result = main.execute_key_press_task(task, log_callback=lambda *_: None)
        return result, events

    def test_repeats_with_interval(self):
        task = {"type": "key_press", "key": "F", "hold_time": 0.1,
                "press_count": 3, "press_interval": 0.5, "after_wait": 0}
        result, events = self._run(task)
        self.assertTrue(result)
        self.assertEqual(events["down"], 3)
        self.assertEqual(events["up"], 3)
        self.assertEqual(events["sleeps"], [0.1, 0.5, 0.1, 0.5, 0.1])

    def test_missing_fields_keep_single_press(self):
        task = {"type": "key_press", "key": "F", "hold_time": 0.1, "after_wait": 0}
        result, events = self._run(task)
        self.assertTrue(result)
        self.assertEqual(events["down"], 1)
        self.assertEqual(events["sleeps"], [0.1])

    def test_stop_during_interval_returns_false_and_releases_key(self):
        task = {"type": "key_press", "key": "F", "hold_time": 0.1,
                "press_count": 3, "press_interval": 0.5, "after_wait": 0}
        result, events = self._run(task, stop_at_interval=True)
        self.assertFalse(result)
        self.assertEqual(events["down"], 1)
        self.assertEqual(events["up"], 1)

    def test_new_task_gets_defaults(self):
        task = make_key_press_task("k", "custom", "按键", "E")
        self.assertEqual(task["press_count"], 1)
        self.assertEqual(task["press_interval"], 0.0)

    def test_both_editor_specs_expose_new_fields(self):
        blueprint = CaptureOverlayMixin._special_field_specs(None, {"type": "key_press"})
        main_spec = g.PySide6ScriptWindow._special_field_specs(None, {"type": "key_press"})
        for spec in (blueprint, main_spec):
            keys = {item[0] for item in spec}
            self.assertIn("press_count", keys)
            self.assertIn("press_interval", keys)


if __name__ == "__main__":
    unittest.main()
