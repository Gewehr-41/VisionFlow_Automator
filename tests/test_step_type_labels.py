# -*- coding: utf-8 -*-
"""步骤类型在界面上统一显示中文；数据里仍保留英文 type。"""

import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import step_types
import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g


class StepTypeLabelTests(unittest.TestCase):
    def test_mapping_keeps_internal_types(self):
        self.assertEqual(step_types.type_label("key_press"), "按键")
        self.assertEqual(step_types.type_label("click_until_gone"), "持续点击")
        self.assertEqual(step_types.type_from_label("按键"), "key_press")
        self.assertEqual(step_types.type_from_label("key_press"), "key_press")
        self.assertEqual(step_types.type_from_label("未知"), "normal")

    def test_blueprint_canvas_uses_chinese_label(self):
        import pathlib
        source = pathlib.Path("blueprint_canvas.py").read_text(encoding="utf-8")
        self.assertIn("step_types.type_label", source)
        self.assertNotIn('{self.task.get(\'type\', \'normal\')}"', source)


class StepTypeLabelUiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = [
            {"id": "label-key", "type": "key_press", "description": "按键测试",
             "enabled": True, "template": "F", "key": "F"},
            {"id": "label-delay", "type": "delay", "description": "延迟测试",
             "enabled": True, "duration": 1.0},
        ]
        self.window = g.PySide6ScriptWindow()
        self.window.show()
        _APP.processEvents()
        self.window.refresh_task_list()
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks = self._orig
        self._tmp.cleanup()

    def _select(self, index):
        item = self.window._find_task_item(index)
        item.setSelected(True)
        self.window.task_list.setCurrentItem(item)
        _APP.processEvents()

    def test_task_list_shows_chinese_type(self):
        self.assertIn("[按键]", self.window._find_task_item(0).text(0))
        self.assertIn("[延迟]", self.window._find_task_item(1).text(0))
        self.assertNotIn("[key_press]", self.window._find_task_item(0).text(0))

    def test_editor_label_shows_chinese_type(self):
        self._select(0)
        self.assertIn("类型: 按键", self.window.selected_label.text())

    def test_add_task_dialog_maps_chinese_back_to_internal_type(self):
        with mock.patch.object(g.QInputDialog, "getItem", return_value=("按键", True)):
            self.window.add_task()
        self.assertEqual(g.TASKS[-1]["type"], "key_press")
        self.assertIn("按键", g.TASKS[-1]["description"])


if __name__ == "__main__":
    unittest.main()
