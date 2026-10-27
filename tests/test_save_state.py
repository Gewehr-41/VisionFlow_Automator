# -*- coding: utf-8 -*-
"""L4：状态栏「保存状态」胶囊（2026-10-27）。

之前「保存」按钮与大量自动保存并存，界面没有"脏/已保存"状态，用户不清楚改到哪一步。
现在状态栏显示三种状态：
  · ● 已保存           —— 内存数据与最近一次写盘一致（日常操作会自动保存）
  · ● 未保存           —— 内存里有改动还没写盘（点「保存」/Ctrl+S）
  · ● 有未应用修改     —— 编辑器里有改动还没点「应用修改」（这部分还没进步骤）
状态栏是全局的，取消选中步骤后仍然能看到"有未应用修改"。
"""

import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class SaveStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(cls._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(cls._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        cls._tasks_snapshot = copy.deepcopy(g.TASKS)
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
        g.TASKS[:] = [{"id": "A", "type": "normal", "description": "原始名称",
                       "template": "1", "enabled": True}]
        self.window.refresh_task_list()
        _APP.processEvents()
        self.window.save_current_tasks()
        self.window._saved_snapshot = None
        self.window._refresh_saved_snapshot()
        self.window._editor_dirty = False
        self.window._refresh_save_state()

    # ---------- 辅助 ----------
    def _state(self):
        return (self.window.save_state_label.text(),
                self.window.save_state_label.property("saveKind"))

    def _select(self, index):
        self.window.task_list.clearSelection()
        item = self.window._find_task_item(index)
        item.setSelected(True)
        self.window.task_list.setCurrentItem(item)
        _APP.processEvents()

    def _type(self, text):
        self.window.description_edit.setText(text)
        self.window.description_edit.textEdited.emit(text)
        _APP.processEvents()

    # ---------- 状态流 ----------
    def test_initial_state_is_saved(self):
        self.assertEqual(self._state(), ("● 已保存", "saved"))

    def test_editor_change_marks_dirty(self):
        self._select(0)
        self._type("改过的名字")
        self.assertEqual(self._state(), ("● 有未应用修改", "dirty"))
        self.assertIn("应用修改", self.window.save_state_label.toolTip())

    def test_dirty_state_survives_deselect(self):
        self._select(0)
        self._type("改过的名字")
        self.window.task_list.clearSelection()
        self.window.clear_editor()
        _APP.processEvents()
        self.assertEqual(self._state(), ("● 有未应用修改", "dirty"),
                         "取消选中后状态栏仍应提示有未应用修改")

    def test_apply_marks_saved(self):
        self._select(0)
        self._type("改过的名字")
        self.window.apply_button.click()
        _APP.processEvents()
        self.assertEqual(self._state(), ("● 已保存", "saved"))
        self.assertEqual(g.TASKS[0]["description"], "改过的名字")

    def test_unsaved_memory_change_shows_unsaved(self):
        g.TASKS.append({"id": "B", "type": "delay", "description": "未保存", "duration": 0, "enabled": True})
        self.window._refresh_save_state()
        self.assertEqual(self._state(), ("● 未保存", "unsaved"))
        self.assertIn("Ctrl+S", self.window.save_state_label.toolTip())

    def test_saving_clears_unsaved(self):
        g.TASKS.append({"id": "B", "type": "delay", "description": "未保存", "duration": 0, "enabled": True})
        self.window._refresh_save_state()
        self.assertEqual(self._state()[1], "unsaved")
        self.window.save_current_tasks()
        _APP.processEvents()
        self.assertEqual(self._state(), ("● 已保存", "saved"))
        self.assertIn("保存于", self.window.last_save_label.text())

    def test_auto_save_on_add_keeps_saved(self):
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("delay", True))):
            self.window.add_task()
        _APP.processEvents()
        self.assertEqual(self._state(), ("● 已保存", "saved"),
                         "新增步骤会自动保存，状态应保持已保存")

    def test_preset_switch_does_not_show_false_unsaved(self):
        g.TASKS.append({"id": "B", "type": "delay", "description": "未保存", "duration": 0, "enabled": True})
        self.window._refresh_save_state()
        self.assertEqual(self._state()[1], "unsaved")
        self.window.on_mode_selected("custom")
        _APP.processEvents()
        self.assertEqual(self._state(), ("● 已保存", "saved"),
                         "切换预设后加载的是已保存数据，不该显示未保存")

    def test_save_button_tooltip_mentions_auto_save(self):
        tooltip = self.window.save_button.toolTip()
        self.assertIn("Ctrl+S", tooltip)
        self.assertIn("自动保存", tooltip)


class SaveStateGuardTests(unittest.TestCase):
    def test_implementation_markers(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("def _refresh_save_state", source)
        self.assertIn("def _refresh_saved_snapshot", source)
        self.assertIn("def _has_unsaved_changes", source)
        self.assertIn("saveKind=\\\"saved\\\"", source)
        self.assertIn("saveKind=\\\"dirty\\\"", source)
        self.assertIn("saveKind=\\\"unsaved\\\"", source)
        self.assertIn("self._refresh_save_state()", source)


if __name__ == "__main__":
    unittest.main()
