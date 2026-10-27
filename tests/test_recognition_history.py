# -*- coding: utf-8 -*-
"""识别调试面板的「执行步骤历史」+ 点击步骤号码回看当时画面（2026-10-27）。

- 历史只在调试窗口打开时记录（避免正常跑脚本额外编码开销）；
- 同一步骤的多次重试只保留最后一次画面（不刷屏）；
- 历史存 JPEG 字节（不常驻 numpy 帧），最多 100 步；
- 左侧列表显示「第 N 步 ✔/✘ 名称」；点击某行冻结实时预览，显示那一步的画面；
  「回到最新」恢复实时预览；「清空」同时清空历史。
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import main  # noqa: E402
import recognition_debug  # noqa: E402
from recognition_debug_window import RecognitionDebugWindow  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _frame(width=240, height=160, color=(20, 40, 60)):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :] = color
    return image


def _record(step=1, task="步骤", hit=True, template="tpl", confidence=0.9, color=(20, 40, 60)):
    recognition_debug.record(
        _frame(color=color), template=template, center=(20, 10) if hit else None,
        confidence=confidence, threshold=0.8, task=task, hit=hit,
        elapsed=1.0, step_number=step)


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        recognition_debug.clear()

    def tearDown(self):
        recognition_debug.set_history_enabled(False)
        recognition_debug.clear()

    def test_disabled_by_default(self):
        self.assertFalse(recognition_debug.history_enabled())
        _record()
        self.assertEqual(recognition_debug.history_snapshot(), [])

    def test_records_one_entry_per_step_occurrence(self):
        recognition_debug.set_history_enabled(True)
        _record(step=1, task="点击确认", confidence=0.5)
        _record(step=1, task="点击确认", confidence=0.9)   # 重试：只更新最后一帧
        self.assertEqual(len(recognition_debug.history_snapshot()), 1)
        entry = recognition_debug.history_snapshot()[0]
        self.assertEqual(entry["step_number"], 1)
        self.assertAlmostEqual(entry["confidence"], 0.9)
        self.assertTrue(entry["frame_jpeg"])

        _record(step=2, task="下一步")
        entries = recognition_debug.history_snapshot()
        self.assertEqual([e["step_number"] for e in entries], [1, 2])

    def test_same_step_number_after_other_step_is_new_entry(self):
        recognition_debug.set_history_enabled(True)
        _record(step=1, task="A")
        _record(step=2, task="B")
        _record(step=1, task="A")     # 例如跳转回来
        entries = recognition_debug.history_snapshot()
        self.assertEqual([e["step_number"] for e in entries], [1, 2, 1])

    def test_history_is_bounded(self):
        recognition_debug.set_history_enabled(True)
        for step in range(1, recognition_debug.HISTORY_LIMIT + 6):
            _record(step=step, task=f"步骤{step}")
        entries = recognition_debug.history_snapshot()
        self.assertEqual(len(entries), recognition_debug.HISTORY_LIMIT)
        self.assertEqual(entries[-1]["step_number"], recognition_debug.HISTORY_LIMIT + 5,
                         "应保留最近的步骤，丢掉最旧的")

    def test_jpeg_roundtrip(self):
        recognition_debug.set_history_enabled(True)
        _record()
        frame_jpeg = recognition_debug.history_snapshot()[0]["frame_jpeg"]
        self.assertIsInstance(frame_jpeg, bytes)
        decoded = recognition_debug.decode_frame(frame_jpeg)
        self.assertEqual(decoded.shape, (160, 240, 3))

    def test_clear_clears_history(self):
        recognition_debug.set_history_enabled(True)
        _record()
        recognition_debug.clear()
        self.assertEqual(recognition_debug.history_snapshot(), [])


class HistoryWindowTests(unittest.TestCase):
    def setUp(self):
        recognition_debug.clear()
        self._parent = QWidget()
        self.window = RecognitionDebugWindow(self._parent)

    def tearDown(self):
        self.window.close()
        self._parent.deleteLater()
        recognition_debug.set_history_enabled(False)
        recognition_debug.clear()

    def test_show_and_close_toggle_history_recording(self):
        self.window.show()
        self.assertTrue(recognition_debug.history_enabled(), "打开调试窗口后应开始记录历史")
        self.window.close()
        self.assertFalse(recognition_debug.history_enabled(), "关闭后应停止记录")

    def test_history_list_labels_and_click_views_that_frame(self):
        recognition_debug.set_history_enabled(True)
        _record(step=1, task="点击确认", color=(0, 0, 255))
        _record(step=2, task="等待画面", hit=False, color=(0, 255, 0))
        self.window.refresh_from_snapshot()
        labels = [self.window.history_list.item(i).text() for i in range(self.window.history_list.count())]
        self.assertEqual(len(labels), 2)
        self.assertIn("第 1 步", labels[0])
        self.assertIn("点击确认", labels[0])
        self.assertIn("第 2 步", labels[1])

        self.window._on_history_item_clicked(self.window.history_list.item(0))
        _APP.processEvents()
        self.assertTrue(self.window._viewing_history)
        self.assertIn("历史画面", self.window.info_label.text())
        self.assertIn("点击确认", self.window.info_label.text())
        pixmap = self.window.image_label.pixmap()
        self.assertIsNotNone(pixmap)
        self.assertFalse(pixmap.isNull(), "历史条目应能显示当时的画面")

    def test_auto_refresh_does_not_override_history_view(self):
        recognition_debug.set_history_enabled(True)
        _record(step=1, task="步骤A")
        self.window.refresh_from_snapshot()
        self.window._on_history_item_clicked(self.window.history_list.item(0))
        _record(step=2, task="步骤B")
        self.window._auto_refresh()
        _APP.processEvents()
        self.assertTrue(self.window._viewing_history)
        self.assertIn("历史画面", self.window.info_label.text(), "看历史时不应被实时画面覆盖")
        self.assertEqual(self.window.history_list.count(), 2, "左侧列表仍应刷新")

    def test_back_to_live_restores_live_preview(self):
        recognition_debug.set_history_enabled(True)
        _record(step=1, task="步骤A")
        self.window.refresh_from_snapshot()
        self.window._on_history_item_clicked(self.window.history_list.item(0))
        _record(step=2, task="步骤B")
        self.window._on_back_to_live()
        _APP.processEvents()
        self.assertFalse(self.window._viewing_history)
        self.assertNotIn("历史画面", self.window.info_label.text())
        self.assertIn("步骤B", self.window.info_label.text())

    def test_clear_button_clears_history_list(self):
        recognition_debug.set_history_enabled(True)
        _record()
        self.window.refresh_from_snapshot()
        self.assertEqual(self.window.history_list.count(), 1)
        self.window.clear_button.click()
        _APP.processEvents()
        self.assertEqual(self.window.history_list.count(), 0)
        self.assertEqual(recognition_debug.history_snapshot(), [])

    def test_empty_history_shows_hint(self):
        self.window.refresh_from_snapshot()
        self.assertTrue(self.window.history_hint.isVisibleTo(self.window))


class EngineStepNumberTests(unittest.TestCase):
    def setUp(self):
        recognition_debug.clear()
        recognition_debug.set_history_enabled(True)
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
        recognition_debug.set_history_enabled(False)
        recognition_debug.clear()

    def test_execute_task_records_step_number(self):
        with mock.patch.object(main, "find_match_by_strategy",
                               lambda *a, **k: ("a", (11, 22), 0.97)):
            main.execute_task({"type": "normal", "template": "a", "timeout": 1,
                               "description": "第 3 步识别", "_outer_step_number": 3},
                              log_callback=lambda m: None)
        entries = recognition_debug.history_snapshot()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["step_number"], 3)
        self.assertEqual(entries[0]["task"], "第 3 步识别")
        self.assertTrue(entries[0]["hit"])


class HistoryGuardTests(unittest.TestCase):
    def test_implementation_markers(self):
        store = open("recognition_debug.py", encoding="utf-8").read()
        window = open("recognition_debug_window.py", encoding="utf-8").read()
        engine = open("main.py", encoding="utf-8").read()
        self.assertIn("def set_history_enabled", store)
        self.assertIn("def history_snapshot", store)
        self.assertIn("def decode_frame", store)
        self.assertIn("step_number=task.get(\"_outer_step_number\")", engine)
        self.assertIn("history_list", window)
        self.assertIn("_on_history_item_clicked", window)
        self.assertIn("回到最新", window)


if __name__ == "__main__":
    unittest.main()
