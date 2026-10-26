# -*- coding: utf-8 -*-
"""项目列表区域：按钮撤除后的右键菜单（步骤 / 分组 / 空白三种上下文）与快捷键。

背景（所有者 2026-10-26）：先反馈"4 行按钮太臃肿"→ 改成两行按钮 + 工具菜单；
随后要求"右键菜单有的就不要放到项目列表上了"→ 编辑按钮**全部撤除**，
面板上只留右上角「工具 ▾」；同时补上分组缺失的右键功能。
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

TOOL_ENTRIES = ["打开蓝图流程", "识别调试", "兜底处理", "片段", "检查更新"]
GROUP_ENTRIES = ["编辑组设置", "重命名分组…", "更改分组颜色…", "在此组中新增步骤", "在此组中新增子组"]


def _task(description, group_id=None, **extra):
    task = {"id": description, "type": "normal", "description": description,
            "enabled": True, "template": description}
    if group_id:
        task["group_id"] = group_id
    task.update(extra)
    return task


class TaskPanelRightClickTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        self.window = g.PySide6ScriptWindow()
        self.window.show()
        _APP.processEvents()
        self.mode = self.window.mode_combo.currentText() or "custom"
        self.group_id = "g_test"
        self.window.mode_group_metadata[self.mode] = {
            "names": {self.group_id: "测试组"},
            "colors": {self.group_id: "#eab308"},
            "order": [self.group_id],
            "expanded": {},
        }
        g.TASKS[:] = [_task("步骤一", self.group_id), _task("步骤二", self.group_id)]
        self.window.refresh_task_list()
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    # ---------- 面板结构 ----------

    def test_edit_buttons_are_removed(self):
        self.assertEqual(self.window.task_buttons, [],
                         "右键菜单里已有的编辑动作不应再占用项目列表面板")

    def test_only_tool_button_remains(self):
        self.assertIs(self.window.tool_button.menu(), self.window.tool_menu)
        self.assertEqual(sorted(self.window.tool_actions), sorted(TOOL_ENTRIES))

    # ---------- 三种右键上下文 ----------

    def test_empty_area_menu_has_creation_actions(self):
        menu = self.window._build_task_list_menu(None)
        actions = {action.text(): action for action in menu.actions() if action.text()}
        for label in ("新增步骤", "新增组", "全选", "清空", "打开蓝图流程"):
            self.assertIn(label, actions)

    def test_step_menu_has_edit_actions_enabled(self):
        item = self.window._find_task_item(0)
        self.window.task_list.setCurrentItem(item)
        item.setSelected(True)
        menu = self.window._build_task_list_menu(item)
        actions = {action.text(): action for action in menu.actions() if action.text()}
        for label in ("复制", "删除", "上移", "下移"):
            self.assertTrue(actions[label].isEnabled(), label)
        self.assertNotIn("重命名分组…", actions)

    def test_group_menu_has_group_actions(self):
        group_item = self.window._group_items()[self.group_id]
        menu = self.window._build_task_list_menu(group_item)
        texts = [action.text() for action in menu.actions() if action.text()]
        for label in GROUP_ENTRIES:
            self.assertIn(label, texts)
        self.assertIn("删除该组及其步骤…", texts)
        self.assertTrue(any(text in ("折叠该组", "展开该组") for text in texts))
        self.assertNotIn("复制", texts, "分组上下文不该出现步骤专用的复制")

    def test_group_menu_toggle_action_works(self):
        group_item = self.window._group_items()[self.group_id]
        group_item.setExpanded(False)
        menu = self.window._build_task_list_menu(group_item)
        action = next(action for action in menu.actions() if action.text() == "展开该组")
        action.trigger()
        self.assertTrue(self.window._group_items()[self.group_id].isExpanded())

    def test_rename_group_updates_metadata_and_tasks(self):
        with mock.patch.object(g.QInputDialog, "getText", staticmethod(lambda *a, **k: ("新名字", True))):
            self.window.rename_group(self.group_id)
        metadata = self.window.mode_group_metadata[self.mode]
        self.assertEqual(metadata["names"][self.group_id], "新名字")
        self.assertTrue(all(task.get("group_name") == "新名字" for task in g.TASKS))
        self.assertIn("已重命名分组", self.window.log_box.toPlainText())

    def test_delete_group_from_menu_removes_tasks(self):
        group_item = self.window._group_items()[self.group_id]
        self.window.task_list.setCurrentItem(group_item)
        _APP.processEvents()
        with mock.patch.object(g.QMessageBox, "question",
                               return_value=g.QMessageBox.StandardButton.Yes):
            self.window.delete_selected_item()
        self.assertEqual(g.TASKS, [])

    def test_right_click_selects_the_item_first(self):
        self.window.current_task_index = -1
        item = self.window._find_task_item(1)
        rect = self.window.task_list.visualItemRect(item)
        fake_menu = mock.MagicMock()
        with mock.patch.object(self.window, "_build_task_list_menu",
                               lambda item=None: fake_menu):
            self.window.show_task_list_menu(rect.center())
        self.assertEqual(self.window.current_task_index, 1)
        self.assertTrue(fake_menu.exec.called)

    def test_group_right_click_switches_editor_to_group_settings(self):
        group_item = self.window._group_items()[self.group_id]
        rect = self.window.task_list.visualItemRect(group_item)
        fake_menu = mock.MagicMock()
        with mock.patch.object(self.window, "_build_task_list_menu",
                               lambda item=None: fake_menu):
            self.window.show_task_list_menu(rect.center())
        self.assertEqual(self.window.selected_label.text(), "组设置")

    # ---------- 快捷键 ----------

    def test_list_shortcuts_are_scoped_to_the_list(self):
        pairs = {(action.shortcut().toString(), action.shortcutContext())
                 for action in self.window.list_shortcuts}
        self.assertEqual({pair[0] for pair in pairs},
                         {"Del", "Ctrl+C", "Ctrl+A", "Ins", "Ctrl+Up", "Ctrl+Down"})
        self.assertTrue(all(pair[1] == Qt.ShortcutContext.WidgetShortcut for pair in pairs),
                        "快捷键必须限定在列表控件上，不能抢编辑框里的按键")


if __name__ == "__main__":
    unittest.main()
