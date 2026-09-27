"""屏幕采集管线的共享实现测试。

主窗口与蓝图窗口此前各有一份近乎逐行相同的采集管线（覆盖层生命周期、
点击/框选/截图回调、取消、清空识别区域、下一模板绑定）。重构后统一到
CaptureOverlayMixin，两个窗口只提供「数据访问 + 收尾钩子」的差异。

本测试锁定重构后的行为，确保两个窗口仍然：
  - 通过 mixin 提供采集方法
  - 把结果写入「各自的」任务列表（主窗口 TASKS / 蓝图窗口 self.tasks）
  - 调用「各自的」收尾钩子
"""

import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g


class CaptureMixinContractTests(unittest.TestCase):
    """先验证契约：两个窗口都应从 mixin 继承采集方法。"""

    def setUp(self):
        # 无头环境下 QMessageBox 会永久阻塞，这里统一屏蔽。
        self._msg_patches = [
            mock.patch.object(g.QMessageBox, "warning", return_value=None),
            mock.patch.object(g.QMessageBox, "information", return_value=None),
            mock.patch.object(g.QMessageBox, "question", return_value=g.QMessageBox.Yes),
            mock.patch.object(g.QMessageBox, "critical", return_value=None),
        ]
        for patcher in self._msg_patches:
            patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        # 预设与任务文件都必须重定向到临时目录：
        # 采集测试会修改 TASKS 并触发保存，若不隔离会写入真实的 saved_tasks.json。
        self._orig_presets = tasks.PRESETS_FILE
        self._orig_tasks = tasks.TASKS_FILE
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        # 还原全局任务列表，避免测试之间互相污染
        g.TASKS[:] = self._tasks_snapshot
        tasks.PRESETS_FILE = self._orig_presets
        tasks.TASKS_FILE = self._orig_tasks
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()
        for patcher in self._msg_patches:
            patcher.stop()

    def test_both_windows_inherit_the_mixin(self):
        self.assertIsInstance(self.window, g.CaptureOverlayMixin)

    def test_mixin_provides_capture_methods(self):
        for name in (
            "_capture_rect", "_show_capture_overlay", "start_click_capture",
            "start_region_capture", "start_next_template_capture", "start_next_region_capture",
            "_begin_dialog_capture", "select_next_template_file", "cancel_capture",
            "finish_too_small", "clear_capture_overlay", "finish_click_capture",
            "finish_region_capture", "clear_match_region", "finish_image_capture",
            "_save_captured_image",
        ):
            with self.subTest(method=name):
                self.assertTrue(hasattr(self.window, name), f"mixin 应提供 {name}")

    def test_blueprint_window_shares_the_same_implementation(self):
        bp = g.BlueprintWindow([], {}, lambda *a: None, {}, self.window, {})
        for name in ("finish_click_capture", "finish_region_capture", "_save_captured_image"):
            with self.subTest(method=name):
                # 取自 mixin，而不是各自类里的一份副本
                self.assertIs(
                    getattr(type(bp), name), getattr(g.CaptureOverlayMixin, name),
                    f"{name} 应直接复用 mixin 实现",
                )
        bp.close()

    def test_main_window_uses_mixin_not_its_own_copy(self):
        for name in ("finish_click_capture", "finish_region_capture", "_save_captured_image"):
            with self.subTest(method=name):
                self.assertIs(getattr(type(self.window), name), getattr(g.CaptureOverlayMixin, name))

    def test_hooks_are_overridden_per_window(self):
        bp = g.BlueprintWindow([], {}, lambda *a: None, {}, self.window, {})
        self.assertIsNot(getattr(type(bp), "_capture_index"), getattr(g.CaptureOverlayMixin, "_capture_index"))
        self.assertIsNot(getattr(type(self.window), "_capture_index"), getattr(g.CaptureOverlayMixin, "_capture_index"))
        bp.close()

    # ---------- 数据访问钩子 ----------

    def test_capture_index_reads_each_windows_own_attribute(self):
        self.window.current_task_index = 3
        self.assertEqual(self.window._capture_index(), 3)
        bp = g.BlueprintWindow([], {}, lambda *a: None, {}, self.window, {})
        bp.current_index = 5
        self.assertEqual(bp._capture_index(), 5)
        bp.close()

    def test_capture_task_list_points_at_each_windows_own_list(self):
        self.assertIs(self.window._capture_task_list(), g.TASKS)
        own = [{"id": "x"}]
        bp = g.BlueprintWindow(own, {}, lambda *a: None, {}, self.window, {})
        self.assertIs(bp._capture_task_list(), bp.tasks)
        bp.close()

    def test_capture_current_task_returns_none_when_index_invalid(self):
        self.window.current_task_index = -1
        self.assertIsNone(self.window._capture_current_task())
        self.window.current_task_index = 10 ** 6
        self.assertIsNone(self.window._capture_current_task())

    # ---------- 点击采集 ----------

    def test_finish_click_capture_writes_to_current_task(self):
        self.window.current_task_index = 0
        before = dict(g.TASKS[0])
        calls = []
        self.window._capture_after_task_changed = lambda: calls.append(True)
        try:
            self.window.finish_click_capture(111, 222)
        finally:
            self.window._capture_after_task_changed = type(self.window)._capture_after_task_changed
        self.assertEqual(g.TASKS[0]["click_x"], 111)
        self.assertEqual(g.TASKS[0]["click_y"], 222)
        self.assertEqual(g.TASKS[0]["click_position"], (111, 222))
        self.assertTrue(calls, "应触发收尾钩子")
        g.TASKS[0].clear()
        g.TASKS[0].update(before)

    def test_finish_click_capture_invokes_callback_when_set(self):
        seen = []
        self.window._capture_callback = lambda payload: seen.append(payload)
        self.window.finish_click_capture(7, 8)
        self.assertEqual(seen, [("click", 7, 8)], "有回调时不应走默认写入路径")
        self.assertIsNone(self.window._capture_callback)

    def test_finish_click_capture_survives_invalid_index(self):
        self.window.current_task_index = -1
        self.window.finish_click_capture(1, 2)   # 不应抛异常

    # ---------- 区域采集 ----------

    def test_finish_region_capture_appends_to_match_rects(self):
        self.window.current_task_index = 0
        task = g.TASKS[0]
        task.pop("match_rects", None)
        task.pop("match_rect", None)
        task.pop("search_rect", None)
        self.window._capture_target = "match"
        self.window.finish_region_capture(10, 20, 30, 40)
        self.assertEqual(task["match_rects"], [(10, 20, 30, 40)])
        self.assertEqual(task["match_rect"], (10, 20, 30, 40))
        self.assertEqual(task["search_rect"], (10, 20, 30, 40))
        self.assertEqual(self.window.match_rect_edit.text(), "10, 20, 30, 40")

    def test_finish_region_capture_next_target_writes_next_rect(self):
        self.window.current_task_index = 0
        task = g.TASKS[0]
        task.pop("next_match_rect", None)
        self.window._capture_target = "next"
        self.window.finish_region_capture(1, 2, 3, 4)
        self.assertEqual(task["next_match_rect"], (1, 2, 3, 4))
        self.assertEqual(task["next_search_rect"], (1, 2, 3, 4))

    def test_finish_region_capture_repairs_non_list_match_rects(self):
        """match_rects 被写成非 list 时应被修回 list 而不是丢弃。"""
        self.window.current_task_index = 0
        task = g.TASKS[0]
        task["match_rects"] = "坏数据"
        self.window._capture_target = "match"
        self.window.finish_region_capture(5, 6, 7, 8)
        self.assertIsInstance(task["match_rects"], list)
        self.assertEqual(task["match_rects"], [(5, 6, 7, 8)])

    # ---------- 清空识别区域 ----------

    def test_clear_match_region_removes_all_rect_keys(self):
        self.window.current_task_index = 0
        task = g.TASKS[0]
        task["match_rects"] = [(1, 2, 3, 4)]
        task["match_rect"] = (1, 2, 3, 4)
        task["search_rect"] = (1, 2, 3, 4)
        self.window.clear_match_region()
        for key in ("match_rects", "match_rect", "search_rect"):
            self.assertNotIn(key, task)
        self.assertEqual(self.window.match_rect_edit.text(), "")

    # ---------- 取消 / 太小 ----------

    def test_cancel_capture_dispatches_cancelled(self):
        seen = []
        self.window._capture_callback = lambda payload: seen.append(payload)
        self.window.cancel_capture()
        self.assertEqual(seen, [("cancelled",)])

    def test_finish_too_small_dispatches_cancelled(self):
        seen = []
        self.window._capture_callback = lambda payload: seen.append(payload)
        self.window.finish_too_small()
        self.assertEqual(seen, [("cancelled",)])

    def test_clear_capture_overlay_without_overlay_is_safe(self):
        self.window.capture_overlay = None
        self.window.clear_capture_overlay()   # 不应抛异常

    # ---------- 下一模板绑定 ----------
    #
    # 注：_save_captured_image 涉及真实屏幕抓取（QApplication.primaryScreen().grabWindow）
    # 与文件落盘，在离屏环境下会弹出模态框并阻塞测试。这里只验证它与回调/任务写入
    # 的契约由 mixin 提供，具体行为不做端到端断言（那需要在真实桌面会话中验证）。

    def test_save_captured_image_comes_from_mixin(self):
        self.assertIs(
            getattr(type(self.window), "_save_captured_image"),
            getattr(g.CaptureOverlayMixin, "_save_captured_image"),
        )


if __name__ == "__main__":
    unittest.main()

