# -*- coding: utf-8 -*-
"""所有步骤类型的「类型专用字段」修改后都应显示「未应用」。"""

import copy
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QPlainTextEdit

import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g


def _make_task(step_type):
    task = {"id": f"type-{step_type}", "type": step_type, "description": step_type,
            "enabled": True, "template": "1", "click": False}
    if step_type == "key_press":
        task.update({"key": "F", "hold_time": 0.1, "press_count": 1, "press_interval": 0.0})
    elif step_type == "keyboard_move":
        task["move_steps"] = [{"key": "W", "duration": 1.0}]
    elif step_type == "drag":
        task.update({"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4, "duration": 0.2})
    elif step_type == "delay":
        task["duration"] = 1.0
    elif step_type == "condition":
        task.update({"condition_templates": ["1"], "condition_operator": "all"})
    elif step_type == "switch":
        task.update({"switch_value": "a", "switch_cases": {"a": 1}})
    elif step_type == "loop":
        task.update({"loop_count": 2, "loop_target": 1})
    elif step_type == "event":
        task.update({"event_template": "1", "event_timeout": 1.0})
    elif step_type == "click_until_gone":
        task.update({"click_interval": 0.5, "click_until_timeout": 10})
    return task


class SpecialFieldDirtyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = []
        self.window = g.PySide6ScriptWindow()
        self.window.show()
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks = self._orig
        self._tmp.cleanup()

    def _select_task(self, task):
        g.TASKS[:] = [task]
        self.window.refresh_task_list()
        _APP.processEvents()
        item = self.window._find_task_item(0)
        item.setSelected(True)
        self.window.task_list.setCurrentItem(item)
        _APP.processEvents()
        self.window._set_editor_state(True)

    def _edit_first_special_editor(self):
        editors = self.window.special_edits
        self.assertTrue(editors, "该类型应有类型专用字段")
        editor = next(iter(editors.values()))
        if isinstance(editor, QCheckBox):
            editor.setChecked(not editor.isChecked())
        elif isinstance(editor, QPlainTextEdit):
            editor.setPlainText("W 1.2\nA 0.5")
        elif isinstance(editor, QLineEdit):
            editor.setText("changed")
            editor.textEdited.emit("changed")
        else:
            self.fail(f"未知控件类型: {type(editor)}")

    def test_all_step_types_mark_dirty(self):
        for step_type in ("key_press", "keyboard_move", "drag", "delay",
                          "condition", "switch", "loop", "event"):
            with self.subTest(type=step_type):
                self._select_task(_make_task(step_type))
                self.assertEqual(self.window.editor_state_label.text(), "已保存")
                self._edit_first_special_editor()
                _APP.processEvents()
                self.assertEqual(self.window.editor_state_label.text(), "未应用",
                                 f"{step_type} 的类型专用字段没有触发未应用")

    def test_condition_invert_checkbox_marks_dirty(self):
        self._select_task(_make_task("condition"))
        checkbox = self.window.special_edits.get("condition_invert")
        self.assertIsInstance(checkbox, QCheckBox)
        checkbox.setChecked(not checkbox.isChecked())
        _APP.processEvents()
        self.assertEqual(self.window.editor_state_label.text(), "未应用")

    def test_key_press_count_and_interval_mark_dirty(self):
        self._select_task(_make_task("key_press"))
        for key in ("press_count", "press_interval"):
            self.window._set_editor_state(True)
            editor = self.window.special_edits[key]
            editor.setText("3" if key == "press_count" else "0.5")
            editor.textEdited.emit(editor.text())
            _APP.processEvents()
            self.assertEqual(self.window.editor_state_label.text(), "未应用", key)


if __name__ == "__main__":
    unittest.main()
