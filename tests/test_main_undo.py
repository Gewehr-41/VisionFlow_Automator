# -*- coding: utf-8 -*-
"""主窗口撤销/重做（2026-10-27）。

设计：快照式撤销——每个**结构编辑**动作执行前记录
`(TASKS 深拷贝, 分组元数据深拷贝, 当前选中下标)`，栈深 50；
`Ctrl+Z` 撤销、`Ctrl+Y` / `Ctrl+Shift+Z` 重做，右键菜单里也有入口。
切换预设会清空撤销栈（快照里的任务属于上一个预设）。
"""

import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(task_id, group_id=None):
    task = {"id": task_id, "type": "normal", "description": task_id, "template": "1", "enabled": True}
    if group_id:
        task["group_id"] = group_id
        task["group_name"] = group_id
    return task


class MainUndoBase(unittest.TestCase):
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
        self.mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[self.mode] = {
            "names": {"g1": "组1", "g2": "组2"},
            "colors": {"g1": "#111111", "g2": "#222222"},
            "parents": {"g1": None, "g2": None},
            "children": {"g1": [], "g2": []},
            "order": ["g1", "g2"],
        }
        g.TASKS[:] = [_task("A", "g1"), _task("B", "g1"), _task("C", "g2")]
        self.window.refresh_task_list()
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    # ---------- 工具 ----------

    def ids(self):
        return [task["id"] for task in g.TASKS]

    def select_step(self, index):
        self.window.task_list.clearSelection()
        item = self.window._find_task_item(index)
        item.setSelected(True)
        self.window.task_list.setCurrentItem(item)
        _APP.processEvents()

    def select_group(self, group_id):
        self.window.task_list.clearSelection()
        self.window._group_items()[group_id].setSelected(True)
        self.window.task_list.setCurrentItem(self.window._group_items()[group_id])
        _APP.processEvents()

    def add_step(self, step_type="delay"):
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: (step_type, True))):
            self.window.add_task()
        _APP.processEvents()


class UndoBasicTests(MainUndoBase):
    def test_undo_redo_add_step(self):
        self.select_step(0)
        before = self.ids()
        self.add_step()
        self.assertEqual(len(g.TASKS), 4)
        self.window.undo()
        self.assertEqual(self.ids(), before, "撤销新增步骤应恢复原列表")
        self.window.redo()
        self.assertEqual(len(g.TASKS), 4, "重做应把新增步骤加回来")

    def test_undo_delete_step(self):
        self.select_step(1)
        with mock.patch.object(g.QMessageBox, "question",
                               return_value=g.QMessageBox.StandardButton.Yes):
            self.window.delete_selected_item()
        self.assertEqual(self.ids(), ["A", "C"])
        self.window.undo()
        self.assertEqual(self.ids(), ["A", "B", "C"])

    def test_undo_copy_step(self):
        self.select_step(0)
        self.window.copy_selected_item()
        self.assertEqual(len(g.TASKS), 4)
        self.window.undo()
        self.assertEqual(len(g.TASKS), 3)
        self.assertEqual(self.ids(), ["A", "B", "C"])

    def test_undo_move_step(self):
        self.select_step(1)
        self.window.move_selected_item(-1)
        self.assertEqual(self.ids(), ["B", "A", "C"])
        self.window.undo()
        self.assertEqual(self.ids(), ["A", "B", "C"])

    def test_undo_move_group_restores_metadata(self):
        self.select_group("g2")
        self.window.move_selected_item(-1)
        self.assertEqual(self.window.mode_group_metadata[self.mode]["order"], ["g2", "g1"])
        self.window.undo()
        self.assertEqual(self.window.mode_group_metadata[self.mode]["order"], ["g1", "g2"],
                         "撤销组移动要恢复分组顺序")
        self.assertEqual(self.ids(), ["A", "B", "C"], "执行顺序也要恢复")

    def test_undo_delete_group_restores_tasks_and_metadata(self):
        self.select_group("g1")
        with mock.patch.object(g.QMessageBox, "question",
                               return_value=g.QMessageBox.StandardButton.Yes):
            self.window.delete_selected_item()
        self.assertEqual(self.ids(), ["C"])
        self.assertNotIn("g1", self.window.mode_group_metadata[self.mode]["names"])
        self.window.undo()
        self.assertEqual(self.ids(), ["A", "B", "C"])
        self.assertIn("g1", self.window.mode_group_metadata[self.mode]["names"])

    def test_undo_rename_group(self):
        with mock.patch.object(g.QInputDialog, "getText", staticmethod(lambda *a, **k: ("新名字", True))):
            self.window.rename_group("g1")
        self.assertEqual(self.window.mode_group_metadata[self.mode]["names"]["g1"], "新名字")
        self.window.undo()
        self.assertEqual(self.window.mode_group_metadata[self.mode]["names"]["g1"], "组1")

    def test_undo_insert_snippet(self):
        self.window.snippets = {"片段": [_task("S")]}
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("片段", True))):
            self.window.insert_snippet_at_selection()
        self.assertEqual(len(g.TASKS), 4)
        self.window.undo()
        self.assertEqual(self.ids(), ["A", "B", "C"])

    def test_redo_cleared_by_new_edit(self):
        self.select_step(0)
        self.add_step()
        self.window.undo()
        self.assertEqual(len(g.TASKS), 3)
        self.add_step()
        self.window.redo()   # 新编辑后重做栈应被清空
        self.assertEqual(len(g.TASKS), 4, "重做栈已清空，redo 不应再加一步")

    def test_undo_without_history_is_safe(self):
        self.window.undo()
        self.assertIn("没有可撤销的操作", self.window.log_box.toPlainText())

    def test_switch_mode_clears_history(self):
        self.select_step(0)
        self.add_step()
        self.assertTrue(self.window.history)
        self.window.on_mode_selected("custom")
        _APP.processEvents()
        self.assertEqual(self.window.history, [])


class UndoShortcutTests(MainUndoBase):
    def test_shortcuts_registered(self):
        sequences = {action.shortcut().toString() for action in self.window.list_shortcuts}
        self.assertIn("Ctrl+Z", sequences)
        self.assertIn("Ctrl+Y", sequences)
        self.assertIn("Ctrl+Shift+Z", sequences)

    def test_shortcuts_are_scoped_to_list(self):
        for action in self.window.list_shortcuts:
            self.assertEqual(action.shortcutContext(), Qt.ShortcutContext.WidgetShortcut)

    def test_context_menu_has_undo_redo(self):
        menu = self.window._build_task_list_menu(None)
        actions = {action.text(): action for action in menu.actions() if action.text()}
        self.assertIn("撤销", actions)
        self.assertIn("重做", actions)
        self.assertFalse(actions["撤销"].isEnabled(), "没有历史时撤销应置灰")
        self.select_step(0)
        self.add_step()
        menu = self.window._build_task_list_menu(None)
        actions = {action.text(): action for action in menu.actions() if action.text()}
        self.assertTrue(actions["撤销"].isEnabled(), "有历史后撤销应可用")

    def test_undo_action_works_from_menu(self):
        self.select_step(0)
        self.add_step()
        menu = self.window._build_task_list_menu(None)
        action = next(action for action in menu.actions() if action.text() == "撤销")
        action.trigger()
        _APP.processEvents()
        self.assertEqual(len(g.TASKS), 3)


if __name__ == "__main__":
    unittest.main()
