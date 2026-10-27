# -*- coding: utf-8 -*-
"""蓝图：收起群组节点的端口可用性 + 组头填色（所有者 2026-10-27 反馈）。

问题：
  1. 收起的群组节点画了左右端口，但**拉不了线**（连接逻辑只认普通节点端口）；
  2. 组头填色不完整：上半正常、下半发黑——`QPainterPath` 默认 OddEvenFill，
     `addRoundedRect + addRect` 的重叠区被"挖空"（表头底部一截透出深色卡片底）。

修复：
  · `BlueprintGroupItem.port_at` 命中左右端口，视图在按下/悬停/释放时都识别折叠卡片：
    从卡片输出拉线 = 从组内**最后一步**出去；拉到卡片输入 = 接到组内**第一步**；
  · 表头路径用 `WindingFill` 取并集，填色完整。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
from blueprint_canvas import BlueprintGroupItem  # noqa: E402

_APP = QApplication.instance() or QApplication([])
GROUP_COLOR = "#eab308"


def _task(task_id, group_id=None):
    task = {"id": task_id, "type": "normal", "description": task_id, "enabled": True, "template": "1"}
    if group_id:
        task["group_id"] = group_id
        task["group_name"] = group_id
    return task


def make_window(two_groups=False):
    if two_groups:
        metadata = {
            "names": {"g1": "组1", "g2": "组2"},
            "colors": {"g1": GROUP_COLOR, "g2": "#38bdf8"},
            "parents": {"g1": None, "g2": None},
            "children": {"g1": [], "g2": []},
            "order": ["g1", "g2"],
        }
        tasks = [_task("A", "g1"), _task("B", "g1"), _task("C", "g2"), _task("D", "g2")]
        positions = {"0": [60, 120], "1": [340, 120], "2": [60, 400], "3": [340, 400]}
    else:
        metadata = {
            "names": {"g1": "组1"},
            "colors": {"g1": GROUP_COLOR},
            "parents": {"g1": None},
            "children": {"g1": []},
            "order": ["g1"],
        }
        tasks = [_task("A", "g1"), _task("B", "g1"), _task("E"), _task("F")]
        positions = {"0": [60, 120], "1": [340, 120], "2": [640, 360], "3": [920, 360]}
    window = g.BlueprintWindow(tasks, {"positions": positions}, lambda *a, **k: None,
                               metadata, None, {})
    window.resize(1100, 700)
    window.show()
    QApplication.processEvents()
    window.collapsed_groups = set(metadata["names"])
    window.refresh()
    QApplication.processEvents()
    return window


def _send(window, kind, scene_point):
    local = QPointF(window.view.mapFromScene(scene_point))
    event = QMouseEvent(kind, local, window.view.viewport().mapToGlobal(local.toPoint()),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    if kind == QEvent.Type.MouseButtonPress:
        window.view.mousePressEvent(event)
    else:
        window.view.mouseReleaseEvent(event)


def drag(window, start_scene, end_scene):
    _send(window, QEvent.Type.MouseButtonPress, start_scene)
    _send(window, QEvent.Type.MouseButtonRelease, end_scene)
    QApplication.processEvents()


def connections(window):
    return [(source, target) for source, _output, target, _default in window._connections()]


def node_port_point(node, side, name="output"):
    if side == "input":
        return QPointF(node.x(), node.y() + node.rect().height() / 2)
    spec = node.port_for_name(name or "output")
    offset = spec[2] if spec else 0
    return QPointF(node.x() + node.rect().width(), node.y() + node.port_y(offset))


class GroupCardPortHitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = make_window()
        cls.card = cls.window._group_items["g1"]

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.window.deleteLater()
        QApplication.processEvents()

    def test_left_and_right_edges_are_ports(self):
        self.assertEqual(self.card.port_at(self.card.mapFromScene(self.card.input_port_point()), 14), "input")
        self.assertEqual(self.card.port_at(self.card.mapFromScene(self.card.output_port_point()), 14), "output")

    def test_center_is_not_a_port(self):
        self.assertIsNone(self.card.port_at(self.card.rect().center(), 14))

    def test_expanded_group_has_no_modeless_ports(self):
        window = make_window()
        try:
            window.collapsed_groups = set()
            window.refresh()
            QApplication.processEvents()
            box = window._group_items["g1"]
            self.assertFalse(box.collapsed)
            self.assertIsNone(box.port_at(box.rect().center(), 14))
        finally:
            window.close()

    def test_hover_port_state_is_tracked(self):
        self.card.set_hover_port("input")
        self.assertEqual(self.card._hover_port, "input")
        self.card.set_hover_port(None)
        self.assertIsNone(self.card._hover_port)


class HeaderFillTests(unittest.TestCase):
    """"组头下半截发黑"回归：表头 26px 内任意位置都应是组色。"""

    @classmethod
    def setUpClass(cls):
        cls.window = make_window()
        cls.card = cls.window._group_items["g1"]

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.window.deleteLater()
        QApplication.processEvents()

    def _header_pixels(self):
        rect = self.card.rect()
        image = QImage(int(rect.width()) + 4, int(rect.height()) + 4, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        painter.translate(-rect.left() + 2, -rect.top() + 2)
        self.card.paint(painter, None)
        painter.end()
        samples = []
        for y in (3, 10, 18, 24):
            for x in (30, 110, int(rect.width()) - 30):
                samples.append(image.pixelColor(x + 2, y + 2))
        return samples

    def test_header_fill_is_complete(self):
        expected = QColor(GROUP_COLOR)
        for pixel in self._header_pixels():
            self.assertLessEqual(
                abs(pixel.red() - expected.red()) + abs(pixel.green() - expected.green())
                + abs(pixel.blue() - expected.blue()), 30,
                f"表头填色不完整（像素 {pixel.name()}），QPainterPath 重叠区可能又被 OddEvenFill 挖空",
            )

    def test_header_bottom_strip_not_dark(self):
        rect = self.card.rect()
        image = QImage(int(rect.width()) + 4, int(rect.height()) + 4, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        painter.translate(-rect.left() + 2, -rect.top() + 2)
        self.card.paint(painter, None)
        painter.end()
        pixel = image.pixelColor(110 + 2, 24 + 2)
        self.assertGreater(pixel.green(), 120, f"表头底边是深色 {pixel.name()}（应有组色填充）")


class GroupCardConnectionTests(unittest.TestCase):
    """折叠卡片端口可以拉线：卡片输出=组内最后一步，卡片输入=组内第一步。"""

    def setUp(self):
        self.window = make_window()
        self.card = self.window._group_items["g1"]

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QApplication.processEvents()

    def test_drag_from_card_output_to_visible_node(self):
        target = self.window._node_items[3]          # F（非相邻，确保不是已有默认连线）
        drag(self.window, self.card.output_port_point(), node_port_point(target, "input"))
        pairs = connections(self.window)
        self.assertIn((1, 3), pairs, "卡片输出应把组内最后一步(B)连到目标节点(F)")
        task_b = self.window.tasks[1]
        self.assertEqual(task_b.get("flow_next"), "F", "最后一步的 flow_next 应指向目标")

    def test_drag_from_visible_node_to_card_input(self):
        source = self.window._node_items[2]          # E
        drag(self.window, node_port_point(source, "output"), self.card.input_port_point())
        pairs = connections(self.window)
        self.assertIn((2, 0), pairs, "外部节点(E)应连到组内第一步(A)")
        task_e = self.window.tasks[2]
        self.assertEqual(task_e.get("flow_next"), "A")

    def test_drag_between_two_group_cards(self):
        window = make_window(two_groups=True)
        try:
            card1 = window._group_items["g1"]
            card2 = window._group_items["g2"]
            drag(window, card2.output_port_point(), card1.input_port_point())
            self.assertIn((3, 0), connections(window), "组2 最后一步(D)应连到 组1 第一步(A)")
        finally:
            window.close()

    def test_drag_within_same_card_creates_nothing(self):
        before = connections(self.window)
        drag(self.window, self.card.output_port_point(), self.card.input_port_point())
        self.assertEqual(connections(self.window), before, "同一个群组节点自己连自己不产生连线")

    def test_group_index_mapping_roles(self):
        self.assertEqual(self.window.view._connection_index(self.card, "source"), 1, "源=组内最后一步")
        self.assertEqual(self.window.view._connection_index(self.card, "target"), 0, "目标=组内第一步")
        node = self.window._node_items[2]
        self.assertEqual(self.window.view._connection_index(node, "source"), 2)


class GroupPortWiringGuardTests(unittest.TestCase):
    def test_view_recognizes_group_cards_for_connections(self):
        source = open("blueprint_canvas.py", encoding="utf-8").read()
        self.assertIn("_group_card_at_scene_pos", source)
        self.assertIn("card.port_at(card.mapFromScene(scene_position)", source)
        self.assertIn("self._connection_index(source_node", source)

    def test_header_uses_winding_fill(self):
        source = open("blueprint_canvas.py", encoding="utf-8").read()
        self.assertIn("header_path.setFillRule(Qt.FillRule.WindingFill)", source)


if __name__ == "__main__":
    unittest.main()
