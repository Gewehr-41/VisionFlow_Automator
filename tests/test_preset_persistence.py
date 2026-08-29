import json
import os
import tempfile
import unittest
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui_pyside6 import PySide6ScriptWindow
import tasks


class PresetPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.original_presets_file = tasks.PRESETS_FILE
        self.original_tasks_file = tasks.TASKS_FILE
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
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
        self._tmp.cleanup()
        tasks.PRESETS_FILE = self.original_presets_file
        tasks.TASKS_FILE = self.original_tasks_file

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
