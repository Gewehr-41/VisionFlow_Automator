# -*- coding: utf-8 -*-
"""F14 蓝图子图：进入某个组内部（只显示该组及其子组的节点、连线和组框）。

设计边界（重要）：**这是纯视图功能**——步骤仍然按 TASKS 的顺序执行，子图只改变"看哪些节点"，
不改任何数据（`layout_data` 不写入聚焦状态），也不影响引擎。
导航：组右键「进入该组内部（子图）」→ 面包屑「← 返回上级 / 根层」→ Esc（焦点在画布时）。
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import blueprint_groups  # noqa: E402
import gui_pyside6 as g  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(task_id, group_id):
    return {"id": task_id, "type": "normal", "description": task_id, "enabled": True,
            "template": "1", "group_id": group_id}


def make_window(groups, tasks, collapsed=None, layout=None):
    """groups: {组名: 父组}（父组 None 表示根层）；tasks: [(id, group_id), ...]"""
    metadata = {"names": {}, "colors": {}, "parents": {}, "children": {}, "order": []}
    for index, (group_id, parent) in enumerate(groups.items()):
        metadata["names"][group_id] = group_id
        metadata["colors"][group_id] = "#eab308"
        metadata["parents"][group_id] = parent
        metadata["children"].setdefault(group_id, [])
        if parent:
            metadata["children"].setdefault(parent, []).append(group_id)
        else:
            metadata["order"].append(group_id)
    task_list = [_task(task_id, group_id) for task_id, group_id in tasks]
    window = g.BlueprintWindow(task_list, dict(layout or {}), lambda *a, **k: None, metadata, None, {})
    if collapsed:
        window.collapsed_groups = set(collapsed)
    window.resize(1400, 900)
    window.show()
    QApplication.processEvents()
    window.refresh()
    QApplication.processEvents()
    return window


class SubgraphFocusTests(unittest.TestCase):
    def setUp(self):
        # g1（根层）含 A、B 两个步骤；g2 是 g1 的子组，含 C；g3 是另一个根层组，含 D
        self.window = make_window(
            {"g1": None, "g2": "g1", "g3": None},
            [("A", "g1"), ("B", "g1"), ("C", "g2"), ("D", "g3")],
        )

    def tearDown(self):
        self.window.close()

    def _visible(self):
        return {index: item.isVisible() for index, item in self.window._node_items.items()}

    def test_root_shows_everything(self):
        self.assertEqual(self.window.focus_group, None)
        self.assertTrue(all(self._visible().values()))
        self.assertIn("根层", self.window.breadcrumb_label.text())

    def test_enter_group_hides_other_groups(self):
        self.window.enter_group("g1")
        visible = self._visible()
        self.assertTrue(visible[0] and visible[1], "g1 的步骤可见")
        self.assertFalse(visible[3], "其它根层组的步骤应隐藏")

    def test_enter_group_includes_descendant_groups(self):
        self.window.enter_group("g1")
        self.assertTrue(self._visible()[2], "子组 g2 的步骤也属于 g1 子图")

    def test_breadcrumb_and_buttons(self):
        self.window.enter_group("g1")
        self.assertIn("g1", self.window.breadcrumb_label.text())
        self.assertIn("3 步", self.window.breadcrumb_label.text())
        self.assertTrue(self.window.back_button.isEnabled())
        self.assertTrue(self.window.root_button.isEnabled())
        self.window.leave_group()
        self.assertEqual(self.window.focus_group, None)
        self.assertIn("根层", self.window.breadcrumb_label.text())
        self.assertFalse(self.window.back_button.isEnabled())
        self.assertFalse(self.window.root_button.isEnabled())

    def test_nested_leave_walks_up_one_level(self):
        self.window.enter_group("g1")
        self.window.enter_group("g2")
        self.assertIn("g1 › g2", self.window.breadcrumb_label.text())
        self.window.leave_group()
        self.assertEqual(self.window.focus_group, "g1", "返回上级只退一层")
        self.window.leave_group()
        self.assertEqual(self.window.focus_group, None)

    def test_root_button_jumps_to_root(self):
        self.window.enter_group("g1")
        self.window.enter_group("g2")
        self.window.focus_root()
        self.assertEqual(self.window.focus_group, None)

    def test_wires_outside_focus_are_hidden(self):
        before = len(self.window._edges)
        self.window.enter_group("g1")
        after = self.window._edges
        self.assertLess(len(after), before, "跨出子图的连线应隐藏")
        visible_indices = set(self.window._focus_visible_indices())
        for edge in after:
            self.assertIn(edge["source"].index, visible_indices)
            self.assertIn(edge["target"].index, visible_indices)

    def test_group_boxes_outside_focus_are_hidden(self):
        self.window.enter_group("g1")
        visible_groups = {gid for gid, box in self.window._group_items.items() if box.isVisible()}
        self.assertIn("g2", visible_groups, "子组的框仍要显示（作为子图内部的边界）")
        self.assertNotIn("g1", visible_groups, "进入后不再画聚焦组自己的卡片，避免压住内部节点")
        self.assertNotIn("g3", visible_groups, "子图外的组框应隐藏")

    def test_focus_is_view_only_state(self):
        layout_before = dict(self.window.layout_data)
        self.window.enter_group("g1")
        self.window.leave_group()
        self.assertEqual(self.window.layout_data, layout_before, "聚焦状态不应该写进布局数据")
        self.assertNotIn("focus_group", self.window.layout_data)

    def test_collapsed_child_group_still_hidden_inside_focus(self):
        window = make_window(
            {"g1": None, "g2": "g1"},
            [("A", "g1"), ("C", "g2")],
            collapsed={"g2"},
        )
        try:
            window.enter_group("g1")
            visible = {index: item.isVisible() for index, item in window._node_items.items()}
            self.assertTrue(visible[0], "g1 自己的步骤可见")
            self.assertFalse(visible[1], "折叠的子组 g2 里步骤仍隐藏")
        finally:
            window.close()

    def test_escape_action_is_scoped_to_canvas(self):
        action = self.window._leave_focus_action
        self.assertEqual(action.shortcut().toString(), "Esc")
        self.assertEqual(action.shortcutContext(), Qt.ShortcutContext.WidgetShortcut)
        self.window.enter_group("g1")
        action.trigger()
        self.assertEqual(self.window.focus_group, None)


class SubgraphMenuTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window({"g1": None}, [("A", "g1")])

    def tearDown(self):
        self.window.close()

    def test_group_menu_has_enter_action(self):
        created = []

        class FakeMenu(g.QMenu):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

            def exec(self, *args, **kwargs):
                return None

        group_item = self.window._group_items["g1"]
        with mock.patch.object(blueprint_groups, "QMenu", FakeMenu):
            self.window.show_group_context_menu(group_item, QPoint(5, 5))
        texts = [action.text() for action in created[0].actions() if action.text()]
        self.assertIn("进入该组内部（子图）", texts)

    def test_enter_action_disabled_when_already_inside(self):
        created = []

        class FakeMenu(g.QMenu):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

            def exec(self, *args, **kwargs):
                return None

        self.window.enter_group("g1")
        with mock.patch.object(blueprint_groups, "QMenu", FakeMenu):
            self.window.show_group_context_menu(self.window._group_items["g1"], QPoint(5, 5))
        action = next(a for a in created[0].actions() if a.text() == "进入该组内部（子图）")
        self.assertFalse(action.isEnabled())

    def test_deleting_focused_group_returns_to_root(self):
        self.window.enter_group("g1")
        with mock.patch.object(blueprint_groups.QMessageBox, "question",
                               return_value=blueprint_groups.QMessageBox.Yes):
            self.window._delete_group("g1")
        self.assertEqual(self.window.focus_group, None, "聚焦的组被删除后应回到根层")


if __name__ == "__main__":
    unittest.main()
