# -*- coding: utf-8 -*-
"""蓝图连线手感（§8.1 #77）：端口判定必须"最近者胜"，并且按在节点边缘外一点点也能起拖。

背景（全部为实测数字，见 2026-10-18 的测量）：
  · 端口间距只有 16/20/32px，而旧判定容差固定 14px，且"第一个落进容差内的就返回"
    → 重叠区永远归 ports() 里靠前的「顺序」：缩放 100% 时瞄准其它端口错抓 31%，
      缩放 43% 时错抓 43%、漏判 7%、命中仅 50%。
  · 端口圆圈画在节点边框上，按偏 1px 到节点外，`itemAt()` 就返回 None → 起不了拖。
  · 拖到目标节点外 1–6px 松手不算目标（代码里的 ±16 余量是死代码）。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def make_task(task_id="a", index=0):
    return {"id": task_id, "type": "normal", "description": f"探针 {index}", "enabled": True,
            "template": "1", "click": True, "required": True}


def make_window(zoom=1.0, count=1):
    window = g.BlueprintWindow([make_task(f"t{i}", i) for i in range(count)], {},
                               lambda *a, **k: None, {}, None, {})
    window.resize(1400, 900)
    if zoom != 1.0:
        window.view.scale(zoom, zoom)
    return window


def mouse_event(kind, view, scene_point, button=Qt.LeftButton):
    viewport = view.viewport()
    local = QPointF(view.mapFromScene(scene_point))
    return QMouseEvent(kind, local, viewport.mapToGlobal(local.toPoint()),
                       button, button, Qt.NoModifier)


class PortHitTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()
        self.node = self.window._node_items[0]
        self.tolerance = self.window.view._port_tolerance()

    def tearDown(self):
        self.window.close()

    def _port_point(self, name, dy):
        spec = self.node.port_for_name(name)
        self.assertIsNotNone(spec, f"节点没有端口 {name}")
        return QPointF(self.node.rect().width() - 5, self.node.port_y(spec[2]) + dy)

    def test_each_port_is_hit_where_it_is_drawn(self):
        """瞄准哪个端口就得到哪个端口（偏差在半个间距以内）。"""
        for name, _label, _offset in self.node.ports():
            for dy in (-6, -3, 0, 3, 6):
                with self.subTest(port=name, dy=dy):
                    self.assertEqual(self.node.output_at(self._port_point(name, dy), self.tolerance), name)

    def test_overlapping_zone_no_longer_steals_to_the_first_port(self):
        """回归：旧代码里"未识别"往「顺序」方向偏 6px 就会被「顺序」抢走。"""
        self.assertEqual(self.node.output_at(self._port_point("failure", 6), self.tolerance), "failure")
        self.assertEqual(self.node.output_at(self._port_point("success", -6), self.tolerance), "success")

    def test_tolerance_follows_zoom(self):
        """容差按屏幕像素换算：缩小画布后不会跟着缩成针尖。"""
        self.assertAlmostEqual(self.tolerance, 14.0, places=3)  # 100% 缩放时是 14
        view = self.window.view
        view.scale(0.43, 0.43)
        self.assertAlmostEqual(view._view_scale(), 0.43, places=3)
        self.assertAlmostEqual(view._port_tolerance(), 22.0, places=3)  # 12/0.43=27.9 → 上限 22
        # 旧容差 14 在 43% 缩放下够不到这个点（离端口 18px），现在应该够得到
        spec = self.node.port_for_name("timeout")
        point = QPointF(self.node.rect().width() - 5, self.node.port_y(spec[2]) - 18)
        self.assertEqual(self.node.output_at(point, view._port_tolerance()), "timeout")
        self.assertIsNone(self.node.output_at(point, 14.0))

    def test_middle_of_node_body_is_not_a_port(self):
        """节点身体中部不能被当成端口，否则节点就没法拖动了。"""
        middle = QPointF(self.node.rect().width() / 2, self.node.rect().height() / 2)
        self.assertIsNone(self.node.output_at(middle, self.tolerance))
        self.assertIsNone(self.node.input_at(middle, self.tolerance))


class EdgeAndDropTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window(count=2)
        self.source = self.window._node_items[0]
        self.target = self.window._node_items[1]
        self.target.setPos(900, 500)
        self.view = self.window.view

    def tearDown(self):
        self.window.close()

    def test_press_just_outside_the_right_edge_still_starts_a_connection(self):
        width = self.source.rect().width()
        for dx in (1, 3, 6, 9):
            with self.subTest(dx=dx):
                point = self.source.mapToScene(
                    QPointF(width + dx, self.source.port_y(0)))
                found = self.view._node_at_scene_pos(point, self.view._START_MARGIN_PX)
                self.assertIs(found, self.source, f"右边缘外 {dx}px 应该还能抓到节点")
                local = self.source.mapFromScene(point)
                self.assertEqual(self.source.output_at(local, self.view._port_tolerance()), "output")

    def test_press_just_outside_the_left_edge_still_grabs_the_input_port(self):
        middle = self.source.rect().height() / 2
        point = self.source.mapToScene(QPointF(-6, middle))
        found = self.view._node_at_scene_pos(point, self.view._START_MARGIN_PX)
        self.assertIs(found, self.source)
        local = self.source.mapFromScene(point)
        self.assertEqual(self.source.input_at(local, self.view._port_tolerance()), "input")

    def test_drop_just_outside_the_target_connects(self):
        middle = self.target.rect().height() / 2
        for dx in (-8, -4, 0):
            with self.subTest(dx=dx):
                point = self.target.mapToScene(QPointF(dx, middle))
                target, port = self.view._connection_target(point, self.source, "output")
                self.assertIs(target, self.target, f"目标左侧 {dx}px 应该算接上")
                self.assertEqual(port, "input")

    def test_full_drag_gesture_emits_the_connection(self):
        """走真实事件链：按下输出口 → 移到目标节点 → 松手，必须发出连线请求。"""
        captured = []
        self.view.connection_requested.connect(
            lambda source_index, target_index, port: captured.append((source_index, target_index, port)))
        start = self.source.mapToScene(
            QPointF(self.source.rect().width() + 4, self.source.port_y(0)))
        end = self.target.mapToScene(QPointF(-5, self.target.rect().height() / 2))
        self.view.mousePressEvent(mouse_event(QEvent.MouseButtonPress, self.view, start))
        self.assertIsNotNone(self.view.pending_connection, "从输出口按下应该进入连线状态")
        self.view.mouseMoveEvent(mouse_event(QEvent.MouseMove, self.view, end))
        self.view.mouseReleaseEvent(mouse_event(QEvent.MouseButtonRelease, self.view, end))
        self.assertEqual(captured, [(0, 1, "output")])

    def test_hover_marks_the_port_that_would_be_grabbed(self):
        scene_point = self.source.mapToScene(
            QPointF(self.source.rect().width() - 5, self.source.port_y(0)))
        self.view._update_hover_feedback(QPointF(self.view.mapFromScene(scene_point)))
        self.assertEqual(self.source._hover_port, "output")
        far = self.source.mapToScene(QPointF(-400, -400))
        self.view._update_hover_feedback(QPointF(self.view.mapFromScene(far)))
        self.assertIsNone(self.source._hover_port)

    def test_hover_survives_a_scene_refresh(self):
        """`refresh()` 会销毁所有节点，而悬停状态里存着节点对象。

        不防这一下，刷新之后鼠标一动就会碰到已销毁的 C++ 对象并抛 RuntimeError
        （新增/删除步骤、应用修改、撤销都会触发 refresh）。
        """
        scene_point = self.source.mapToScene(
            QPointF(self.source.rect().width() - 5, self.source.port_y(0)))
        view_point = QPointF(self.view.mapFromScene(scene_point))
        self.view._update_hover_feedback(view_point)
        self.assertEqual(self.source._hover_port, "output")

        self.window.refresh()   # 旧节点全部销毁并重建
        self.view._update_hover_feedback(view_point)   # 不得抛异常
        self.view._clear_hover()                       # 同样不得抛异常
        self.assertIsNone(self.view._hovered_port)


if __name__ == "__main__":
    unittest.main()
