# -*- coding: utf-8 -*-
"""F5「识别调试面板」：快照存储、帧转图、窗口显示、引擎埋点。"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import main  # noqa: E402
import recognition_debug  # noqa: E402
from recognition_debug_window import RecognitionDebugWindow  # noqa: E402
from ui_common import _frame_to_pixmap  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _frame(width=200, height=120):
    return np.zeros((height, width, 3), dtype=np.uint8)


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        recognition_debug.clear()

    def tearDown(self):
        recognition_debug.clear()

    def test_record_and_snapshot_roundtrip(self):
        frame = _frame()
        recognition_debug.record(frame, template="a", center=(10, 20), confidence=0.91,
                                 threshold=0.8, task="步骤 A", hit=True, elapsed=1.25)
        state = recognition_debug.snapshot()
        self.assertTrue(state["hit"])
        self.assertEqual(state["template"], "a")
        self.assertEqual(state["center"], (10, 20))
        self.assertAlmostEqual(state["confidence"], 0.91)
        self.assertAlmostEqual(state["threshold"], 0.8)
        self.assertEqual(state["task"], "步骤 A")
        self.assertEqual(state["elapsed"], 1.25)
        self.assertIs(state["frame"], frame, "保存的是引用（避免每 0.1 秒深拷贝一帧）")
        self.assertGreater(state["timestamp"], 0)

    def test_snapshot_is_a_copy(self):
        recognition_debug.record(_frame(), template="a", hit=True)
        snapshot = recognition_debug.snapshot()
        snapshot["template"] = "changed"
        self.assertEqual(recognition_debug.snapshot()["template"], "a")

    def test_clear_resets_state(self):
        recognition_debug.record(_frame(), template="a", hit=True)
        recognition_debug.clear()
        state = recognition_debug.snapshot()
        self.assertFalse(state["hit"])
        self.assertIsNone(state["frame"])
        self.assertEqual(state["timestamp"], 0.0)


class FrameConversionTests(unittest.TestCase):
    def test_bgr_frame_converts(self):
        pixmap = _frame_to_pixmap(_frame(320, 200))
        self.assertIsNotNone(pixmap)
        self.assertEqual((pixmap.width(), pixmap.height()), (320, 200))

    def test_max_width_scales_down(self):
        pixmap = _frame_to_pixmap(_frame(800, 400), max_width=200)
        self.assertEqual(pixmap.width(), 200)
        self.assertEqual(pixmap.height(), 100)

    def test_none_and_bad_shapes_are_safe(self):
        self.assertIsNone(_frame_to_pixmap(None))
        self.assertIsNone(_frame_to_pixmap("not an array"))

    def test_grayscale_is_rejected(self):
        self.assertIsNone(_frame_to_pixmap(np.zeros((10, 10), dtype=np.uint8)))


class DebugWindowTests(unittest.TestCase):
    def setUp(self):
        recognition_debug.clear()
        self.window = RecognitionDebugWindow()

    def tearDown(self):
        self.window.close()
        recognition_debug.clear()

    def test_hit_shows_template_score_and_position(self):
        recognition_debug.record(_frame(), template="btn_ok", center=(50, 40), confidence=0.93,
                                 threshold=0.75, task="点击确认", hit=True, elapsed=2.0)
        self.window.refresh_from_snapshot()
        text = self.window.info_label.text()
        self.assertIn("命中", text)
        self.assertIn("btn_ok", text)
        self.assertIn("0.93", text)
        self.assertIn("(50, 40)", text)
        self.assertIn("点击确认", text)

    def test_miss_shows_not_found(self):
        recognition_debug.record(_frame(), template="btn_ok", threshold=0.75,
                                 task="点击确认", hit=False, elapsed=3.0)
        self.window.refresh_from_snapshot()
        text = self.window.info_label.text()
        self.assertIn("未命中", text)
        self.assertIn("0.75", text)

    def test_hit_draws_marker_on_pixmap(self):
        recognition_debug.record(_frame(), template="btn_ok", center=(50, 40),
                                 confidence=0.93, hit=True)
        self.window.refresh_from_snapshot()
        pixmap = self.window.image_label.pixmap()
        self.assertFalse(pixmap.isNull())
        image = pixmap.toImage()
        colors = {image.pixelColor(x, y).name() for x in range(pixmap.width())
                  for y in range(pixmap.height())}
        self.assertIn("#ef4444", colors, "命中位置应画红色标记")

    def test_pause_stops_auto_refresh(self):
        recognition_debug.record(_frame(), template="a", hit=True)
        self.window.refresh_from_snapshot()
        self.window.pause_checkbox.setChecked(True)
        recognition_debug.record(_frame(), template="b", hit=True)
        self.window.refresh_from_snapshot(only_if_new=True)
        self.assertIn("a", self.window.info_label.text(), "暂停后不该跟着刷新")

    def test_clear_button_resets_panel(self):
        recognition_debug.record(_frame(), template="a", hit=True)
        self.window.refresh_from_snapshot()
        self.window.clear_button.click()
        self.assertIn("尚无识别记录", self.window.info_label.text())


class EngineRecordingTests(unittest.TestCase):
    def setUp(self):
        recognition_debug.clear()
        self._patches = [
            mock.patch.object(main, "capture_screen", lambda *a, **k: _frame()),
            mock.patch.object(main, "match_task_templates", lambda *a, **k: {}),
            mock.patch.object(main, "interruptible_sleep", lambda s, f: False),
            mock.patch.object(main, "click_global_position", lambda *a, **k: None),
            mock.patch.object(main, "wait_for_step_result", lambda *a, **k: True),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()
        recognition_debug.clear()

    def test_hit_is_recorded(self):
        with mock.patch.object(main, "find_match_by_strategy",
                               lambda *a, **k: ("a", (11, 22), 0.97)):
            main.execute_task({"type": "normal", "template": "a", "timeout": 0,
                               "description": "记录命中"}, log_callback=lambda m: None)
        state = recognition_debug.snapshot()
        self.assertTrue(state["hit"])
        self.assertEqual(state["template"], "a")
        self.assertEqual(state["center"], (11, 22))
        self.assertAlmostEqual(state["confidence"], 0.97)
        self.assertEqual(state["task"], "记录命中")
        self.assertIsNotNone(state["frame"])

    def test_miss_is_recorded(self):
        with mock.patch.object(main, "find_match_by_strategy",
                               lambda *a, **k: (None, None, 0.0)):
            with self.assertRaises(RuntimeError):
                main.execute_task({"type": "normal", "template": "a", "timeout": 0.02,
                                   "description": "记录未命中"}, log_callback=lambda m: None)
        state = recognition_debug.snapshot()
        self.assertFalse(state["hit"])
        self.assertIsNone(state["center"])
        self.assertEqual(state["task"], "记录未命中")


class DebugButtonTests(unittest.TestCase):
    def test_main_window_opens_debug_window(self):
        window = g.PySide6ScriptWindow()
        try:
            window.open_recognition_debug()
            self.assertIsNotNone(getattr(window, "recognition_debug_window", None))
            window.open_recognition_debug()
            self.assertIs(window.recognition_debug_window, window.recognition_debug_window)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
