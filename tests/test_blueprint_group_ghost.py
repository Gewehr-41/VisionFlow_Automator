# -*- coding: utf-8 -*-
"""蓝图群组卡片拖动残影（所有者 2026-10-27：组节点端点没连接时拖动出现重影）。

根因：收起态的输入/输出端口画在 `rect()` 的左右边框上（半径 5，一半在 rect 外），
但 `BlueprintGroupItem` 没重写 `boundingRect()`；Qt 移动图元只重绘
"旧包围盒 ∪ 新包围盒"，包围盒不含端口 → 旧位置的半圈圆点没被擦掉 = 重影。
（普通步骤节点的包围盒是 `adjusted(-10,-10,10,10)`，所以它不重影。）

测试用"局部重绘"忠实复现机制：先整场景画一次 → 图元移开 → 只擦除并重画**旧包围盒**区域
→ 检查旧端口位置是否已恢复成背景色。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
from blueprint_canvas import BlueprintGroupItem  # noqa: E402

_APP = QApplication.instance() or QApplication([])
BACKGROUND = QColor("#1a1d24")
CARD_FILL = QColor("#111827")


def _task(task_id, group_id):
    return {"id": task_id, "type": "normal", "description": task_id, "enabled": True,
            "template": "1", "group_id": group_id}


def make_window(collapsed=True):
    metadata = {
        "names": {"g1": "战斗流程"},
        "colors": {"g1": "#eab308"},
        "parents": {"g1": None},
        "children": {"g1": []},
        "order": ["g1"],
    }
    tasks = [_task("A", "g1"), _task("B", "g1")]
    layout = {"positions": {"0": [80, 100], "1": [360, 100]}}
    window = g.BlueprintWindow(tasks, layout, lambda *a, **k: None, metadata, None, {})
    window.resize(1200, 800)
    window.show()
    QApplication.processEvents()
    if collapsed:
        window.collapsed_groups = {"g1"}
    window.refresh()
    QApplication.processEvents()
    return window


class GroupGhostTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()
        self.box = self.window._group_items["g1"]
        self.scene_rect = self.window.scene.sceneRect()
        self.image = QImage(1000, 700, QImage.Format.Format_ARGB32)
        self.scale_x = self.image.width() / self.scene_rect.width()
        self.scale_y = self.image.height() / self.scene_rect.height()
        self._render_full()

    def tearDown(self):
        self.window.close()

    # ---------- 坐标换算 / 绘制 ----------

    def _to_image(self, point):
        return QPointF((point.x() - self.scene_rect.left()) * self.scale_x,
                       (point.y() - self.scene_rect.top()) * self.scale_y)

    def _region_to_image(self, rect):
        top_left = self._to_image(rect.topLeft())
        bottom_right = self._to_image(rect.bottomRight())
        return QRectF(top_left, bottom_right)

    def _render_full(self):
        self.image.fill(BACKGROUND)
        painter = QPainter(self.image)
        self.window.scene.render(painter, QRectF(self.image.rect()), self.scene_rect)
        painter.end()

    def _repaint_region(self, scene_bounds):
        image_rect = self._region_to_image(scene_bounds)
        painter = QPainter(self.image)
        painter.setClipRect(image_rect)
        painter.fillRect(image_rect, BACKGROUND)
        self.window.scene.render(painter, image_rect, scene_bounds)
        painter.end()

    def _pixel(self, scene_point):
        return self.image.pixelColor(self._to_image(scene_point).toPoint())

    @staticmethod
    def _close_to(pixel, expected, tolerance=14):
        return (abs(pixel.red() - expected.red()) + abs(pixel.green() - expected.green())
                + abs(pixel.blue() - expected.blue())) <= tolerance

    # ---------- 用例 ----------

    def test_bounding_rect_covers_ports(self):
        rect = self.box.rect()
        bounds = self.box.boundingRect()
        self.assertLessEqual(bounds.left(), rect.left() - 6,
                             "包围盒要覆盖画在边框外的输入端口（半径 5 + 描边）")
        self.assertGreaterEqual(bounds.right(), rect.right() + 6,
                                "包围盒要覆盖输出端口")
        self.assertLessEqual(bounds.top(), rect.top() - 2, "也要覆盖 2px 描边")

    def test_dragging_leaves_no_stale_port_pixels(self):
        rect = self.box.rect()
        old_bounds = self.box.boundingRect().translated(self.box.pos())
        # 端口半径 5，圆心在边框上：检查「圆点在 rect 外的那一半」(x = left - 3)
        old_port_scene = self.box.mapToScene(rect.left() - 3, rect.center().y())
        # 先确认拖动前该位置确实画着端口，不是背景
        self.assertFalse(self._close_to(self._pixel(old_port_scene), BACKGROUND),
                         "测试前提：拖动前端口位置应有内容")

        # 模拟拖动：把卡片向右挪出 320（不与旧位置重叠），再按 Qt 规则只重绘旧包围盒区域
        self.box.setPos(self.box.pos().x() + 320, self.box.pos().y())
        _APP.processEvents()
        self._repaint_region(old_bounds)
        _APP.processEvents()

        self.assertTrue(self._close_to(self._pixel(old_port_scene), BACKGROUND, 20),
                        f"旧端口位置仍有残留像素 {self._pixel(old_port_scene).name()}，说明拖动重影没修好")

    def test_new_position_is_painted_after_move(self):
        rect = self.box.rect()
        old_port_scene = self.box.mapToScene(rect.left() - 3, rect.center().y())
        self.box.setPos(self.box.pos().x() + 320, self.box.pos().y())
        _APP.processEvents()
        self._render_full()
        moved_port = self.box.mapToScene(rect.left() - 3, rect.center().y())
        self.assertFalse(self._close_to(self._pixel(moved_port), BACKGROUND),
                         "移动后的位置应画着端口")
        self.assertTrue(self._close_to(self._pixel(old_port_scene), BACKGROUND, 20),
                        "整场景重绘后旧位置应是背景（不残留卡片/端口）")

    def test_expanded_group_bounding_rect_covers_border(self):
        window = make_window(collapsed=False)
        try:
            box = window._group_items["g1"]
            rect = box.rect()
            bounds = box.boundingRect()
            self.assertLessEqual(bounds.left(), rect.left() - 1)
            self.assertGreaterEqual(bounds.right(), rect.right() + 1)
        finally:
            window.close()

    def test_group_item_is_a_collapsed_card(self):
        self.assertIsInstance(self.box, BlueprintGroupItem)
        self.assertTrue(self.box.collapsed)


if __name__ == "__main__":
    unittest.main()
