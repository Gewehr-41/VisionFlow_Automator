import json
import os
import tempfile
import unittest
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import tasks

# 构造 QWidget 之前必须先有 QApplication，否则 Qt 会直接以
# "Must construct a QApplication before a QWidget" 中止进程。
# 用模块级单例，避免每个测试各建一个 QApplication（Qt 只允许一个）。
_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g
from gui_pyside6 import PySide6ScriptWindow


class PresetPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.original_presets_file = tasks.PRESETS_FILE
        self.original_tasks_file = tasks.TASKS_FILE
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        # 必须同时替换 gui_pyside6 里持有的 save_* 引用：
        # 它们是在导入时绑定的原函数，读的是当时的模块级路径常量，
        # 仅改 tasks.PRESETS_FILE 不会影响它们，会写入真实的工程数据文件。
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        self._tasks_snapshot = deepcopy(tasks.TASKS)
        if os.path.exists(tasks.PRESETS_FILE):
            os.remove(tasks.PRESETS_FILE)
        if os.path.exists(tasks.TASKS_FILE):
            os.remove(tasks.TASKS_FILE)

        tasks.USER_PRESETS.clear()
        tasks.USER_PRESETS["追放每日任务"] = [{
            "type": "click_until_gone",
            "template": "old_img",
            "templates": ["old_img"],
            "description": "旧图片",
        }]
        tasks.TASKS[:] = deepcopy(tasks.USER_PRESETS["追放每日任务"])

    def tearDown(self):
        os.chdir(self._cwd)
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        tasks.TASKS[:] = self._tasks_snapshot
        tasks.PRESETS_FILE = self.original_presets_file
        tasks.TASKS_FILE = self.original_tasks_file
        self._tmp.cleanup()

    def test_non_custom_preset_saves_to_preset_file(self):
        window = PySide6ScriptWindow()
        window.mode_combo.setCurrentText("追放每日任务")
        window.mode_tasks["追放每日任务"] = [{
            "type": "click_until_gone",
            "template": "new_img",
            "templates": ["new_img"],
            "description": "新图片",
        }]
        tasks.TASKS[:] = deepcopy(window.mode_tasks["追放每日任务"])

        window.save_current_tasks()

        with open(tasks.PRESETS_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertIn("追放每日任务", payload)
        self.assertEqual(payload["追放每日任务"][0]["template"], "new_img")


if __name__ == "__main__":
    unittest.main()
