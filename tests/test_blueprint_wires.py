# -*- coding: utf-8 -*-
"""蓝图连线的批量操作（§8.1 #78）：

  · Ctrl+右键拖动 = 划断，划过几条切几条（Blender 的 Cut Links）；
  · 连线可被框选，框选后可一次性删除；
  · 框选节点时顺带框到的连线不算选中（防止"删步骤"把穿过的线一起删掉）。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsItem  # noqa: E402

import gui_pyside6 as g  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def make_window(count=3):
    tasks = [{"id": f"t{i}", "type": "normal", "description": f"步骤{i}", "enabled": True,
              "template": "1", "click": True, "required": True} for i in range(count)]
    window = g.BlueprintWindow(tasks, {}, lambda *a, **k: None, {}, None, {})
    window.resize(1400, 900)
    return window


def mouse_event(kind, view, scene_point, button=Qt.LeftButton, modifiers=Qt.NoModifier):
    viewport = view.viewport()
    local = QPointF(view.mapFromScene(scene_point))
    return QMouseEvent(kind, local, viewport.mapToGlobal(local.toPoint()), button, button, modifiers)


class WireCutTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()
        self.view = self.window.view
        # 默认布局是三个节点横排：连线都在 y≈100 附近，x 分别落在 270–320 / 550–600
        self.wires = [edge["wire"] for edge in self.window._edges]
        self.assertEqual(len(self.wires), 2, "三个步骤应有两段默认执行流连线")

    def tearDown(self):
        self.window.close()

    def _gesture(self, start, end, button=Qt.RightButton, modifiers=Qt.ControlModifier):
        self.view.mousePressEvent(mouse_event(QEvent.MouseButtonPress, self.view, start, button, modifiers))
        if end is not None:
            self.view.mouseMoveEvent(mouse_event(QEvent.MouseMove, self.view, end, Qt.NoButton, modifiers))
            self.view.mouseReleaseEvent(mouse_event(QEvent.MouseButtonRelease, self.view, end, button, modifiers))
        else:
            self.view.mouseReleaseEvent(mouse_event(QEvent.MouseButtonRelease, self.view, start, button, modifiers))

    def test_ctrl_right_drag_cuts_every_wire_it_crosses(self):
        self._gesture(QPointF(200, 100), QPointF(700, 100))
        self.assertEqual(self.window._edges, [], "两段连线都应被切断")
        for task in self.window.tasks[:2]:
            self.assertIsNone(task.get("flow_next"), "被切断的连线应清空 flow_next")
            self.assertTrue(task.get("flow_next_disabled"),
                            "切断后应标记 flow_next_disabled，避免又自动补一条默认执行流")
        self.assertNotIn("flow_next_disabled", self.window.tasks[2],
                         "最后一步本来就没有出边，不该被改动")

    def test_cut_marks_the_wires_before_release(self):
        self.view.mousePressEvent(mouse_event(QEvent.MouseButtonPress, self.view, QPointF(200, 100),
                                              Qt.RightButton, Qt.ControlModifier))
        self.view.mouseMoveEvent(mouse_event(QEvent.MouseMove, self.view, QPointF(700, 100),
                                             Qt.NoButton, Qt.ControlModifier))
        self.assertTrue(all(wire.cut_preview for wire in self.wires), "划过时应把将被切断的线标红")
        self.assertTrue(self.view.cut_active(), "松手前应处于划断状态")
        self.view.mouseReleaseEvent(mouse_event(QEvent.MouseButtonRelease, self.view, QPointF(700, 100),
                                                Qt.RightButton, Qt.ControlModifier))
        self.assertFalse(self.view.cut_active(), "松手后应结束划断状态")
        self.assertFalse(any(wire.cut_preview for wire in self.wires), "松手后应清掉预览标记")

    def test_cut_path_that_misses_every_wire_changes_nothing(self):
        self._gesture(QPointF(0, 400), QPointF(900, 400))
        self.assertEqual(len(self.window._edges), 2, "没划到任何连线时不该有任何改动")
        for task in self.window.tasks:
            self.assertNotIn("flow_next_disabled", task, "没划到就不该改动任何步骤")

    def test_right_drag_without_ctrl_does_not_cut(self):
        self._gesture(QPointF(200, 100), QPointF(700, 100), modifiers=Qt.NoModifier)
        self.assertEqual(len(self.window._edges), 2, "不按 Ctrl 的右键拖动不该切任何东西")

    def test_tiny_ctrl_right_drag_does_not_cut(self):
        """Ctrl+右键只挪了两像素：不该切线，也不该压掉右键菜单。

        用 2px 而不是 0px：零长度切线本身面积为空，测不出"最短拖动距离"这道保护。
        """
        self._gesture(QPointF(300, 96), QPointF(300, 98))
        self.assertEqual(len(self.window._edges), 2, "2px 的抖动不该切线")
        self.assertFalse(self.view.consume_menu_suppression(), "没切线就不该压掉右键菜单")

    def test_menu_is_suppressed_once_after_a_cut(self):
        self._gesture(QPointF(300, 100), QPointF(320, 100))
        self.assertTrue(self.view.consume_menu_suppression(), "划断后应压掉那一次右键菜单")
        self.assertFalse(self.view.consume_menu_suppression(), "压掉一次后自动恢复")
        self.assertFalse(self.view.cut_active(), "手势结束后不应残留划断状态")


class WireSelectionTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()
        self.view = self.window.view

    def tearDown(self):
        self.window.close()

    def _wires(self):
        return [edge["wire"] for edge in self.window._edges]

    def test_wire_is_selectable(self):
        for wire in self._wires():
            self.assertTrue(wire.flags() & QGraphicsItem.ItemIsSelectable, "连线应可被框选")

    def test_selected_wire_is_highlighted(self):
        wire = self._wires()[0]
        wire.setSelected(True)
        self.window._update_edges()
        self.assertTrue(wire._highlighted, "被框选的连线应该高亮，否则看不出选中了哪些")

    def test_box_selecting_wires_keeps_the_editor_open(self):
        self.window._select_node(1)
        index_before = self.window.current_index
        for wire in self._wires():
            wire.setSelected(True)
        self.assertEqual(self.window.current_index, index_before,
                         "只框选连线时不该把右侧正在编辑的步骤清掉")

    def test_selecting_a_node_deselects_the_wires(self):
        for wire in self._wires():
            wire.setSelected(True)
        self.window._node_items[0].setSelected(True)
        self.assertFalse(any(wire.isSelected() for wire in self._wires()),
                         "框选节点时顺带框到的连线应自动排除（否则删步骤会连坐删线）")

    def test_delete_removes_all_selected_wires_at_once(self):
        for wire in self._wires():
            wire.setSelected(True)
        self.window.delete_selected()
        self.assertEqual(self.window._edges, [], "选中的两段连线应一次删掉")
        self.assertEqual(len(self.window.tasks), 3, "删连线不该删步骤")
        self.assertIsNone(self.window.tasks[0].get("flow_next"))
        self.assertIsNone(self.window.tasks[1].get("flow_next"))


if __name__ == "__main__":
    unittest.main()
