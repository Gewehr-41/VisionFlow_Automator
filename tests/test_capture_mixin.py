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
import main

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
        # 识别区域没有手工输入坐标的控件了，唯一"当前条"的表达就是 _region_index
        self.assertEqual(self.window._region_index, 0)

    def test_finish_region_capture_selects_the_new_region(self):
        """框第 2 条时当前下标必须变成 1（新框的那条），而不是留在 0。

        否则"框了第 2 个区域"在界面上毫无反馈，删除按钮还会删错条目。
        """
        self.window.current_task_index = 0
        task = g.TASKS[0]
        task.pop("match_rects", None)
        self.window._capture_target = "match"

        self.window.finish_region_capture(10, 20, 30, 40)
        self.assertEqual(self.window._region_index, 0)
        self.window.finish_region_capture(50, 60, 70, 80)
        self.assertEqual(self.window._region_index, 1, "新框的区域应成为当前选中项")

    def test_clear_match_region_resets_the_region_index(self):
        self.window.current_task_index = 0
        task = g.TASKS[0]
        # 夹具里的任务可能自带区域，先清干净，让下标从 0 开始数
        task.pop("match_rects", None)
        task.pop("match_rect", None)
        task.pop("search_rect", None)
        self.window._capture_target = "match"
        self.window.finish_region_capture(10, 20, 30, 40)
        self.window.finish_region_capture(50, 60, 70, 80)
        self.assertEqual(self.window._region_index, 1)

        self.window.clear_match_region()

        self.assertEqual(self.window._region_index, 0)

    def test_finish_region_capture_next_target_does_not_add_a_region(self):
        """框选「下一模板出现位置」时不该把它算进识别区域。"""
        self.window.current_task_index = 0
        task = g.TASKS[0]
        task.pop("match_rects", None)
        task.pop("next_match_rect", None)
        before_index = self.window._region_index
        self.window._capture_target = "next"

        self.window.finish_region_capture(1, 2, 3, 4)

        self.assertNotIn("match_rects", task)
        self.assertEqual(task["next_match_rect"], (1, 2, 3, 4))
        self.assertEqual(self.window._region_index, before_index)

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

    # ---------- 绑定图片对话框（select_template_file）----------
    #
    # 该方法此前在两个窗口各有一份 90 行实现，实测差异只有 3 处索引访问。
    # 现已统一到 mixin，这里锁定：取自 mixin、无当前步骤时直接返回、
    # 能正确构造对话框并把候选模板写入编辑控件。

    def test_select_template_file_comes_from_mixin(self):
        for cls in (type(self.window), g.BlueprintWindow):
            with self.subTest(cls=cls.__name__):
                self.assertIs(
                    getattr(cls, "select_template_file"),
                    getattr(g.CaptureOverlayMixin, "select_template_file"),
                )

    def test_select_template_file_returns_early_without_current_task(self):
        self.window.current_task_index = -1
        with mock.patch.object(g.QDialog, "exec", side_effect=AssertionError("不应打开对话框")):
            self.window.select_template_file()   # 不应抛异常，也不应打开对话框

    def test_select_template_file_seeds_list_from_template_edit(self):
        self.window.current_task_index = 0
        self.window.template_edit.setText("auto, start_loop")
        captured = {}

        def fake_exec(dialog_self):
            # 对话框已构建完成，取出其中的 QListWidget 检查初始项
            from PySide6.QtWidgets import QListWidget
            widget = dialog_self.findChild(QListWidget)
            captured["items"] = [widget.item(i).text() for i in range(widget.count())]
            return 0

        with mock.patch.object(g.QDialog, "exec", fake_exec):
            self.window.select_template_file()
        self.assertEqual(captured.get("items"), ["auto", "start_loop"])

    def test_select_template_file_save_writes_back_to_template_edit(self):
        """点「保存」应把列表内容写回模板编辑框。"""
        self.window.current_task_index = 0
        self.window.template_edit.setText("alpha")
        from PySide6.QtWidgets import QListWidget, QPushButton

        def fake_exec(dialog_self):
            widget = dialog_self.findChild(QListWidget)
            widget.addItem("beta")
            # 找到「保存」按钮并触发
            for button in dialog_self.findChildren(QPushButton):
                if button.text() == "保存":
                    button.click()
                    break
            return 0

        with mock.patch.object(g.QDialog, "exec", fake_exec):
            self.window.select_template_file()
        self.assertEqual(self.window.template_edit.text(), "alpha, beta")

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
        self.assertEqual(self.window.region_selector.count(), 0)
        self.assertEqual(self.window.region_status_label.text(), "未设置")

    # ---------- 清空点击点 ----------
    #
    # 所有者 2026-09-28 反馈：界面上只有「记录点击点」，没有一个可见的"清空"入口
    # （此前只能把 X/Y 两个框清空再点「应用修改」）。按钮放在「点击: X/Y」同一行末尾。

    def _task_for_click_tests(self):
        """保证 TASKS 里至少一项并让下标指向它——不依赖本地真实数据的条数。"""
        if not g.TASKS:
            g.TASKS.append({"id": "probe", "type": "normal", "description": "探针"})
        self.window.current_task_index = 0
        return g.TASKS[0]

    def test_clear_click_point_removes_all_click_keys(self):
        task = self._task_for_click_tests()
        task.update({"click_x": 1779, "click_y": 463, "click_position": (1779, 463)})
        self.window.click_x_edit.setText("1779")
        self.window.click_y_edit.setText("463")

        self.window.clear_click_point()

        for key in ("click_x", "click_y", "click_position"):
            self.assertNotIn(key, task, "三个键必须一起删：只删 click_x/y 时 click_position 仍会生效")
        self.assertEqual(self.window.click_x_edit.text(), "")
        self.assertEqual(self.window.click_y_edit.text(), "")
        self.assertIsNone(main.resolve_click_position(task), "引擎视角：已没有备用点击点")

    def test_clear_click_point_keeps_the_rest_of_the_task(self):
        task = self._task_for_click_tests()
        task.update({"click_x": 10, "click_y": 20, "click_position": (10, 20),
                     "match_rects": [[1, 2, 3, 4]], "threshold": 0.8, "wait_for": "time"})
        self.window.clear_click_point()
        self.assertEqual(task["match_rects"], [[1, 2, 3, 4]], "只清点击点，不能动识别区域")
        self.assertEqual(task["threshold"], 0.8)
        self.assertEqual(task["wait_for"], "time")

    def test_click_clear_button_sits_in_the_same_row_as_xy_in_both_windows(self):
        """按钮必须与 X/Y 同一行（所有者指定放在"点击 xy 设置栏后面"），两个窗口都要有。"""
        def row_containing(layout, *widgets):
            for index in range(layout.count()):
                sub = layout.itemAt(index).layout()
                if sub is None:
                    continue
                items = {sub.itemAt(i).widget() for i in range(sub.count())}
                if all(widget in items for widget in widgets):
                    return sub
                deeper = row_containing(sub, *widgets)
                if deeper is not None:
                    return deeper
            return None

        blueprint = g.BlueprintWindow([], {}, lambda *a: None, {}, self.window, {})
        for window in (self.window, blueprint):
            button = window.click_clear_button
            self.assertEqual(button.text(), "清空点击点")
            layout = window.click_x_edit.parentWidget().layout()
            self.assertIsNotNone(
                row_containing(layout, window.click_x_edit, window.click_y_edit, button),
                "「清空点击点」应与 X/Y 在同一行",
            )

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


    # ---------- 分组编辑（也已统一到 mixin）----------

    def test_group_editing_methods_come_from_mixin(self):
        for name in (
            "_load_editor_for_group", "_apply_group_editor",
            "_pick_group_color", "_update_group_color_button",
            "_group_names", "_group_colors", "_group_order",
            "_group_children", "_group_parents",
        ):
            with self.subTest(method=name):
                self.assertIs(
                    getattr(type(self.window), name), getattr(g.CaptureOverlayMixin, name),
                    f"{name} 应直接复用 mixin 实现",
                )

    def test_group_metadata_accessor_differs_per_window(self):
        own = copy.deepcopy(g.TASKS)
        bp = g.BlueprintWindow(own, {}, lambda *a: None, {"names": {"g": "蓝图组"}}, self.window, {})
        # 蓝图窗口使用自己的副本
        self.assertIs(bp._group_metadata_for_current_mode(), bp.group_metadata)
        self.assertEqual(bp._group_names(), {"g": "蓝图组"})
        # 主窗口使用 mode_group_metadata[当前预设]
        mode = self.window.mode_combo.currentText() or "custom"
        self.assertIs(self.window._group_metadata_for_current_mode(), self.window.mode_group_metadata[mode])
        bp.close()

    def test_group_colors_and_defaults_differ_per_window(self):
        self.assertEqual(self.window._group_default_color(), "#e0f2fe")
        self.assertEqual(self.window._group_color_button_text_color(), "#1f2937")
        bp = g.BlueprintWindow([], {}, lambda *a: None, {}, self.window, {})
        self.assertEqual(bp._group_default_color(), "#38bdf8")
        self.assertEqual(bp._group_color_button_text_color(), "#ffffff")
        self.assertEqual(bp._group_color_dialog_title(), "选择组颜色")
        bp.close()

    def test_update_group_color_button_uses_existing_color(self):
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[mode] = {"colors": {"g1": "#abcdef"}}
        self.window._pending_group_color = None
        self.window._update_group_color_button("g1")
        self.assertEqual(self.window.group_color_button.text(), "#abcdef")

    def test_update_group_color_button_falls_back_to_default(self):
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[mode] = {"colors": {}}
        self.window._pending_group_color = None
        self.window._update_group_color_button("未记录颜色的组")
        self.assertEqual(self.window.group_color_button.text(), "#e0f2fe")

    def test_update_group_color_button_prefers_pending_color(self):
        self.window._pending_group_color = "#111111"
        try:
            self.window._update_group_color_button("任意组")
            self.assertEqual(self.window.group_color_button.text(), "#111111")
        finally:
            self.window._pending_group_color = None

    def test_load_editor_for_group_fills_name_and_clears_current_task(self):
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[mode] = {"names": {"g1": "甲组"}}
        self.window.current_task_index = 5
        self.window._pending_group_color = "#999999"
        self.window._load_editor_for_group("g1")
        self.assertEqual(self.window.group_name_edit.text(), "甲组")
        self.assertEqual(self.window.current_task_index, -1, "应清除当前步骤")
        self.assertEqual(self.window.current_group_id, "g1")
        self.assertIsNone(self.window._pending_group_color, "应重置待定颜色")

    def test_load_editor_for_group_unknown_id_falls_back_to_id(self):
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[mode] = {"names": {}}
        self.window._load_editor_for_group("未知组")
        self.assertEqual(self.window.group_name_edit.text(), "未知组")

    def test_apply_group_editor_writes_name_and_syncs_tasks(self):
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[mode] = {"names": {"g1": "旧名"}, "colors": {}}
        for task in g.TASKS:
            task["group_id"] = "g1"
        self.window.current_group_id = "g1"
        self.window.group_name_edit.setText("新名")
        self.window._pending_group_color = None
        self.window._apply_group_editor()
        self.assertEqual(self.window._group_names().get("g1"), "新名")
        for task in g.TASKS:
            self.assertEqual(task.get("group_name"), "新名", "步骤上的组名应同步")

    def test_apply_group_editor_writes_pending_color(self):
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[mode] = {"names": {"g1": "组"}, "colors": {}}
        for task in g.TASKS:
            task["group_id"] = "g1"
        self.window.current_group_id = "g1"
        self.window.group_name_edit.setText("组")
        self.window._pending_group_color = "#00ff00"
        self.window._apply_group_editor()
        self.assertEqual(self.window._group_colors().get("g1"), "#00ff00")
        for task in g.TASKS:
            self.assertEqual(task.get("group_color"), "#00ff00")
        self.window._pending_group_color = None

    def test_apply_group_editor_without_group_is_noop(self):
        self.window.current_group_id = None
        self.window._apply_group_editor()   # 不应抛异常

    def test_blueprint_group_metadata_is_isolated_from_main_window(self):
        """蓝图窗口持有副本，改它不应影响主窗口的元数据。"""
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[mode] = {"names": {"shared": "原值"}, "colors": {}}
        bp = g.BlueprintWindow(
            copy.deepcopy(g.TASKS), {},
            lambda *a: None,
            copy.deepcopy(self.window.mode_group_metadata[mode]),
            self.window, {},
        )
        bp.group_metadata.setdefault("names", {})["shared"] = "被蓝图改了"
        self.assertEqual(self.window._group_names().get("shared"), "原值")
        bp.close()


    # ---------- 迂回编辑器（也已统一到 mixin）----------

    def test_detour_editor_methods_come_from_mixin(self):
        for name in ("open_detour_editor", "_configure_detour_step"):
            with self.subTest(method=name):
                self.assertIs(
                    getattr(g.PySide6ScriptWindow, name), getattr(g.CaptureOverlayMixin, name),
                    f"{name} 应直接复用 mixin 实现",
                )
                self.assertIs(
                    getattr(g.BlueprintWindow, name), getattr(g.CaptureOverlayMixin, name),
                    f"{name} 在蓝图窗口也应复用 mixin 实现",
                )

    def test_open_detour_editor_returns_early_without_current_task(self):
        self.window.current_task_index = -1
        with mock.patch.object(g.QDialog, "exec", side_effect=AssertionError("不应打开对话框")):
            self.window.open_detour_editor()

    def test_open_detour_editor_returns_early_for_unsupported_type(self):
        self.window.current_task_index = 0
        original = g.TASKS[0].get("type")
        g.TASKS[0]["type"] = "key_press"     # 只有 normal / advanced 支持迂回
        try:
            with mock.patch.object(g.QDialog, "exec", side_effect=AssertionError("不应打开对话框")):
                self.window.open_detour_editor()
        finally:
            g.TASKS[0]["type"] = original

    def test_open_detour_editor_creates_detour_steps_for_normal_task(self):
        self.window.current_task_index = 0
        g.TASKS[0]["type"] = "normal"
        g.TASKS[0].pop("detour_steps", None)
        with mock.patch.object(g.QDialog, "exec", return_value=0):
            self.window.open_detour_editor()
        self.assertIsInstance(g.TASKS[0].get("detour_steps"), list,
                              "normal 步骤应初始化 detour_steps")

    def test_open_detour_editor_repairs_non_list_detour_steps(self):
        self.window.current_task_index = 0
        g.TASKS[0]["type"] = "normal"
        g.TASKS[0]["detour_steps"] = "坏数据"
        with mock.patch.object(g.QDialog, "exec", return_value=0):
            self.window.open_detour_editor()
        self.assertIsInstance(g.TASKS[0]["detour_steps"], list)

    def _blueprint_window(self):
        mode = self.window.mode_combo.currentText() or "custom"
        return g.BlueprintWindow(
            copy.deepcopy(g.TASKS), {},
            lambda *a: None,
            copy.deepcopy(self.window.mode_group_metadata.get(mode, {})),
            self.window, {},
        )

    def _drive_detour_dialog(self, owner, step, prepare=None):
        """打开迂回子步骤对话框并点「保存」，返回 (是否走完保存, 弹窗警告列表)。

        必须断言 dialog.accept() 是否被调用：保存过程中若抛异常（例如 mixin 调用了
        只存在于另一个窗口的辅助方法），PySide6 只把 traceback 打到 stderr，槽里
        静默失败、对话框不关闭。此时**只看字段是否被写入会假通过**——崩溃点之前的
        字段照样会被写进去。
        """
        from PySide6.QtWidgets import QDialog, QPushButton

        accepted = []
        warnings = []
        real_accept = QDialog.accept

        def spy_accept(dialog_self):
            accepted.append(True)
            return real_accept(dialog_self)

        def fake_exec(dialog_self):
            if prepare is not None:
                prepare(dialog_self)
            for button in dialog_self.findChildren(QPushButton):
                if button.text() == "保存":
                    button.click()
                    break
            return 0

        def record_warning(*args, **_kwargs):
            warnings.append(args[1] if len(args) > 1 else "")
            return None

        with mock.patch.object(g.QDialog, "exec", fake_exec), \
                mock.patch.object(QDialog, "accept", spy_accept), \
                mock.patch.object(g.QMessageBox, "warning", side_effect=record_warning):
            owner._configure_detour_step(step, None)
        return bool(accepted), warnings

    def test_configure_detour_step_saves_in_main_window(self):
        """主窗口也必须能走完保存。

        回归：36da50b 把两份对话框合成一份 mixin 实现时，保留了蓝图窗口的辅助
        函数名（_int/_float/_parse_rect_text），主窗口没有这三个方法，点「保存」
        在 Qt 槽里抛 AttributeError，对话框不关闭、界面上看起来什么都没发生。
        """
        step = {
            "type": "normal",
            "description": "子步骤",
            "template": "auto",
            "click_x": 11,
            "click_y": 22,
            "match_rect": [1, 2, 3, 4],
        }

        accepted, warnings = self._drive_detour_dialog(self.window, step)

        self.assertTrue(accepted, "保存应走完 dialog.accept()；没有走完说明槽里抛异常被静默吞掉")
        self.assertEqual(warnings, [])
        self.assertEqual(step["type"], "normal")
        self.assertEqual(step["description"], "子步骤")
        self.assertEqual(step["template"], "auto")
        self.assertEqual((step["click_x"], step["click_y"]), (11, 22))
        self.assertEqual(step["click_position"], (11, 22))
        self.assertEqual(step["match_rect"], (1, 2, 3, 4))

    def test_detour_dialog_keeps_region_for_both_windows(self):
        """打开对话框什么都不改直接保存，不能把识别区域弄丢。

        回归：区域曾按 Python 列表的 repr 显示成 '[30, 28, 89, 81]'，解析失败后
        走了"清空区域"分支，识别范围从某个区域变成全屏。
        """
        blueprint = self._blueprint_window()
        try:
            for label, owner in (("主窗口", self.window), ("蓝图窗口", blueprint)):
                with self.subTest(owner=label):
                    step = {
                        "type": "normal",
                        "description": "退出茶吧",
                        "match_rect": [30, 28, 89, 81],
                        "search_rect": [30, 28, 89, 81],
                    }
                    accepted, warnings = self._drive_detour_dialog(owner, step)
                    self.assertTrue(accepted)
                    self.assertEqual(warnings, [])
                    self.assertEqual(step["match_rect"], (30, 28, 89, 81))
                    self.assertEqual(step["search_rect"], (30, 28, 89, 81))
                    self.assertEqual(step["match_rects"], [(30, 28, 89, 81)])
        finally:
            blueprint.close()

    def test_detour_dialog_shows_region_without_brackets(self):
        from PySide6.QtWidgets import QLineEdit

        shown = []

        def prepare(dialog_self):
            for box in dialog_self.findChildren(QLineEdit):
                if box.text().count(",") == 3:
                    shown.append(box.text())

        self._drive_detour_dialog(self.window, {"type": "normal", "match_rect": [30, 28, 89, 81]},
                                 prepare=prepare)

        self.assertEqual(shown, ["30, 28, 89, 81"], "区域应显示成可再次解析的 4 个数字")

    def test_detour_dialog_refuses_invalid_region_and_keeps_it(self):
        from PySide6.QtWidgets import QLineEdit

        def prepare(dialog_self):
            for box in dialog_self.findChildren(QLineEdit):
                if box.text().count(",") == 3:
                    box.setText("乱七八糟")

        step = {"type": "normal", "description": "退出茶吧", "match_rect": [30, 28, 89, 81]}
        accepted, warnings = self._drive_detour_dialog(self.window, step, prepare=prepare)

        self.assertFalse(accepted, "区域解析不了时不应关闭对话框")
        self.assertEqual(len(warnings), 1, "应弹出一次明确提示")
        self.assertEqual(step["match_rect"], [30, 28, 89, 81], "非法输入绝不能把已有区域删掉")

    def test_detour_dialog_loads_the_five_fields(self):
        """这 5 个键引擎一直在用，此前对话框既不显示也不回写。"""
        from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit

        seen = {}

        def prepare(dialog_self):
            for box in dialog_self.findChildren(QLineEdit):
                if box.text() == "12.5":
                    seen["timeout"] = True
            for combo in dialog_self.findChildren(QComboBox):
                if combo.count() == 3 and "等待目标模板出现" in combo.itemText(1):
                    seen["wait_for_index"] = combo.currentIndex()
            for box in dialog_self.findChildren(QCheckBox):
                if box.text() == "可选步骤（跳过）":
                    seen["optional"] = box.isChecked()
                if box.text() == "必须识别到图片再点击":
                    seen["match_required"] = box.isChecked()

        step = {
            "type": "normal",
            "timeout": 12.5,
            "wait_for": "next_appear",
            "click_requires_match": False,
            "optional": True,
        }
        accepted, warnings = self._drive_detour_dialog(self.window, step, prepare=prepare)

        self.assertTrue(accepted)
        self.assertEqual(warnings, [])
        self.assertTrue(seen.get("timeout"), "超时输入框应显示已存的 12.5")
        self.assertEqual(seen.get("wait_for_index"), 1, "等待方式应显示已存的 next_appear")
        self.assertTrue(seen.get("optional"))
        self.assertFalse(seen.get("match_required"))

    def test_detour_dialog_saves_the_four_fields(self):
        from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit

        def prepare(dialog_self):
            for box in dialog_self.findChildren(QLineEdit):
                if box.text() == "5.0":
                    box.setText("9.5")
            for combo in dialog_self.findChildren(QComboBox):
                if combo.count() == 3 and "等待目标模板出现" in combo.itemText(1):
                    combo.setCurrentIndex(1)
            for box in dialog_self.findChildren(QCheckBox):
                if box.text() == "可选步骤（跳过）":
                    box.setChecked(True)

        step = {
            "type": "normal",
            "timeout": 5.0,
            "offset": [0, 0],
            "wait_for": "time",
            "click_requires_match": True,
            "optional": False,
            "required": True,
        }
        accepted, warnings = self._drive_detour_dialog(self.window, step, prepare=prepare)

        self.assertTrue(accepted)
        self.assertEqual(warnings, [])
        self.assertEqual(step["timeout"], 9.5)
        self.assertEqual(step["wait_for"], "next_appear")
        # 原来的「偏移」一行已按所有者要求删除，对话框不再碰 offset
        self.assertEqual(step["offset"], [0, 0], "对话框不该改动 offset")
        self.assertNotIn("offset_x", step)
        self.assertNotIn("offset_y", step)
        self.assertTrue(step["optional"])
        self.assertFalse(step["required"], "required 必须跟着 optional 一起翻")
        self.assertTrue(step["click_requires_match"])

    def test_detour_dialog_hides_normal_only_fields_for_other_types(self):
        """非普通/高级类型的子步骤不该显示、也不该被写入这几个键。"""
        from PySide6.QtWidgets import QGroupBox

        visibility = []

        def prepare(dialog_self):
            for box in dialog_self.findChildren(QGroupBox):
                if "识别与点击" in box.title():
                    visibility.append(box.isVisibleTo(dialog_self))

        step = {
            "type": "drag",
            "description": "向左拖曳画面",
            "template": "drag",
            "start_x": 500, "start_y": 300, "end_x": 100, "end_y": 300,
            "duration": 0.25,
        }
        accepted, warnings = self._drive_detour_dialog(self.window, step, prepare=prepare)

        self.assertTrue(accepted)
        self.assertEqual(warnings, [])
        self.assertEqual(visibility, [False], "拖拽类型不应显示普通/高级专属字段")
        for key in ("timeout", "offset", "offset_x", "offset_y", "wait_for",
                    "click_requires_match", "optional", "required"):
            self.assertNotIn(key, step, f"{key} 不应写进非普通/高级类型的子步骤")
        self.assertEqual((step["start_x"], step["end_x"]), (500, 100), "拖拽坐标应原样保留")

    def test_detour_dialog_shows_normal_only_fields_for_normal_type(self):
        from PySide6.QtWidgets import QGroupBox

        visibility = []

        def prepare(dialog_self):
            for box in dialog_self.findChildren(QGroupBox):
                if "识别与点击" in box.title():
                    visibility.append(box.isVisibleTo(dialog_self))

        self._drive_detour_dialog(self.window, {"type": "normal"}, prepare=prepare)

        self.assertEqual(visibility, [True])

    def test_mixin_methods_only_read_attributes_both_windows_provide(self):
        """mixin 的方法不得依赖「只存在于某一个窗口」的属性。

        这正是上面那条 AttributeError 的成因，只是因为没人在两个窗口上都点过保存，
        所以只断言字段值的测试抓不到。这里用 AST 取出每个 mixin 方法读到的 self.X，
        再到两个真实窗口实例上查验。

        源码位置**跟着类走**（`CaptureOverlayMixin.__module__`）：mixin 已从
        gui_pyside6.py 外移到 capture_overlay.py，写死文件路径的测试会失效。
        """
        import ast
        import sys

        owner = sys.modules[g.CaptureOverlayMixin.__module__]
        with open(owner.__file__, encoding="utf-8") as source_file:
            tree = ast.parse(source_file.read())
        mixin = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "CaptureOverlayMixin"
        )
        blueprint = self._blueprint_window()
        try:
            offenders = []
            for node in mixin.body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                assigned = set()
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Assign):
                        assigned |= {
                            target.attr for target in inner.targets
                            if isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name) and target.value.id == "self"
                        }
                reads = {
                    inner.attr for inner in ast.walk(node)
                    if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name)
                    and inner.value.id == "self" and isinstance(inner.ctx, ast.Load)
                }
                for name in sorted(reads - assigned):
                    missing = [
                        label for label, owner in (("主窗口", self.window), ("蓝图窗口", blueprint))
                        if not hasattr(owner, name)
                    ]
                    if missing:
                        offenders.append(f"{node.name}() 读了 self.{name}，{'/'.join(missing)}没有")
            self.assertEqual(
                offenders, [],
                "CaptureOverlayMixin 不得依赖只存在于单个窗口的属性：\n  " + "\n  ".join(offenders),
            )
        finally:
            blueprint.close()


if __name__ == "__main__":
    unittest.main()

