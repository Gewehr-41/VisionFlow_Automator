# -*- coding: utf-8 -*-
"""蓝图：框选多个组后整块拖动（所有者 2026-10-27 反馈）。

原因：视图是 `RubberBandDrag`，而组不是 Qt 的 `ItemIsMovable`——按住组拖动时 Qt 把它当橡皮筋，
**移动过程中 clearSelection()**，多选瞬间只剩按下的那个组，于是只拖动一个。
另外组自己的 `mousePressEvent` 原来会先 `clearSelection()` 再单选自己。

修复：
  · 组已选中时不再清选择（只有点未选中的组才清）；
  · 拖动组期间临时把视图切到 NoDrag（松手恢复），组自己按"选中组的所有成员节点并集"整块移动；
  · 父子组同时选中时按节点去重，避免同一节点被移动两次；
  · 单击多选中的某个组（没拖动）仍把选择收敛成它自己，跟 Qt 默认一致。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsView  # noqa: E402

import gui_pyside6 as g  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(task_id, group_id=None):
    task = {"id": task_id, "type": "normal", "description": task_id, "enabled": True, "template": "1"}
    if group_id:
        task["group_id"] = group_id
        task["group_name"] = group_id
    return task


def make_window(two_groups=True, nested=False):
    if nested:
        metadata = {"names": {"g1": "父组", "g2": "子组"}, "colors": {"g1": "#eab308", "g2": "#38bdf8"},
                    "parents": {"g1": None, "g2": "g1"}, "children": {"g1": ["g2"], "g2": []},
                    "order": ["g1"]}
        tasks = [_task("A", "g1"), _task("B", "g2")]
        positions = {"0": [100, 200], "1": [400, 260]}
    elif two_groups:
        metadata = {"names": {"g1": "组1", "g2": "组2"}, "colors": {}, "parents": {"g1": None, "g2": None},
                    "children": {"g1": [], "g2": []}, "order": ["g1", "g2"]}
        tasks = [_task("A", "g1"), _task("B", "g1"), _task("C", "g2"), _task("D", "g2")]
        positions = {"0": [100, 200], "1": [400, 200], "2": [100, 600], "3": [400, 600]}
    else:
        metadata = {"names": {"g1": "组1"}, "colors": {}, "parents": {"g1": None},
                    "children": {"g1": []}, "order": ["g1"]}
        tasks = [_task("A", "g1"), _task("B", "g1")]
        positions = {"0": [100, 200], "1": [400, 200]}
    window = g.BlueprintWindow(tasks, {"positions": positions}, lambda *a, **k: None,
                               metadata, None, {})
    window.resize(1200, 800)
    window.show()
    QApplication.processEvents()
    return window


def _send(window, kind, scene_point):
    local = QPointF(window.view.mapFromScene(scene_point))
    event = QMouseEvent(kind, local, window.view.viewport().mapToGlobal(local.toPoint()),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    if kind == QEvent.Type.MouseButtonPress:
        window.view.mousePressEvent(event)
    elif kind == QEvent.Type.MouseMove:
        window.view.mouseMoveEvent(event)
    else:
        window.view.mouseReleaseEvent(event)


def drag(window, start_scene, dx, dy, steps=2):
    _send(window, QEvent.Type.MouseButtonPress, start_scene)
    for step in range(1, steps + 1):
        _send(window, QEvent.Type.MouseMove,
              QPointF(start_scene.x() + dx * step / steps, start_scene.y() + dy * step / steps))
    _send(window, QEvent.Type.MouseButtonRelease, QPointF(start_scene.x() + dx, start_scene.y() + dy))
    QApplication.processEvents()


def select_groups(window, *group_ids):
    window.scene.clearSelection()
    for group_id in group_ids:
        window._group_items[group_id].setSelected(True)
    QApplication.processEvents()


def group_topleft(window, group_id):
    rect = window._group_items[group_id].sceneBoundingRect()
    return rect.left(), rect.top()


class CollapsedMultiGroupDragTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()
        self.window.collapsed_groups = {"g1", "g2"}
        self.window.refresh()
        QApplication.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QApplication.processEvents()

    def test_drag_selected_groups_moves_all(self):
        select_groups(self.window, "g1", "g2")
        before = {gid: group_topleft(self.window, gid) for gid in ("g1", "g2")}
        drag(self.window, self.window._group_items["g1"].sceneBoundingRect().center(), 120, 80)
        for gid in ("g1", "g2"):
            after = group_topleft(self.window, gid)
            self.assertAlmostEqual(after[0] - before[gid][0], 120, delta=1.0,
                                   msg=f"{gid} 组应一起水平移动")
            self.assertAlmostEqual(after[1] - before[gid][1], 80, delta=1.0,
                                   msg=f"{gid} 组应一起垂直移动")
        self.assertEqual(len(self.window.scene.selectedItems()), 2, "拖动后应保持两块都选中")

    def test_band_selected_groups_then_drag_keeps_selection(self):
        """真实流程：先框选两个折叠组，再拖其中一个——两块都要动。"""
        c1 = self.window._group_items["g1"].sceneBoundingRect()
        c2 = self.window._group_items["g2"].sceneBoundingRect()
        _send(self.window, QEvent.Type.MouseButtonPress, QPointF(c1.left() - 40, c1.top() - 40))
        _send(self.window, QEvent.Type.MouseMove, QPointF(c2.right() + 40, c2.bottom() + 40))
        _send(self.window, QEvent.Type.MouseButtonRelease, QPointF(c2.right() + 40, c2.bottom() + 40))
        QApplication.processEvents()
        self.assertEqual(len(self.window.scene.selectedItems()), 2)
        before = {gid: group_topleft(self.window, gid) for gid in ("g1", "g2")}
        drag(self.window, self.window._group_items["g1"].sceneBoundingRect().center(), 90, 60)
        for gid in ("g1", "g2"):
            after = group_topleft(self.window, gid)
            self.assertAlmostEqual(after[0] - before[gid][0], 90, delta=1.0)
            self.assertAlmostEqual(after[1] - before[gid][1], 60, delta=1.0)

    def test_drag_unselected_group_moves_only_it(self):
        select_groups(self.window, "g1")
        before2 = group_topleft(self.window, "g2")
        drag(self.window, self.window._group_items["g2"].sceneBoundingRect().center(), 50, 30)
        self.assertEqual([item.group_id for item in self.window.scene.selectedItems()], ["g2"],
                         "拖未选中的组应改为只选中它")
        self.assertAlmostEqual(group_topleft(self.window, "g2")[0] - before2[0], 50, delta=1.0)

    def test_click_without_drag_collapses_selection(self):
        select_groups(self.window, "g1", "g2")
        center = self.window._group_items["g1"].sceneBoundingRect().center()
        _send(self.window, QEvent.Type.MouseButtonPress, center)
        _send(self.window, QEvent.Type.MouseButtonRelease, center)
        QApplication.processEvents()
        selected = [item.group_id for item in self.window.scene.selectedItems()]
        self.assertEqual(selected, ["g1"], "单击多选里的组应收敛成只选中它")

    def test_drag_mode_restored_after_drag(self):
        select_groups(self.window, "g1", "g2")
        drag(self.window, self.window._group_items["g1"].sceneBoundingRect().center(), 40, 20)
        self.assertEqual(self.window.view.dragMode(), QGraphicsView.DragMode.RubberBandDrag,
                         "拖动结束后要恢复橡皮筋框选模式")

    def test_layout_positions_updated_for_all_moved_nodes(self):
        select_groups(self.window, "g1", "g2")
        drag(self.window, self.window._group_items["g1"].sceneBoundingRect().center(), 100, 50)
        positions = self.window.layout_data["positions"]
        for index in range(4):
            self.assertIn(str(index), positions)
        x0, y0 = positions["0"]
        x1, y1 = positions["1"]
        self.assertAlmostEqual(x1 - x0, 300, delta=1.0, msg="块内相对位置应保持不变")


class NestedAndExpandedMultiGroupDragTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "window", None) is not None:
            self.window.close()
            self.window.deleteLater()
            QApplication.processEvents()

    def test_parent_and_child_selected_move_nodes_only_once(self):
        self.window = make_window(two_groups=False, nested=True)
        select_groups(self.window, "g1", "g2")
        before = {index: self.window.layout_data["positions"][str(index)] for index in (0, 1)}
        drag(self.window, self.window._group_items["g1"].sceneBoundingRect().topLeft() + QPointF(30, 6),
             70, 40)
        for index in (0, 1):
            after = self.window.layout_data["positions"][str(index)]
            self.assertAlmostEqual(after[0] - before[index][0], 70, delta=1.0,
                                   msg=f"节点 {index} 只应移动一次（父子组同时选中不能重复移动）")
            self.assertAlmostEqual(after[1] - before[index][1], 40, delta=1.0)

    def test_expanded_groups_move_together(self):
        self.window = make_window()
        select_groups(self.window, "g1", "g2")
        before = {index: self.window.layout_data["positions"][str(index)] for index in range(4)}
        drag(self.window, QPointF(self.window._group_items["g1"].sceneBoundingRect().left() + 40,
                                  self.window._group_items["g1"].sceneBoundingRect().top() + 6), 60, 25)
        for index in range(4):
            after = self.window.layout_data["positions"][str(index)]
            self.assertAlmostEqual(after[0] - before[index][0], 60, delta=1.0)
            self.assertAlmostEqual(after[1] - before[index][1], 25, delta=1.0)


class MultiGroupDragGuardTests(unittest.TestCase):
    def test_group_press_keeps_existing_selection(self):
        source = open("blueprint_canvas.py", encoding="utf-8").read()
        self.assertIn("if scene is not None and not self.isSelected():", source)
        self.assertIn("_restore_view_drag_mode", source)
        self.assertIn("NoDrag", source)

    def test_drag_targets_dedupes_nodes(self):
        source = open("blueprint_canvas.py", encoding="utf-8").read()
        self.assertIn("if id(node) not in seen:", source)
        self.assertIn("def _drag_targets(self):", source)


if __name__ == "__main__":
    unittest.main()
