"""多识别区域（match_rects）的测试。

数据模型
--------
一个步骤可以配置**多条**识别区域，引擎按条数走不同策略
（`main.match_task_templates`）：0 条走全屏匹配；1 条先在该区域内找、未命中再逐次
扩大到全屏；N 条则逐条尝试并取每张模板的最高分（不走扩容兜底）。所以条数是有
语义的，不能被静默改变。

界面约定（2026-09-28 起）
-------------------------
区域**只能靠「框选识别区域」产生**，界面不再提供手工输入坐标的控件。窗口里只有：
  - 「区域选择」下拉：列出每条区域及其坐标，可切换当前条
  - 「共 N 个」状态标签
  - 「删除本区域」按钮：删掉当前选中那条，其余保留
「清空识别区域」按钮则清掉全部。因为框选/删除都直接写任务并立即落盘，
「应用修改」**完全不碰**区域字段。
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

NO_COORDINATE_ATTRS = (
    "match_rect_edit",
    "region_left_edit", "region_top_edit",
    "region_right_edit", "region_bottom_edit",
    "region_center_x_edit", "region_center_y_edit",
)


def make_task(task_id="t", rects=None, task_type="normal"):
    rects = copy.deepcopy(rects if rects is not None else [R1, R2, R3])
    return {
        "id": task_id, "type": task_type, "enabled": True, "description": "测试",
        "template": "new_step", "click": True, "required": True,
        "match_rects": rects,
        "match_rect": list(rects[0]) if rects else None,
        "search_rect": list(rects[0]) if rects else None,
    }


def selector_state(window):
    """返回 (当前下标, 条目数, 状态文本)。"""
    return (window.region_selector.currentIndex(),
            window.region_selector.count(),
            window.region_status_label.text())


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

    def test_normalize_region_accepts_bracket_wrapped_text(self):
        """界面曾把矩形按 Python 列表的 repr 显示成 '[30, 28, 89, 81]'。

        这类文本（以及用户从 JSON 里粘回来的同类内容）必须仍能解析：解析不了就会
        被当成"清空识别区域"，把匹配范围从某个区域悄悄放大成全屏。
        """
        self.assertEqual(self.mixin._normalize_region("[30, 28, 89, 81]"), (30, 28, 89, 81))
        self.assertEqual(self.mixin._normalize_region("(30, 28, 89, 81)"), (30, 28, 89, 81))
        self.assertEqual(self.mixin._normalize_region(" [30，28，89，81] "), (30, 28, 89, 81))

    def test_normalize_region_keeps_degenerate_rects(self):
        """列出已存数据时不做 right>left 校验，退化矩形也要能列出来让用户删掉。"""
        self.assertEqual(self.mixin._normalize_region([500, 10, 100, 100]), (500, 10, 100, 100))

    def test_normalize_region_rejects_junk(self):
        for bad in (None, "", "1, 2, 3", [1, 2, 3], "a, b, c, d", 42):
            with self.subTest(value=bad):
                self.assertIsNone(self.mixin._normalize_region(bad))

    def test_region_rects_of_reads_match_rects(self):
        self.assertEqual(self.mixin._region_rects_of(make_task()),
                         [(10, 10, 100, 100), (200, 200, 300, 300), (400, 400, 500, 500)])

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


class MainWindowRegionListTests(_IsolatedTestCase):
    """区域列表的展示 / 切换 / 删除（主窗口）。"""

    def setUp(self):
        super().setUp()
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        super().tearDown()

    def _load(self, task=None):
        g.TASKS[:] = [copy.deepcopy(task if task is not None else make_task())]
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        return g.TASKS[0]

    def test_loads_the_region_list_and_shows_the_count(self):
        self._load()
        self.assertEqual(selector_state(self.window), (0, 3, "共 3 个"))

    def test_selector_lists_every_region_with_its_coordinates(self):
        self._load()
        items = [self.window.region_selector.itemText(i) for i in range(3)]
        self.assertEqual(items, ["区域 1（10, 10, 100, 100）",
                                 "区域 2（200, 200, 300, 300）",
                                 "区域 3（400, 400, 500, 500）"])

    def test_switching_the_selector_updates_the_current_region(self):
        self._load()
        self.window.region_selector.setCurrentIndex(2)
        self.assertEqual(self.window._region_index, 2)

    def test_no_region_disables_the_selector_and_delete_button(self):
        self._load({"id": "t", "type": "normal", "enabled": True, "description": "无区域",
                    "template": "new_step", "click": True, "required": True})
        self.assertEqual(selector_state(self.window), (-1, 0, "未设置"))
        self.assertFalse(self.window.region_selector.isEnabled())
        self.assertFalse(self.window.region_delete_button.isEnabled())

    def test_delete_button_removes_only_the_selected_region(self):
        task = self._load()
        self.window.region_selector.setCurrentIndex(1)
        self.window.delete_current_region()

        self.assertEqual(task["match_rects"], [(10, 10, 100, 100), (400, 400, 500, 500)])
        self.assertEqual(selector_state(self.window), (1, 2, "共 2 个"))

    def test_delete_button_clamps_the_index_at_the_end(self):
        self._load()
        self.window.region_selector.setCurrentIndex(2)
        self.window.delete_current_region()
        self.assertEqual(selector_state(self.window), (1, 2, "共 2 个"))

    def test_deleting_the_last_region_clears_the_fields(self):
        task = self._load(make_task(rects=[R1]))
        self.window.delete_current_region()
        for key in ("match_rects", "match_rect", "search_rect"):
            self.assertNotIn(key, task)
        self.assertEqual(selector_state(self.window), (-1, 0, "未设置"))

    def test_clear_match_region_removes_everything(self):
        task = self._load()
        self.window.clear_match_region()
        for key in ("match_rects", "match_rect", "search_rect"):
            self.assertNotIn(key, task)
        self.assertEqual(selector_state(self.window), (-1, 0, "未设置"))

    def test_apply_does_not_change_the_regions(self):
        """「应用修改」不该碰区域字段（区域只由框选/删除维护）。"""
        task = self._load()
        before = copy.deepcopy(task["match_rects"])
        self.window.region_selector.setCurrentIndex(1)
        self.window.description_edit.setText("改个描述")
        self.window.apply_selected_task()

        self.assertEqual(task["match_rects"], before)
        self.assertEqual(task["description"], "改个描述")


class FramingFeedbackTests(_IsolatedTestCase):
    """框选识别区域后必须看得出"多了一条"，并且新那条成为当前选中项。"""

    def setUp(self):
        super().setUp()
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        super().tearDown()

    def _load(self, rects):
        g.TASKS[:] = [make_task(rects=rects)]
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        return g.TASKS[0]

    def _frame(self, rect):
        self.window._capture_target = "match"
        self.window.finish_region_capture(*rect)

    def test_framing_a_region_adds_it_and_selects_it(self):
        task = self._load([R1])
        self._frame((700, 700, 800, 800))

        self.assertEqual(task["match_rects"], [(10, 10, 100, 100), (700, 700, 800, 800)])
        self.assertEqual(selector_state(self.window), (1, 2, "共 2 个"))

    def test_framing_a_second_region_is_visible_in_the_list(self):
        """此前第 2 条框完后界面看不出任何变化，像没框上。"""
        self._load([R1])
        self._frame((700, 700, 800, 800))

        items = [self.window.region_selector.itemText(i) for i in range(2)]
        self.assertIn("区域 2（700, 700, 800, 800）", items)
        self.assertEqual(self.window.region_selector.currentIndex(), 1)

    def test_framing_keeps_the_earlier_regions(self):
        task = self._load([R1, R2, R3])
        self._frame((700, 700, 800, 800))
        self.assertEqual(task["match_rects"][:3], [(10, 10, 100, 100),
                                                   (200, 200, 300, 300),
                                                   (400, 400, 500, 500)])

    def test_the_newly_framed_region_is_the_one_deleted(self):
        task = self._load([R1])
        self._frame((700, 700, 800, 800))
        self.window.delete_current_region()
        self.assertEqual(task["match_rects"], [(10, 10, 100, 100)])


class RegionIndexLifecycleTests(_IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        super().tearDown()

    def test_switching_steps_resets_the_selected_region(self):
        g.TASKS[:] = [make_task("a"), make_task("b")]
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        self.window.region_selector.setCurrentIndex(2)
        self.window.select_task_index(1)

        self.assertEqual(self.window.current_task_index, 1)
        self.assertEqual(self.window.region_selector.currentIndex(), 0)

    def test_reloading_the_same_step_keeps_the_selected_region(self):
        """刷新同一步骤不能把选中项弹回第 1 条，否则刚框的那条会立刻"消失"。"""
        g.TASKS[:] = [make_task()]
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        self.window.region_selector.setCurrentIndex(2)
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        self.assertEqual(self.window.region_selector.currentIndex(), 2)


class BlueprintRegionListTests(_IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.window = g.BlueprintWindow([make_task()], {}, lambda *a, **k: None, {}, None, {})

    def tearDown(self):
        self.window.close()
        super().tearDown()

    def _load(self, rects=None):
        self.window.tasks = [make_task(rects=rects)]
        self.window._select_node(0)
        return self.window.tasks[0]

    def test_loads_the_region_list_and_shows_the_count(self):
        self._load()
        self.assertEqual(selector_state(self.window), (0, 3, "共 3 个"))

    def test_switching_the_selector_updates_the_current_region(self):
        self._load()
        self.window.region_selector.setCurrentIndex(1)
        self.assertEqual(self.window._region_index, 1)

    def test_delete_button_removes_only_the_selected_region(self):
        task = self._load()
        self.window.region_selector.setCurrentIndex(0)
        self.window.delete_current_region()
        self.assertEqual(task["match_rects"], [(200, 200, 300, 300), (400, 400, 500, 500)])
        self.assertEqual(selector_state(self.window), (0, 2, "共 2 个"))

    def test_framing_selects_the_new_region(self):
        task = self._load([R1])
        self.window._capture_target = "match"
        self.window.finish_region_capture(700, 700, 800, 800)
        self.assertEqual(selector_state(self.window), (1, 2, "共 2 个"))
        self.assertEqual(task["match_rects"][1], (700, 700, 800, 800))

    def test_apply_does_not_change_the_regions(self):
        task = self._load()
        before = copy.deepcopy(task["match_rects"])
        self.window.description_edit.setText("改个描述")
        self.window._apply_editor()
        self.assertEqual(task["match_rects"], before)

    # ---------- 采集后不该清空编辑面板（既有 bug） ----------

    def test_region_capture_keeps_the_editor_panel(self):
        """曾经的 bug：`refresh()` 会把 `current_index` 置为 -1（场景重建、选中项
        丢失 → `_refresh_editor_from_selection()` 清空），而
        `_capture_after_task_changed()` 是在 refresh **之后**才读它，于是面板被清空。"""
        self._load()
        self.window._capture_target = "match"
        self.window.finish_region_capture(700, 700, 800, 800)

        self.assertEqual(self.window.current_index, 0, "采集后不该丢掉当前步骤")
        self.assertEqual(self.window.region_selector.count(), 4)

    def test_click_capture_keeps_the_editor_panel(self):
        self._load()
        before = self.window.region_selector.count()
        self.window.finish_click_capture(123, 456)
        self.assertEqual(self.window.current_index, 0)
        self.assertEqual(self.window.region_selector.count(), before)

    def test_next_template_region_capture_keeps_the_editor_panel(self):
        self._load()
        before = self.window.region_selector.count()
        self.window._capture_target = "next"
        self.window.finish_region_capture(1, 2, 3, 4)
        self.assertEqual(self.window.current_index, 0)
        self.assertEqual(self.window.region_selector.count(), before,
                         "框选「下一模板出现位置」不该增加识别区域")


class RegionGroupVisibilityTests(_IsolatedTestCase):
    """「识别区域」组对哪些步骤类型可见。

    背景：区域控件此前挂在 `recognition_group` 里，而那个组对 `click_until_gone`
    是隐藏的 —— 可引擎偏偏为这类步骤使用识别区域
    （`execute_click_until_gone_task` → `match_task_templates` → `resolve_search_rects`）。
    所有者真实数据里唯一的多区域步骤（追放主线关卡 第 6 步「跳过剧情」）正是这个类型。
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
        g.TASKS[:] = [make_task(task_type=task_type, rects=[R1, R2])]
        self.window.refresh_task_list()
        self.window.select_task_index(0)

    def _blueprint_for(self, task_type):
        window = g.BlueprintWindow([make_task(task_type=task_type, rects=[R1, R2])], {},
                                   lambda *a, **k: None, {}, None, {})
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

        task = make_task()
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
    """`click_until_gone` 步骤的识别区域现在也能看见、能管。"""

    def setUp(self):
        super().setUp()
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        super().tearDown()

    def _load(self):
        g.TASKS[:] = [make_task(task_type="click_until_gone")]
        self.window.refresh_task_list()
        self.window.select_task_index(0)
        return g.TASKS[0]

    def test_regions_are_listed_and_switchable(self):
        self._load()
        self.assertEqual(selector_state(self.window), (0, 3, "共 3 个"))
        self.window.region_selector.setCurrentIndex(1)
        self.assertEqual(self.window._region_index, 1)

    def test_delete_button_works_for_this_type(self):
        task = self._load()
        self.window.region_selector.setCurrentIndex(2)
        self.window.delete_current_region()
        self.assertEqual(task["match_rects"], [(10, 10, 100, 100), (200, 200, 300, 300)])

    def test_framing_works_for_this_type(self):
        task = self._load()
        self.window._capture_target = "match"
        self.window.finish_region_capture(900, 900, 950, 950)
        self.assertEqual(task["match_rects"][-1], (900, 900, 950, 950))
        self.assertEqual(selector_state(self.window), (3, 4, "共 4 个"))

    def test_blueprint_lists_regions_for_this_type(self):
        task = make_task(task_type="click_until_gone")
        window = g.BlueprintWindow([task], {}, lambda *a, **k: None, {}, None, {})
        try:
            window._select_node(0)
            self.assertTrue(window.region_group.isVisibleTo(window))
            self.assertEqual(window.region_selector.count(), 3)
            window.region_selector.setCurrentIndex(2)
            window.delete_current_region()
            self.assertEqual(task["match_rects"], [(10, 10, 100, 100), (200, 200, 300, 300)])
        finally:
            window.close()


class NoCoordinateInputTests(_IsolatedTestCase):
    """两个窗口都不该再有区域坐标输入控件（区域只能靠框选产生）。"""

    def setUp(self):
        super().setUp()
        self.window = g.PySide6ScriptWindow()
        self.blueprint = g.BlueprintWindow([make_task()], {}, lambda *a, **k: None, {}, None, {})

    def tearDown(self):
        self.window.close()
        self.blueprint.close()
        super().tearDown()

    def test_no_coordinate_widgets(self):
        for name, window in (("主窗口", self.window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                for attr in NO_COORDINATE_ATTRS:
                    self.assertFalse(hasattr(window, attr), f"{name} 仍存在 {attr}")

    def test_no_region_editing_helpers(self):
        for name, window in (("主窗口", self.window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                for hook in ("_capture_show_region_widgets",
                             "_capture_clear_region_widgets",
                             "_capture_apply_current_region",
                             "_current_match_rect",
                             "_region_widgets",
                             "_capture_region_entry_state",
                             "_capture_region_entry_is_blank"):
                    self.assertFalse(hasattr(window, hook), f"{name} 仍有 {hook}")

    def test_selector_and_delete_are_still_there(self):
        for name, window in (("主窗口", self.window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                self.assertTrue(hasattr(window, "region_selector"))
                self.assertTrue(hasattr(window, "region_delete_button"))
                self.assertTrue(callable(window.delete_current_region))


if __name__ == "__main__":
    unittest.main()
