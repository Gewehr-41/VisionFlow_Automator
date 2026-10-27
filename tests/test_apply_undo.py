# -*- coding: utf-8 -*-
"""M2：「应用修改」也进入撤销栈（2026-10-27）。

之前只有结构操作（增删/拖动/分组等）会进历史；用户在编辑器里改字段并点「应用修改」后，
Ctrl+Z 会跳过这次字段修改、直接撤销更早的结构操作。现在：
  · 应用前先留快照，确认确实有改动才压栈（没改动的空应用不产生历史）；
  · Ctrl+Z 恢复字段旧值、Ctrl+Y 重做；
  · 与结构操作的历史按先后顺序交错，可以连续回退。
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


class ApplyUndoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = [{"id": "A", "type": "normal", "description": "原始名称",
                       "template": "1", "threshold": 0.8, "timeout": 5, "enabled": True}]
        self.window = g.PySide6ScriptWindow()
        self.window.show()
        _APP.processEvents()
        self.window.history.clear()
        self.window.redo_history.clear()
        self._select(0)

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def _select(self, index):
        self.window.task_list.clearSelection()
        item = self.window._find_task_item(index)
        item.setSelected(True)
        self.window.task_list.setCurrentItem(item)
        _APP.processEvents()

    def _apply(self, description=None, threshold=None):
        if description is not None:
            self.window.description_edit.setText(description)
            self.window.description_edit.textEdited.emit(description)   # 模拟用户输入
        if threshold is not None:
            self.window.threshold_edit.setText(str(threshold))
            self.window.threshold_edit.textEdited.emit(str(threshold))
        self.window.apply_button.click()
        _APP.processEvents()

    def test_apply_pushes_history_and_undo_restores_fields(self):
        self._apply(description="改过的名字", threshold=0.55)
        self.assertEqual(g.TASKS[0]["description"], "改过的名字")
        self.assertEqual(len(self.window.history), 1, "应用修改应压入撤销栈")
        self.window.undo()
        _APP.processEvents()
        self.assertEqual(g.TASKS[0]["description"], "原始名称")
        self.assertEqual(g.TASKS[0]["threshold"], 0.8)
        self.assertEqual(self.window.description_edit.text(), "原始名称", "编辑器也应恢复旧值")
        self.assertEqual(self.window.editor_state_label.text(), "已保存")
        self.assertEqual(len(self.window.redo_history), 1)

    def test_redo_reapplies_the_change(self):
        self._apply(description="改过的名字")
        self.window.undo()
        _APP.processEvents()
        self.window.redo()
        _APP.processEvents()
        self.assertEqual(g.TASKS[0]["description"], "改过的名字")
        self.assertEqual(self.window.description_edit.text(), "改过的名字")
        self.assertEqual(len(self.window.redo_history), 0)

    def test_noop_apply_does_not_push_history(self):
        history_before = len(self.window.history)
        self.window.apply_button.click()
        _APP.processEvents()
        self.assertEqual(len(self.window.history), history_before,
                         "没改动时点应用修改不应产生撤销记录")

    def test_multiple_applies_can_be_undone_one_by_one(self):
        self._apply(description="第一版")
        self._apply(description="第二版")
        self.assertEqual(len(self.window.history), 2)
        self.window.undo()
        _APP.processEvents()
        self.assertEqual(g.TASKS[0]["description"], "第一版")
        self.window.undo()
        _APP.processEvents()
        self.assertEqual(g.TASKS[0]["description"], "原始名称")
        self.window.redo()
        self.window.redo()
        _APP.processEvents()
        self.assertEqual(g.TASKS[0]["description"], "第二版")

    def test_structural_and_field_history_interleave(self):
        self._apply(description="先改字段")
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("delay", True))):
            self.window.add_task()
        _APP.processEvents()
        self.assertEqual(len(g.TASKS), 2)
        # 第一次撤销：撤掉新增步骤
        self.window.undo()
        _APP.processEvents()
        self.assertEqual(len(g.TASKS), 1)
        self.assertEqual(g.TASKS[0]["description"], "先改字段")
        # 第二次撤销：撤掉字段修改
        self.window.undo()
        _APP.processEvents()
        self.assertEqual(g.TASKS[0]["description"], "原始名称")


class ApplyUndoGuardTests(unittest.TestCase):
    def test_push_history_accepts_snapshot(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("def _push_history(self, snapshot=None):", source)
        self.assertIn("self._push_history((before_tasks, current_snapshot[1], current_snapshot[2]))", source)
        self.assertIn("before_tasks = deepcopy(TASKS)", source)


if __name__ == "__main__":
    unittest.main()
