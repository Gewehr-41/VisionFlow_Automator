"""多识别区域（match_rects）的编辑测试。

背景（两个真实缺陷）
--------------------
1. **区域被静默压缩**：`apply_selected_task` / `_apply_editor` 都写死
   `task["match_rects"] = [rect]`，于是 `normal` / `advanced` 步骤只要点一次
   「应用修改」，多条识别区域就被压成 1 条。而区域**条数是有语义的**：
   `main.match_task_templates` 对 0 条走全屏匹配、1 条走"先试该区域再逐次扩大到
   全屏"、N 条则逐条尝试并取每张模板的最高分（**不走扩容兜底**）。
2. **框第 2 个区域时界面毫无反馈**：区域结果写进控件后又被重载覆盖回第 1 条，
   用户看不到也改不了第 2 条。

现在两个窗口都有「区域选择」下拉 + 共 N 个 + 删除本区域，编辑的永远是当前选中的
那一条，其余区域原样保留。
"""

import copy
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g

R1 = [10, 10, 100, 100]
R2 = [200, 200, 300, 300]
R3 = [400, 400, 500, 500]


def make_task(task_id="t", rects=None):
    rects = copy.deepcopy(rects if rects is not None else [R1, R2, R3])
    return {
        "id": task_id, "type": "normal", "enabled": True, "description": "测试",
        "template": "new_step", "click": True, "required": True,
        "match_rects": rects,
        "match_rect": list(rects[0]) if rects else None,
        "search_rect": list(rects[0]) if rects else None,
    }


class _IsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_presets_file = tasks.PRESETS_FILE
        self._orig_tasks_file = tasks.TASKS_FILE
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)

    def tearDown(self):
        g.TASKS[:] = self._tasks_snapshot
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()


class RegionHelperTests(_IsolatedTestCase):
    """mixin 里的纯逻辑（不需要窗口）。"""

    def setUp(self):
        super().setUp()
        self.mixin = g.CaptureOverlayMixin()

    def test_normalize_region_accepts_lists_and_tuples(self):
        self.assertEqual(self.mixin._normalize_region([1, 2, 3, 4]), (1, 2, 3, 4))
        self.assertEqual(self.mixin._normalize_region((1, 2, 3, 4)), (1, 2, 3, 4))
        self.assertEqual(self.mixin._normalize_region(["1", "2", "3", "4"]), (1, 2, 3, 4))

    def test_normalize_region_accepts_text_forms(self):
        self.assertEqual(self.mixin._normalize_region("1, 2, 3, 4"), (1, 2, 3, 4))
        self.assertEqual(self.mixin._normalize_region("1，2，3，4"), (1, 2, 3, 4))

    def test_normalize_region_keeps_degenerate_rects(self):
        """显示/回写已存数据时不做 right>left 校验，退化矩形也要能显示出来让用户改。"""
        self.assertEqual(self.mixin._normalize_region([500, 10, 100, 100]), (500, 10, 100, 100))

    def test_normalize_region_rejects_junk(self):
        for bad in (None, "", "1, 2, 3", [1, 2, 3], "a, b, c, d", 42):
            with self.subTest(value=bad):
                self.assertIsNone(self.mixin._normalize_region(bad))

    def test_region_rects_of_reads_match_rects(self):
        self.assertEqual(self.mixin._region_rects_of(make_task()), [(10, 10, 100, 100),
                                                                    (200, 200, 300, 300),
                                                                    (400, 400, 500, 500)])

    def test_region_rects_of_falls_back_to_single_rect_fields(self):
        self.assertEqual(self.mixin._region_rects_of({"match_rect": [1, 2, 3, 4]}), [(1, 2, 3, 4)])
        self.assertEqual(self.mixin._region_rects_of({"search_rect": [5, 6, 7, 8]}), [(5, 6, 7, 8)])

    def test_region_rects_of_ignores_empty_and_junk(self):
        self.assertEqual(self.mixin._region_rects_of({}), [])
        self.assertEqual(self.mixin._region_rects_of({"match_rects": []}), [])
        self.assertEqual(self.mixin._region_rects_of({"match_rects": ["垃圾"]}), [])
        self.assertEqual(self.mixin._region_rects_of(None), [])

    def test_store_region_rects_writes_all_three_fields(self):
        task = {}
        self.mixin._store_region_rects(task, [(1, 2, 3, 4), (5, 6, 7, 8)])
        self.assertEqual(task["match_rects"], [(1, 2, 3, 4), (5, 6, 7, 8)])
        self.assertEqual(task["match_rect"], (1, 2, 3, 4))
        self.assertEqual(task["search_rect"], (1, 2, 3, 4))

    def test_store_region_rects_clears_when_empty(self):
        task = {"match_rects": [(1, 2, 3, 4)], "match_rect": (1, 2, 3, 4), "search_rect": (1, 2, 3, 4)}
        self.mixin._store_region_rects(task, [])
        for key in ("match_rects", "match_rect", "search_rect"):
            self.assertNotIn(key, task)


class MainWindowMultiRegionTests(_IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        super().tearDown()

    def _load(self, task):
        g.TASKS[:] = [copy.deepcopy(task)]
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        return g.TASKS[0]

    def _boxes(self):
        return [editor.text() for editor in self.window._region_widgets()[:4]]

    def _selector(self):
        selector = self.window.region_selector
        return (selector.currentIndex(), selector.count(), self.window.region_status_label.text())

    # ---------- 载入与切换 ----------

    def test_loads_first_region_and_shows_the_count(self):
        self._load(make_task())
        self.assertEqual(self._boxes(), ["10", "10", "100", "100"])
        self.assertEqual(self._selector(), (0, 3, "共 3 个"))

    def test_selector_lists_every_region_with_its_coordinates(self):
        self._load(make_task())
        items = [self.window.region_selector.itemText(i) for i in range(3)]
        self.assertEqual(items, ["区域 1（10, 10, 100, 100）",
                                 "区域 2（200, 200, 300, 300）",
                                 "区域 3（400, 400, 500, 500）"])

    def test_switching_the_selector_loads_that_region(self):
        self._load(make_task())
        self.window.region_selector.setCurrentIndex(2)
        self.assertEqual(self._boxes(), ["400", "400", "500", "500"])

    def test_no_region_disables_the_selector(self):
        task = {"id": "t", "type": "normal", "enabled": True, "description": "无区域",
                "template": "new_step", "click": True, "required": True}
        self._load(task)
        self.assertEqual(self._boxes(), ["", "", "", ""])
        self.assertEqual(self._selector(), (-1, 0, "未设置"))
        self.assertFalse(self.window.region_selector.isEnabled())
        self.assertFalse(self.window.region_delete_button.isEnabled())

    # ---------- 缺陷 1：不再压缩 ----------

    def test_apply_updates_only_the_selected_region(self):
        task = self._load(make_task())
        self.window.region_selector.setCurrentIndex(2)
        self.window.region_left_edit.setText("401")
        self.window.apply_selected_task()

        self.assertEqual(task["match_rects"], [(10, 10, 100, 100),
                                               (200, 200, 300, 300),
                                               (401, 400, 500, 500)])
        self.assertEqual(task["search_rect"], (10, 10, 100, 100),
                         "search_rect 应始终等于第 1 条")

    def test_apply_keeps_the_other_regions_when_editing_the_first(self):
        task = self._load(make_task())
        self.window.region_left_edit.setText("11")
        self.window.apply_selected_task()
        self.assertEqual(len(task["match_rects"]), 3)
        self.assertEqual(task["match_rects"][0], (11, 10, 100, 100))

    def test_apply_with_all_boxes_empty_removes_only_the_selected_region(self):
        task = self._load(make_task())
        self.window.region_selector.setCurrentIndex(1)
        for editor in self.window._region_widgets():
            editor.clear()
        self.window.apply_selected_task()

        self.assertEqual(task["match_rects"], [(10, 10, 100, 100), (400, 400, 500, 500)])

    def test_apply_with_all_boxes_empty_on_the_last_region_keeps_the_others(self):
        task = self._load(make_task())
        self.window.region_selector.setCurrentIndex(2)
        for editor in self.window._region_widgets():
            editor.clear()
        self.window.apply_selected_task()
        self.assertEqual(task["match_rects"], [(10, 10, 100, 100), (200, 200, 300, 300)])

    # ---------- 打字打错不该丢区域 ----------

    def test_apply_with_partially_filled_boxes_changes_nothing(self):
        task = self._load(make_task())
        before = list(task["match_rects"])
        self.window.region_selector.setCurrentIndex(1)
        self.window.region_right_edit.clear()
        self.window.region_bottom_edit.clear()
        self.window.apply_selected_task()

        self.assertEqual(task["match_rects"], before, "填一半不该删掉区域")
        self.assertIn("未修改该区域", self.window.log_box.toPlainText())

    def test_apply_with_inverted_rect_changes_nothing(self):
        task = self._load(make_task())
        before = list(task["match_rects"])
        self.window.region_selector.setCurrentIndex(1)
        self.window.region_left_edit.setText("299")
        self.window.region_right_edit.setText("201")
        self.window.apply_selected_task()

        self.assertEqual(task["match_rects"], before, "右下小于左上不该删掉区域")
        self.assertIn("未修改该区域", self.window.log_box.toPlainText())

    # ---------- 删除按钮 ----------

    def test_delete_button_removes_only_the_selected_region(self):
        task = self._load(make_task())
        self.window.region_selector.setCurrentIndex(1)
        self.window.delete_current_region()

        self.assertEqual(task["match_rects"], [(10, 10, 100, 100), (400, 400, 500, 500)])
        self.assertEqual(self._selector(), (1, 2, "共 2 个"))

    def test_delete_button_clamps_the_index_at_the_end(self):
        task = self._load(make_task())
        self.window.region_selector.setCurrentIndex(2)
        self.window.delete_current_region()
        self.assertEqual(self._selector(), (1, 2, "共 2 个"))

    def test_deleting_the_last_region_clears_the_fields(self):
        task = self._load(make_task(rects=[R1]))
        self.window.delete_current_region()
        for key in ("match_rects", "match_rect", "search_rect"):
            self.assertNotIn(key, task)
        self.assertEqual(self._selector(), (-1, 0, "未设置"))

    # ---------- 缺陷 2：框选后有反馈 ----------

    def test_framing_a_second_region_selects_and_shows_it(self):
        task = self._load(make_task(rects=[R1]))
        self.window._capture_target = "match"
        self.window.finish_region_capture(700, 700, 800, 800)

        self.assertEqual(len(task["match_rects"]), 2)
        self.assertEqual(task["match_rects"][1], (700, 700, 800, 800))
        self.assertEqual(self._boxes(), ["700", "700", "800", "800"],
                         "刚框好的区域必须显示出来，否则用户以为没框上")
        self.assertEqual(self._selector(), (1, 2, "共 2 个"))

    def test_framing_keeps_the_earlier_regions(self):
        task = self._load(make_task())
        self.window._capture_target = "match"
        self.window.finish_region_capture(700, 700, 800, 800)
        self.assertEqual(task["match_rects"][:3], [(10, 10, 100, 100),
                                                   (200, 200, 300, 300),
                                                   (400, 400, 500, 500)])

    # ---------- 区域下标的生命周期 ----------

    def test_switching_steps_resets_the_selected_region(self):
        g.TASKS[:] = [make_task("a"), make_task("b")]
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        self.window.region_selector.setCurrentIndex(2)
        self.window.select_task_index(1)

        self.assertEqual(self.window.current_task_index, 1)
        self.assertEqual(self.window.region_selector.currentIndex(), 0)

    def test_reloading_the_same_step_keeps_the_selected_region(self):
        """刷新同一步骤不能把选中项弹回第 1 条，否则框选新区域后会被立刻覆盖。"""
        self._load(make_task())
        self.window.region_selector.setCurrentIndex(2)
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        self.assertEqual(self.window.region_selector.currentIndex(), 2)


class BlueprintMultiRegionTests(_IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.window = g.BlueprintWindow([make_task()], {}, lambda *a, **k: None, {}, None, {})

    def tearDown(self):
        self.window.close()
        super().tearDown()

    def _load(self):
        self.window._select_node(0)
        return self.window.tasks[0]

    def test_loads_first_region_and_shows_the_count(self):
        self._load()
        self.assertEqual(self.window.match_rect_edit.text(), "10, 10, 100, 100")
        self.assertEqual(self.window.region_selector.count(), 3)
        self.assertEqual(self.window.region_status_label.text(), "共 3 个")

    def test_switching_the_selector_loads_that_region(self):
        self._load()
        self.window.region_selector.setCurrentIndex(1)
        self.assertEqual(self.window.match_rect_edit.text(), "200, 200, 300, 300")

    def test_apply_updates_only_the_selected_region(self):
        task = self._load()
        self.window.region_selector.setCurrentIndex(1)
        self.window.match_rect_edit.setText("201, 201, 301, 301")
        self.window._apply_editor()

        self.assertEqual(task["match_rects"], [(10, 10, 100, 100),
                                               (201, 201, 301, 301),
                                               (400, 400, 500, 500)])

    def test_apply_with_empty_text_removes_only_the_selected_region(self):
        task = self._load()
        self.window.region_selector.setCurrentIndex(1)
        self.window.match_rect_edit.clear()
        self.window._apply_editor()

        self.assertEqual(task["match_rects"], [(10, 10, 100, 100), (400, 400, 500, 500)])

    def test_apply_with_junk_text_changes_nothing(self):
        task = self._load()
        before = list(task["match_rects"])
        self.window.region_selector.setCurrentIndex(1)
        self.window.match_rect_edit.setText("这不是矩形")
        self.window._apply_editor()
        self.assertEqual(task["match_rects"], before)

    def test_framing_selects_the_new_region(self):
        task = self._load()
        self.window._capture_target = "match"
        self.window.finish_region_capture(700, 700, 800, 800)
        self.assertEqual(self.window.match_rect_edit.text(), "700, 700, 800, 800")
        self.assertEqual(self.window.region_selector.currentIndex(), 3)

    def test_delete_button_removes_only_the_selected_region(self):
        task = self._load()
        self.window.region_selector.setCurrentIndex(0)
        self.window.delete_current_region()
        self.assertEqual(task["match_rects"], [(200, 200, 300, 300), (400, 400, 500, 500)])
        self.assertEqual(self.window.match_rect_edit.text(), "200, 200, 300, 300")

    # ---------- 采集后不该清空编辑面板（既有 bug） ----------

    def test_region_capture_keeps_the_editor_panel(self):
        """曾经的 bug：`refresh()` 会重建场景、丢掉选中项，进而把 `current_index`
        置为 -1；而 `_capture_after_task_changed()` 是在 refresh **之后**才读
        `self.current_index` 去重载面板，于是拿到 -1 → 整个右侧面板被清空。"""
        self._load()
        self.window._capture_target = "match"
        self.window.finish_region_capture(700, 700, 800, 800)

        self.assertEqual(self.window.current_index, 0, "采集后不该丢掉当前步骤")
        self.assertEqual(self.window.match_rect_edit.text(), "700, 700, 800, 800")

    def test_click_capture_keeps_the_editor_panel(self):
        self._load()
        original = self.window.match_rect_edit.text()
        self.window.finish_click_capture(123, 456)
        self.assertEqual(self.window.current_index, 0)
        self.assertEqual(self.window.match_rect_edit.text(), original,
                         "采集点击点不该把识别区域输入框清空")

    def test_next_template_region_capture_keeps_the_editor_panel(self):
        self._load()
        original = self.window.match_rect_edit.text()
        self.window._capture_target = "next"
        self.window.finish_region_capture(1, 2, 3, 4)
        self.assertEqual(self.window.current_index, 0)
        self.assertEqual(self.window.match_rect_edit.text(), original)


class RegionGroupVisibilityTests(_IsolatedTestCase):
    """「识别区域」组对哪些步骤类型可见。

    背景：区域控件此前挂在 `recognition_group` 里，而那个组对 `click_until_gone`
    是隐藏的 —— 可引擎偏偏为这类步骤使用识别区域
    （`execute_click_until_gone_task` → `match_task_templates` → `resolve_search_rects`）。
    表现为"配置生效、界面看不见也改不了"，而所有者真实数据里唯一的多区域步骤
    （`追放主线关卡` 第 6 步「跳过剧情」）正是这个类型。
    """

    VISIBLE_TYPES = ("normal", "advanced", "click_until_gone")
    HIDDEN_TYPES = ("key_press", "keyboard_move", "drag", "delay")

    def setUp(self):
        super().setUp()
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        super().tearDown()

    def _load_main(self, task_type):
        g.TASKS[:] = [make_task(task_id="t", rects=[R1, R2])]
        g.TASKS[0]["type"] = task_type
        self.window.refresh_task_list()
        self.window.select_task_index(0)

    def _blueprint_for(self, task_type):
        task = make_task(task_id="t", rects=[R1, R2])
        task["type"] = task_type
        window = g.BlueprintWindow([task], {}, lambda *a, **k: None, {}, None, {})
        window._select_node(0)
        return window

    def test_main_window_region_group_visible_for_the_expected_types(self):
        for task_type in self.VISIBLE_TYPES:
            with self.subTest(task_type=task_type):
                self._load_main(task_type)
                self.assertTrue(self.window.region_group.isVisibleTo(self.window),
                                f"{task_type} 步骤应能看到识别区域")
                self.assertEqual(self.window.region_selector.count(), 2)

    def test_main_window_region_group_hidden_for_other_types(self):
        for task_type in self.HIDDEN_TYPES:
            with self.subTest(task_type=task_type):
                self._load_main(task_type)
                self.assertFalse(self.window.region_group.isVisibleTo(self.window),
                                 f"{task_type} 步骤不该显示识别区域")

    def test_blueprint_region_group_visible_for_the_expected_types(self):
        for task_type in self.VISIBLE_TYPES:
            with self.subTest(task_type=task_type):
                window = self._blueprint_for(task_type)
                try:
                    self.assertTrue(window.region_group.isVisibleTo(window),
                                    f"{task_type} 步骤应能看到识别区域")
                finally:
                    window.close()

    def test_blueprint_region_group_hidden_for_other_types(self):
        for task_type in self.HIDDEN_TYPES:
            with self.subTest(task_type=task_type):
                window = self._blueprint_for(task_type)
                try:
                    self.assertFalse(window.region_group.isVisibleTo(window),
                                     f"{task_type} 步骤不该显示识别区域")
                finally:
                    window.close()

    def test_click_until_gone_still_hides_the_recognition_group(self):
        """区域组独立出来之后，识别设置组对 click_until_gone 仍应保持隐藏。"""
        self._load_main("click_until_gone")
        self.assertFalse(self.window.recognition_group.isVisibleTo(self.window))
        self.assertTrue(self.window.region_group.isVisibleTo(self.window))

    def test_clear_editor_hides_the_region_group(self):
        self._load_main("normal")
        # 主窗口叫 clear_editor，蓝图窗口叫 _clear_editor
        clearer = getattr(self.window, "clear_editor", None) or self.window._clear_editor
        clearer()
        self.assertFalse(self.window.region_group.isVisibleTo(self.window))

    def test_selecting_a_group_hides_the_region_group_then_step_shows_it_again(self):
        from PySide6.QtCore import Qt

        task = make_task(task_id="t")
        task["group_id"] = "gr"
        g.TASKS[:] = [task]
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[mode] = {
            "names": {"gr": "外层组"}, "colors": {"gr": "#eab308"}, "expanded": {},
            "order": ["gr"], "parents": {"gr": None}, "children": {"gr": []},
        }
        self.window.refresh_task_list()
        group_item = self.window.task_list.topLevelItem(0)
        step_item = group_item.child(0)
        self.assertEqual(group_item.data(0, Qt.UserRole), "group")

        self.window.task_list.setCurrentItem(step_item)
        self.assertTrue(self.window.region_group.isVisibleTo(self.window))
        self.window.task_list.setCurrentItem(group_item)
        self.assertFalse(self.window.region_group.isVisibleTo(self.window),
                         "选中分组时应隐藏识别区域")
        self.window.task_list.setCurrentItem(step_item)
        self.assertTrue(self.window.region_group.isVisibleTo(self.window),
                        "选回步骤后识别区域应重新显示")


class ClickUntilGoneRegionTests(_IsolatedTestCase):
    """`click_until_gone` 步骤的识别区域现在既能看见、也能改。"""

    def setUp(self):
        super().setUp()
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        super().tearDown()

    def _load(self):
        task = make_task(task_id="t")
        task["type"] = "click_until_gone"
        g.TASKS[:] = [task]
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        return g.TASKS[0]

    def test_regions_are_listed_and_switchable(self):
        self._load()
        self.assertEqual(self.window.region_selector.count(), 3)
        self.window.region_selector.setCurrentIndex(1)
        self.assertEqual([e.text() for e in self.window._region_widgets()[:4]],
                         ["200", "200", "300", "300"])

    def test_apply_updates_only_the_selected_region(self):
        task = self._load()
        self.window.region_selector.setCurrentIndex(1)
        self.window.region_left_edit.setText("201")
        self.window.apply_selected_task()

        self.assertEqual(task["match_rects"], [(10, 10, 100, 100),
                                               (201, 200, 300, 300),
                                               (400, 400, 500, 500)],
                         "持续点击步骤的「应用修改」也应回写识别区域，且不压缩其余区域")

    def test_apply_with_all_boxes_empty_removes_only_the_selected_region(self):
        task = self._load()
        self.window.region_selector.setCurrentIndex(0)
        for editor in self.window._region_widgets():
            editor.clear()
        self.window.apply_selected_task()
        self.assertEqual(task["match_rects"], [(200, 200, 300, 300), (400, 400, 500, 500)])

    def test_delete_button_works_for_this_type(self):
        task = self._load()
        self.window.region_selector.setCurrentIndex(2)
        self.window.delete_current_region()
        self.assertEqual(task["match_rects"], [(10, 10, 100, 100), (200, 200, 300, 300)])

    def test_blueprint_apply_updates_only_the_selected_region(self):
        task = make_task(task_id="t")
        task["type"] = "click_until_gone"
        window = g.BlueprintWindow([task], {}, lambda *a, **k: None, {}, None, {})
        try:
            window._select_node(0)
            self.assertTrue(window.region_group.isVisibleTo(window))
            window.region_selector.setCurrentIndex(2)
            window.match_rect_edit.setText("401, 401, 501, 501")
            window._apply_editor()
        finally:
            window.close()

        self.assertEqual(task["match_rects"], [(10, 10, 100, 100),
                                               (200, 200, 300, 300),
                                               (401, 401, 501, 501)])


if __name__ == "__main__":
    unittest.main()
