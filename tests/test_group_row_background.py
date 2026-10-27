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


class GroupRowBackgroundGuardTests(unittest.TestCase):
    def test_draw_row_implementation_markers(self):
        source = open("task_widgets.py", encoding="utf-8").read()
        self.assertIn("def drawRow(self, painter, options, index):", source)
        self.assertIn("painter.fillRect(options.rect, color)", source)
        self.assertIn("Qt.UserRole) == \"group\"", source)


if __name__ == "__main__":
    unittest.main()
