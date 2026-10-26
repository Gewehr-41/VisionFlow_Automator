# -*- coding: utf-8 -*-
"""F14 第二轮：组在画布上表现为"群组节点"（Blender 式），并修复"组内无法创建"。

背景（所有者 2026-10-27）：第一轮只做"进入组内部看子图"，效果不行；要求像 Blender 群组：
  · 收起后的组 = **一个节点**（卡片 + 输入/输出端口），跨边界连线连到它而不是看不见的内部节点；
  · 双击群组节点进入内部编辑；
  · 组内部可以继续建子组/步骤（此前画布右键固定传 None=根层，建在根层自然看不见）。
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(task_id, group_id):
    return {"id": task_id, "type": "normal", "description": task_id, "enabled": True,
            "template": "1", "group_id": group_id}


def make_window(collapsed=None):
    metadata = {
        "names": {"g1": "战斗流程", "g2": "弹窗处理"},
        "colors": {"g1": "#eab308", "g2": "#38bdf8"},
        "parents": {"g1": None, "g2": "g1"},
        "children": {"g1": ["g2"], "g2": []},
        "order": ["g1"],
    }
    tasks = [_task("A", "g1"), _task("B", "g1"), _task("C", "g2"), _task("D", None)]
    layout = {"positions": {"0": [60, 120], "1": [340, 120], "2": [60, 300], "3": [620, 180]}}
    window = g.BlueprintWindow(tasks, layout, lambda *a, **k: None, metadata, None, {})
    window.resize(1200, 760)
    window.show()
    QApplication.processEvents()
    if collapsed:
        window.collapsed_groups = set(collapsed)
    window.refresh()
    QApplication.processEvents()
    return window


def double_click(window, scene_point):
    view = window.view
    local = view.mapFromScene(scene_point)
    event = QMouseEvent(QEvent.Type.MouseButtonDblClick, local, view.viewport().mapToGlobal(local),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.mouseDoubleClickEvent(event)
    QApplication.processEvents()


class FakeMenu(g.QMenu):
    created = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        FakeMenu.created.append(self)

    def exec(self, *args, **kwargs):
        return None


class GroupNodeRenderTests(unittest.TestCase):
    def tearDown(self):
        for window in getattr(self, "_windows", []):
            window.close()

    def setUp(self):
        self._windows = []
        self.window = make_window(collapsed={"g1"})
        self._windows.append(self.window)
        self.box = self.window._group_items["g1"]

    def test_collapsed_group_is_node_sized(self):
        self.assertTrue(self.box.collapsed)
        self.assertEqual(round(self.box.rect().width()), 230)
        self.assertEqual(round(self.box.rect().height()), 120, "收起后应是一个节点卡片，而不是 18px 细条")

    def test_expanded_group_is_not_node(self):
        window = make_window()
        self._windows.append(window)
        box = window._group_items["g1"]
        self.assertFalse(box.collapsed)
        self.assertNotEqual(round(box.rect().height()), 120)

    def test_cross_boundary_wire_attaches_to_group_ports(self):
        # C 在子组 g2（g1 收起后整体隐藏）里，D 在根层：连线应从群组节点右侧端口出去
        cross = [edge for edge in self.window._edges
                 if edge["target"].index == 3]
        self.assertTrue(cross, "应该存在 C→D 这条跨边界连线")
        start = self.window._node_endpoint(2, "output", "output")
        end = self.window._node_endpoint(3, "input")
        self.assertAlmostEqual(start.x(), self.box.output_port_point().x(), delta=0.5)
        self.assertAlmostEqual(start.y(), self.box.output_port_point().y(), delta=0.5)
        self.assertFalse(self.box.rect().contains(end), "另一端应在外部节点上")

    def test_child_group_box_hidden_under_collapsed_ancestor(self):
        box = self.window._group_items.get("g2")
        self.assertTrue(box is None or not box.isVisible(),
                        "上级组收起成节点时，子组框不该再画出来")

    def test_double_click_collapsed_group_enters_subgraph(self):
        center = self.box.sceneBoundingRect().center()
        double_click(self.window, center)
        self.assertEqual(self.window.focus_group, "g1")

    def test_double_click_expanded_header_still_toggles(self):
        window = make_window()
        self._windows.append(window)
        box = window._group_items["g1"]
        scene_rect = box.sceneBoundingRect()
        from PySide6.QtCore import QPointF
        double_click(window, QPointF(scene_rect.center().x(), scene_rect.top() + 5))
        self.assertIn("g1", window.collapsed_groups, "展开态的组头双击仍是收起/展开（既有行为不变）")
        self.assertIsNone(window.focus_group)

    def test_entered_collapsed_group_shows_inner_nodes(self):
        self.window.enter_group("g1")
        visible = {index: item.isVisible() for index, item in self.window._node_items.items()}
        self.assertTrue(visible[0] and visible[1], "进入收起的组后，组内节点必须可见（否则是空白画布）")
        self.assertFalse(visible[3], "组外节点仍隐藏")

    def test_focused_group_card_is_not_drawn_inside(self):
        self.window.enter_group("g1")
        self.assertFalse(self.window._group_items["g1"].isVisible(),
                         "进入后不该再画自己的群组卡片（会压住内部节点）")


class CreateInsideGroupTests(unittest.TestCase):
    def setUp(self):
        FakeMenu.created = []
        self.window = make_window(collapsed={"g1"})
        self.window.enter_group("g1")

    def tearDown(self):
        self.window.close()

    def _empty_canvas_menu(self):
        from PySide6.QtCore import QPointF
        empty_view_point = self.window.view.mapFromScene(QPointF(-600, -600))
        with mock.patch.object(g, "QMenu", FakeMenu):
            self.window.show_context_menu(empty_view_point)
        self.assertTrue(FakeMenu.created, "空白处右键应弹出菜单")
        actions = {action.text(): action for action in FakeMenu.created[-1].actions() if action.text()}
        self.assertIn("新建步骤", actions, f"空白处菜单内容异常: {sorted(actions)}")
        return actions

    def test_default_group_is_focus_group(self):
        self.assertEqual(self.window._canvas_default_group(), "g1")

    def test_create_subgroup_inside_group(self):
        actions = self._empty_canvas_menu()
        actions["新增组"].trigger()
        parents = self.window.group_metadata["parents"]
        new_ids = [gid for gid in parents if gid not in ("g1", "g2")]
        self.assertEqual(len(new_ids), 1, "应新建一个组")
        self.assertEqual(str(parents[new_ids[0]]), "g1", "新组要挂在当前进入的组下面")

    def test_create_step_inside_group(self):
        actions = self._empty_canvas_menu()
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("delay", True))):
            actions["新建步骤"].trigger()
        created = [task for task in self.window.tasks if task.get("type") == "delay"]
        self.assertEqual(len(created), 1)
        self.assertEqual(str(created[0].get("group_id")), "g1", "新建步骤要落在当前进入的组里")


if __name__ == "__main__":
    unittest.main()
