# -*- coding: utf-8 -*-
"""M1：编辑器「未应用 / 已保存」状态胶囊（2026-10-27）。

所有者要求：在「类型: … | ID: …」同一行右侧，显示类似运行状态胶囊的 UI，
分别是「未应用」（改了字段但还没点应用修改）和「已保存」（已应用/刚加载）。
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


def _task(task_id, description, step_type="normal"):
    return {"id": task_id, "type": step_type, "description": description,
            "template": "1", "enabled": True}


class EditorStateIndicatorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = [_task("A", "第一步"), _task("B", "第二步", "delay")]
        self.window = g.PySide6ScriptWindow()
        self.window.show()
        _APP.processEvents()
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

    def _type_in(self, edit, text):
        edit.setText(text)
        edit.textEdited.emit(text)   # 模拟"用户输入"（setText 本身不发 textEdited）
        _APP.processEvents()

    # ---------- 胶囊本身 ----------

    def test_loaded_step_shows_saved(self):
        self.assertTrue(self.window.editor_state_label.isVisible())
        self.assertEqual(self.window.editor_state_label.text(), "已保存")
        self.assertEqual(self.window.editor_state_label.property("stateKind"), "saved")

    def test_pill_is_on_the_same_row_as_type_label(self):
        type_pos = self.window.selected_label.mapTo(
            self.window, self.window.selected_label.rect().topLeft())
        state_pos = self.window.editor_state_label.mapTo(
            self.window, self.window.editor_state_label.rect().topLeft())
        self.assertLessEqual(abs(type_pos.y() - state_pos.y()), 4, "状态胶囊应与「类型 / ID」在同一行")

    def test_pill_is_at_the_far_right_of_the_row(self):
        type_pos = self.window.selected_label.mapTo(
            self.window, self.window.selected_label.rect().topLeft())
        state_pos = self.window.editor_state_label.mapTo(
            self.window, self.window.editor_state_label.rect().topLeft())
        label_right = type_pos.x() + self.window.selected_label.width()
        self.assertGreater(state_pos.x(), label_right + 100,
                           "状态胶囊应贴在这一行的最右边（中间留伸缩空白）")

    def test_typing_marks_unsaved(self):
        self._type_in(self.window.description_edit, "改过的名字")
        self.assertEqual(self.window.editor_state_label.text(), "未应用")
        self.assertEqual(self.window.editor_state_label.property("stateKind"), "dirty")
        self.assertIn("应用修改", self.window.editor_state_label.toolTip())

    def test_checkbox_toggle_marks_unsaved(self):
        self.window.click_checkbox.toggle()
        _APP.processEvents()
        self.assertEqual(self.window.editor_state_label.text(), "未应用")

    def test_apply_marks_saved_again(self):
        self._type_in(self.window.description_edit, "改过的名字")
        self.assertEqual(self.window.editor_state_label.text(), "未应用")
        self.window.apply_button.click()
        _APP.processEvents()
        self.assertEqual(self.window.editor_state_label.text(), "已保存")
        self.assertEqual(g.TASKS[0].get("description"), "改过的名字")

    def test_switching_step_resets_to_saved(self):
        self._type_in(self.window.description_edit, "改过的名字")
        self._select(1)
        self.assertEqual(self.window.editor_state_label.text(), "已保存")
        self.assertEqual(self.window.editor_state_label.property("stateKind"), "saved")

    def test_clear_editor_hides_pill(self):
        self.window.clear_editor()
        _APP.processEvents()
        self.assertFalse(self.window.editor_state_label.isVisible())

    def test_group_selection_hides_pill(self):
        self.window.mode_group_metadata[self.window.mode_combo.currentText() or "custom"] = {
            "names": {"g1": "组1"}, "colors": {}, "parents": {"g1": None},
            "children": {"g1": []}, "order": ["g1"],
        }
        self.window.refresh_task_list()
        _APP.processEvents()
        group_item = self.window._group_items().get("g1")
        if group_item is None:
            self.skipTest("该夹具下没有分组行")
        self.window.task_list.clearSelection()
        group_item.setSelected(True)
        self.window.task_list.setCurrentItem(group_item)
        _APP.processEvents()
        self.assertFalse(self.window.editor_state_label.isVisible())


class EditorStateIndicatorGuardTests(unittest.TestCase):
    def test_signals_and_qss_exist(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("def _connect_editor_dirty_signals", source)
        self.assertIn('"description_edit", "template_edit"', source)
        self.assertIn('"#editorState[stateKind=\\"dirty\\"]', source)
        self.assertIn('"#editorState[stateKind=\\"saved\\"]', source)
        self.assertIn("def _set_editor_state", source)


if __name__ == "__main__":
    unittest.main()
