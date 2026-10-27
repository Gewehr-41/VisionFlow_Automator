# -*- coding: utf-8 -*-
"""M4 / M6（2026-10-27）。

M4：主界面 `Ctrl+C` 改成"复制到剪贴板"（不再原地生成副本），新增 `Ctrl+V` 粘贴，
    与蓝图窗口语义一致；粘贴插到选中项之后，含分组结构时生成新组 id / 名称加「副本」。
M6：日志面板最多保留 5000 行（不再无限增长）+ 「清空日志」按钮。
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

GROUP_ID = "g1"
CHILD_ID = "g2"


def _task(task_id, description, group_id=None):
    task = {"id": task_id, "type": "normal", "description": description,
            "template": "1", "enabled": True}
    if group_id:
        task["group_id"] = group_id
        task["group_name"] = group_id
    return task


class CopyPasteTests(unittest.TestCase):
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
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_group_metadata[mode] = {
            "names": {GROUP_ID: "主组", CHILD_ID: "子组"},
            "colors": {GROUP_ID: "#eab308", CHILD_ID: "#38bdf8"},
            "parents": {GROUP_ID: None, CHILD_ID: GROUP_ID},
            "children": {GROUP_ID: [CHILD_ID], CHILD_ID: []},
            "order": [GROUP_ID],
        }
        g.TASKS[:] = [_task("A", "第一步", GROUP_ID), _task("B", "第二步", CHILD_ID), _task("C", "第三步")]
        self.window.task_clipboard = None
        self.window.refresh_task_list()
        _APP.processEvents()

    def _select(self, index):
        self.window.task_list.clearSelection()
        item = self.window._find_task_item(index)
        item.setSelected(True)
        self.window.task_list.setCurrentItem(item)
        _APP.processEvents()

    def test_copy_does_not_modify_tasks(self):
        self._select(0)
        before = copy.deepcopy(g.TASKS)
        self.window.copy_selected_item()
        _APP.processEvents()
        self.assertEqual([task["id"] for task in g.TASKS], [task["id"] for task in before],
                         "Ctrl+C 只复制到剪贴板，不应改列表")
        self.assertEqual(len(self.window.task_clipboard["tasks"]), 1)
        self.assertIn("Ctrl+V", self.window.log_box.toPlainText())

    def test_paste_inserts_after_selection_with_new_ids(self):
        self._select(0)
        self.window.copy_selected_item()
        self.window.paste_clipboard_item()
        _APP.processEvents()
        self.assertEqual(len(g.TASKS), 4, "粘贴应新增 1 个步骤")
        self.assertEqual(g.TASKS[1]["description"], "第一步 副本")
        self.assertNotEqual(g.TASKS[1]["id"], "A", "副本应使用新 id")

    def test_paste_with_empty_clipboard_is_noop(self):
        before = len(g.TASKS)
        self.window.paste_clipboard_item()
        _APP.processEvents()
        self.assertEqual(len(g.TASKS), before)
        self.assertIn("剪贴板是空的", self.window.log_box.toPlainText())

    def test_copy_group_then_paste_clones_group_structure(self):
        self.window.task_list.clearSelection()
        self.window._group_items()[GROUP_ID].setSelected(True)
        self.window.copy_selected_item()
        before = len(g.TASKS)
        self.window.paste_clipboard_item()
        _APP.processEvents()
        self.assertEqual(len(g.TASKS), before + 2, "组分组的 2 个步骤都要复制")
        mode = self.window.mode_combo.currentText() or "custom"
        metadata = self.window.mode_group_metadata[mode]
        copied_groups = [gid for gid, name in metadata["names"].items() if name.endswith("副本")]
        self.assertEqual(len(copied_groups), 2, "分组结构（主组 + 子组）要一起复制")
        top = next(gid for gid in copied_groups if metadata["parents"][gid] is None)
        child = next(gid for gid in copied_groups if gid != top)
        self.assertEqual(metadata["parents"][child], top, "嵌套关系要保持在副本内部")

    def test_ctrl_v_shortcut_is_registered(self):
        shortcuts = {action.shortcut().toString() for action in self.window.list_shortcuts}
        self.assertIn("Ctrl+C", shortcuts)
        self.assertIn("Ctrl+V", shortcuts)

    def test_paste_can_be_undone(self):
        self._select(0)
        self.window.copy_selected_item()
        self.window.paste_clipboard_item()
        _APP.processEvents()
        self.assertEqual(len(g.TASKS), 4)
        self.window.undo()
        _APP.processEvents()
        self.assertEqual(len(g.TASKS), 3, "粘贴应进撤销栈")


class LogPanelTests(unittest.TestCase):
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

    def test_log_has_line_limit(self):
        self.assertEqual(self.window.log_box.maximumBlockCount(), 5000, "日志应有 5000 行上限")

    def test_log_does_not_grow_beyond_limit(self):
        for index in range(5100):
            self.window.log_box.appendPlainText(f"x{index}")
        self.assertLessEqual(self.window.log_box.blockCount(), 5000,
                             "超过上限后应丢弃最旧的日志行")

    def test_clear_log_button_is_after_about_in_status_bar(self):
        from PySide6.QtWidgets import QWidget

        window = self.window
        self.assertIsInstance(window.clear_log_button.parent(), QWidget)
        self.assertTrue(window.status_bar_widget.isAncestorOf(window.clear_log_button),
                        "「清空日志」应放在底部状态栏")
        self.assertLess(window.about_button.x(), window.clear_log_button.x(),
                        "「清空日志」应排在「关于」后面")

    def test_clear_log_button_clears_panel(self):
        self.window.append_log("待清空的日志")
        _APP.processEvents()
        self.assertIn("待清空的日志", self.window.log_box.toPlainText())
        self.window.clear_log_button.click()
        _APP.processEvents()
        self.assertEqual(self.window.log_box.toPlainText(), "", "点「清空日志」应清空面板")


class CopyPasteLogGuardTests(unittest.TestCase):
    def test_implementation_markers(self):
        task_source = open("task_list_manager.py", encoding="utf-8").read()
        gui_source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("def paste_clipboard_item", task_source)
        self.assertIn("self.task_clipboard = {", task_source)
        self.assertIn('("Ctrl+V", self.paste_clipboard_item)', gui_source)
        self.assertIn("setMaximumBlockCount(5000)", gui_source)
        self.assertIn("def clear_log", gui_source)
        self.assertIn("清空日志", gui_source)


if __name__ == "__main__":
    unittest.main()
