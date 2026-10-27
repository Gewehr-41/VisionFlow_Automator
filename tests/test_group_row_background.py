# -*- coding: utf-8 -*-
"""组行底色铺满整行（2026-10-27 所有者反馈）。

现象：给分组设置颜色后，组行左侧的缩进/展开箭头那一小段仍是树默认白底，
复选框看起来"蓝底白勾没有铺满、左边还有一段白边"。
原因：`QTreeWidgetItem.setBackground()` 只覆盖 item 区域（缩进区之后）；
本次在 `TaskListWidget.drawRow()` 里先用组颜色铺满整行（含缩进区），再交给基类绘制。
"""

import copy
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])

GROUP_COLOR = "#22c55e"


class GroupRowBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(cls._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(cls._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        cls._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = []
        cls.window = g.PySide6ScriptWindow()
        cls.window.show()
        _APP.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        g.TASKS[:] = cls._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = cls._orig
        cls._tmp.cleanup()

    def setUp(self):
        self.window.mode_combo.setCurrentText("custom")
        mode = "custom"
        self.window.mode_group_metadata[mode] = {
            "names": {"g1": "绿色组"},
            "colors": {"g1": GROUP_COLOR},
            "parents": {"g1": None},
            "children": {"g1": []},
            "order": ["g1"],
        }
        g.TASKS[:] = [
            {"id": "A", "type": "normal", "description": "步骤A", "template": "1",
             "enabled": True, "group_id": "g1"},
        ]
        self.window.refresh_task_list()
        _APP.processEvents()
        self.window.task_list.clearSelection()
        _APP.processEvents()

    def _left_pixel(self, item):
        """取行左侧缩进/箭头区域（复选框左边）的像素颜色。"""
        rect = self.window.task_list.visualItemRect(item)
        image = self.window.task_list.viewport().grab().toImage()
        return image.pixelColor(4, rect.center().y()).name()

    def test_group_row_color_fills_left_indent_area(self):
        item = self.window._group_items()["g1"]
        self.assertEqual(self._left_pixel(item), GROUP_COLOR.lower(),
                         "组行缩进区应被组颜色铺满，不能留白边")

    def test_step_row_indent_area_is_unchanged(self):
        step_item = self.window._find_task_item(0)
        self.assertEqual(self._left_pixel(step_item), "#ffffff",
                         "步骤行没有组底色，左侧应保持默认白底")

    def test_selected_group_row_still_uses_selection_color(self):
        item = self.window._group_items()["g1"]
        self.window.task_list.setCurrentItem(item)
        item.setSelected(True)
        _APP.processEvents()
        self.assertEqual(self._left_pixel(item), "#dbeafe",
                         "选中组行应显示选中色（组颜色只作为行底，不能盖住选中态）")


class GroupCheckboxContrastTests(unittest.TestCase):
    """组行颜色和复选框同色时，复选框不能"消失"（所有者 2026-10-27 反馈：只有悬浮才显示）。"""

    _SAME_BLUE = "#0284c7"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(cls._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(cls._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        cls._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = []
        cls.window = g.PySide6ScriptWindow()
        cls.window.show()
        _APP.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        g.TASKS[:] = cls._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = cls._orig
        cls._tmp.cleanup()

    def setUp(self):
        self.window.mode_combo.setCurrentText("custom")
        self.window.mode_group_metadata["custom"] = {
            "names": {"g1": "蓝组"},
            "colors": {"g1": self._SAME_BLUE},
            "parents": {"g1": None},
            "children": {"g1": []},
            "order": ["g1"],
        }
        g.TASKS[:] = [
            {"id": "A", "type": "normal", "description": "步骤A", "template": "1",
             "enabled": True, "group_id": "g1"},
        ]
        self.window.refresh_task_list()
        _APP.processEvents()
        self.window.task_list.clearSelection()
        _APP.processEvents()

    def test_checked_checkbox_has_white_outline_on_same_color_row(self):
        item = self.window._group_items()["g1"]
        self.assertEqual(item.checkState(0), Qt.CheckState.Checked)
        rect = self.window.task_list.visualItemRect(item)
        image = self.window.task_list.viewport().grab().toImage()
        cy = rect.center().y()
        # 复选框外框在 viewport x=27..46；左侧 2px 是描边
        border = image.pixelColor(28, cy).name()
        fill = image.pixelColor(40, cy).name()   # 避让中间的白勾笔画
        row_background = image.pixelColor(4, cy).name()
        self.assertEqual(row_background, self._SAME_BLUE, "组行底色应和复选框同色（复现条件）")
        self.assertEqual(border, "#ffffff", "复选框必须有白色描边，否则和组行同色时看不见")
        self.assertEqual(fill, self._SAME_BLUE, "复选框内部仍是蓝色底")


class GroupRowBackgroundGuardTests(unittest.TestCase):
    def test_draw_row_implementation_markers(self):
        source = open("task_widgets.py", encoding="utf-8").read()
        self.assertIn("def drawRow(self, painter, options, index):", source)
        self.assertIn("painter.fillRect(options.rect, color)", source)
        self.assertIn("Qt.UserRole) == \"group\"", source)

    def test_checkbox_is_self_drawn_with_white_border(self):
        """Windows 11 原生样式不常驻绘制 QSS indicator，复选框必须由 delegate 自绘。"""
        widget_source = open("task_widgets.py", encoding="utf-8").read()
        gui_source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("def _paint_checkbox", widget_source)
        self.assertIn('border = QColor("#ffffff")', widget_source)
        self.assertIn("Qt.CheckState(int(state))", widget_source,
                      "CheckStateRole 是 int，必须转枚举，否则勾选态会按未选中画")
        self.assertIn(
            "QTreeWidget::indicator { width: 20px; height: 20px; background: transparent;"
            " border: none; image: none; }",
            gui_source, "QSS indicator 只应保留占位，不应再依赖它画复选框")


if __name__ == "__main__":
    unittest.main()
