# -*- coding: utf-8 -*-
"""绑定图片对话框的保存语义（2026-10-28 所有者要求）：

- 点「保存」退出：立即按「应用修改」路径写入步骤并持久化（不再需要第二次点应用修改）；
- 点「叉」退出：改动保留在编辑框里，状态显示「未应用」，点应用修改后才写入。
"""

import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QListWidget, QPushButton

import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g

_FIXTURE = [
    {"id": "bind-0", "type": "normal", "description": "绑定测试", "enabled": True,
     "template": "alpha", "click": True, "required": True},
]


class BindingImageSaveTests(unittest.TestCase):
    def setUp(self):
        self._msg_patches = [
            mock.patch.object(g.QMessageBox, "warning", return_value=None),
            mock.patch.object(g.QMessageBox, "information", return_value=None),
            mock.patch.object(g.QMessageBox, "critical", return_value=None),
        ]
        for patcher in self._msg_patches:
            patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = copy.deepcopy(_FIXTURE)
        self.window = g.PySide6ScriptWindow()
        self.window.show()
        self.mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_tasks[self.mode] = copy.deepcopy(_FIXTURE)
        _APP.processEvents()
        item = self.window._find_task_item(0)
        item.setSelected(True)
        self.window.task_list.setCurrentItem(item)
        _APP.processEvents()
        self.window.template_edit.setText("alpha")
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks = self._orig
        for patcher in self._msg_patches:
            patcher.stop()
        self._tmp.cleanup()

    def _open_dialog(self, add_item, click_save):
        def fake_exec(dialog_self):
            widget = dialog_self.findChild(QListWidget)
            if add_item:
                widget.addItem(add_item)
            if click_save:
                for button in dialog_self.findChildren(QPushButton):
                    if button.text() == "保存":
                        button.click()
                        break
            return 0

        with mock.patch.object(g.QDialog, "exec", fake_exec):
            self.window.select_template_file()

    def test_save_commits_to_task_and_persists(self):
        """点保存 -> 立即写入步骤并落盘，状态为已保存。"""
        self._open_dialog(add_item="beta", click_save=True)
        self.assertEqual(g.TASKS[0]["template"], "alpha, beta", "保存后应直接写入步骤")
        self.assertEqual(self.window.editor_state_label.text(), "已保存")
        self.assertEqual(self.window.editor_state_label.property("stateKind"), "saved")
        saved = tasks.load_tasks()
        self.assertEqual(saved[0].get("template"), "alpha, beta", "保存后应已持久化到磁盘")

    def test_close_with_x_keeps_pending_and_marks_dirty(self):
        """点叉退出 -> 改动保留在编辑框，状态未应用，步骤数据尚未写入。"""
        self._open_dialog(add_item="beta", click_save=False)
        self.assertEqual(self.window.template_edit.text(), "alpha, beta",
                         "叉掉对话框也应保留待应用的改动")
        self.assertEqual(g.TASKS[0]["template"], "alpha", "叉掉时不应直接写入步骤")
        self.assertEqual(self.window.editor_state_label.text(), "未应用")
        self.assertEqual(self.window.editor_state_label.property("stateKind"), "dirty")

        # 再点应用修改：待应用的绑定真正写入
        self.window.apply_selected_task()
        self.assertEqual(g.TASKS[0]["template"], "alpha, beta")
        self.assertEqual(self.window.editor_state_label.text(), "已保存")

    def test_close_without_changes_stays_saved(self):
        """没改动就点叉：不应误标为未应用。"""
        self._open_dialog(add_item=None, click_save=False)
        self.assertEqual(self.window.editor_state_label.text(), "已保存")


if __name__ == "__main__":
    unittest.main()
