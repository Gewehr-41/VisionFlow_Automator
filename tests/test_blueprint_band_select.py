# -*- coding: utf-8 -*-
"""蓝图：框选能选中组（所有者 2026-10-27 反馈"组无法框选"）。

原因：展开态组框的 `shape()` 只覆盖顶部 18px 标题栏（内容区透明、避免挡住节点），
Qt 的橡皮筋框选按 shape 判定 → 只框住组体、或在组内空白处拖框时，组永远选不中。

修复：框选结束（从空白处起框、非 Ctrl 只选线模式）后，按组框的 `sceneBoundingRect`
与选择框相交补选组；Ctrl 框选行为保持不变。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
from blueprint_canvas import BlueprintGroupItem  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(task_id, group_id=None):
    task = {"id": task_id, "type": "normal", "description": task_id, "enabled": True, "template": "1"}
    if group_id:
        task["group_id"] = group_id
        task["group_name"] = group_id
    return task


def make_window():
    metadata = {"names": {"g1": "组1"}, "colors": {"g1": "#eab308"},
                "parents": {"g1": None}, "children": {"g1": []}, "order": ["g1"]}
    window = g.BlueprintWindow(
        [_task("A", "g1"), _task("B", "g1")],
        {"positions": {"0": [100, 200], "1": [400, 200]}},
        lambda *args, **kwargs: None, metadata, None, {})
    window.resize(1100, 700)
    window.show()
    QApplication.processEvents()
    return window


def band(window, start_scene, end_scene, ctrl=False):
    modifiers = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier

    def send(kind, scene_point):
        local = QPointF(window.view.mapFromScene(scene_point))
        event = QMouseEvent(kind, local, window.view.viewport().mapToGlobal(local.toPoint()),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, modifiers)
        if kind == QEvent.Type.MouseButtonPress:
            window.view.mousePressEvent(event)
        elif kind == QEvent.Type.MouseMove:
            window.view.mouseMoveEvent(event)
        else:
            window.view.mouseReleaseEvent(event)

    send(QEvent.Type.MouseButtonPress, start_scene)
    midpoint = QPointF((start_scene.x() + end_scene.x()) / 2, (start_scene.y() + end_scene.y()) / 2)
    send(QEvent.Type.MouseMove, midpoint)
    send(QEvent.Type.MouseMove, end_scene)
    send(QEvent.Type.MouseButtonRelease, end_scene)
    QApplication.processEvents()
    return window.scene.selectedItems()


def group_selected(window):
    return any(isinstance(item, BlueprintGroupItem) and item.group_id == "g1"
               for item in window.scene.selectedItems())


class GroupBandSelectTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()
        self.box = self.window._group_items["g1"].sceneBoundingRect()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QApplication.processEvents()

    def test_band_across_group_body_selects_group(self):
        start = QPointF(self.box.left() - 60, self.box.top() + 60)
        end = QPointF(self.box.right() + 30, self.box.top() + 100)
        band(self.window, start, end)
        self.assertTrue(group_selected(self.window), "框住组体应选中组（展开组 shape 只有标题栏）")

    def test_band_only_over_group_empty_strip_selects_group(self):
        # 组底部没有节点的空条（节点底 320，组底 340）：只框住组框本身
        start = QPointF(self.box.left() - 60, self.box.bottom() - 5)
        end = QPointF(self.box.right() + 30, self.box.bottom() + 8)
        band(self.window, start, end)
        self.assertTrue(group_selected(self.window), "只框住组框空条也应选中组")

    def test_small_band_inside_group_empty_area_selects_group(self):
        start = QPointF(self.box.left() + 280, self.box.top() + 80)
        end = QPointF(self.box.left() + 380, self.box.top() + 140)
        band(self.window, start, end)
        self.assertTrue(group_selected(self.window), "在组内空白处拖小框应选中组")

    def test_band_away_from_group_does_not_select_group(self):
        band(self.window, QPointF(self.box.right() + 300, self.box.bottom() + 300),
             QPointF(self.box.right() + 500, self.box.bottom() + 500))
        self.assertFalse(group_selected(self.window))

    def test_ctrl_band_does_not_select_group(self):
        band(self.window, QPointF(self.box.left() - 60, self.box.top() + 60),
             QPointF(self.box.right() + 30, self.box.top() + 100), ctrl=True)
        self.assertFalse(group_selected(self.window), "Ctrl 框选只选连线，不应顺带选中组")

    def test_click_without_drag_does_not_select_group(self):
        point = QPointF(self.box.left() + 300, self.box.top() + 60)
        band(self.window, point, QPointF(point.x() + 1, point.y() + 1))
        self.assertFalse(group_selected(self.window), "单击空白不应把组选中")

    def test_band_around_group_still_selects_nodes(self):
        band(self.window, QPointF(self.box.left() - 40, self.box.top() - 40),
             QPointF(self.box.right() + 40, self.box.bottom() + 40))
        indices = [item.index for item in self.window.scene.selectedItems()
                   if hasattr(item, "index")]
        self.assertEqual(sorted(indices), [0, 1], "框选节点行为应保持不变")
        self.assertTrue(group_selected(self.window))

    def test_collapsed_card_band_still_selects_card(self):
        self.window.collapsed_groups = {"g1"}
        self.window.refresh()
        QApplication.processEvents()
        card = self.window._group_items["g1"].sceneBoundingRect()
        band(self.window, QPointF(card.left() - 20, card.top() - 20),
             QPointF(card.right() + 20, card.bottom() + 20))
        self.assertTrue(group_selected(self.window))


class BandSelectionGuardTests(unittest.TestCase):
    def test_release_applies_group_band_selection(self):
        source = open("blueprint_canvas.py", encoding="utf-8").read()
        self.assertIn("self._apply_group_band_selection(event)", source)
        self.assertIn("def _apply_group_band_selection(self, event):", source)
        self.assertIn("item.sceneBoundingRect().intersects(band)", source)

    def test_band_ignores_ctrl_wire_mode(self):
        source = open("blueprint_canvas.py", encoding="utf-8").read()
        self.assertIn("not from_empty or self._band_prefers_wires", source)


if __name__ == "__main__":
    unittest.main()
