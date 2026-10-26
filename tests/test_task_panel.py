# -*- coding: utf-8 -*-
"""项目列表按钮区瘦身（所有者 2026-10-26 反馈"已经 4 行，非常臃肿"）。

采用的方案：**两行编辑按钮 + 「工具」下拉菜单**，并补上列表右键菜单与主窗口快捷键。
按钮区从 4 行 13 个降到 2 行 8 个 + 1 个「工具 ▾」。
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

EDIT_BUTTONS = ["全选", "清空", "上移", "下移", "新增组", "新增步骤", "复制", "删除"]
TOOL_ENTRIES = ["打开蓝图流程", "识别调试", "兜底处理", "片段", "检查更新"]


def _task(description, **extra):
    task = {"id": description, "type": "normal", "description": description,
            "enabled": True, "template": description}
    task.update(extra)
    return task


class TaskPanelLayoutTests(unittest.TestCase):
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

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    # ---------- 按钮区 ----------

    def test_edit_buttons_are_two_rows_of_four(self):
        texts = [button.text() for button in self.window.task_buttons]
        self.assertEqual(texts, EDIT_BUTTONS, "编辑按钮固定 8 个（2 行 × 4）")
        # 网格列数：按钮都在同一 QGridLayout 里，列号应为 0~3（也就是最多 4 列）
        grid = self.window.task_button_grid
        columns = {grid.getItemPosition(grid.indexOf(button))[1] for button in self.window.task_buttons}
        self.assertEqual(columns, {0, 1, 2, 3}, "每行 4 个")
        rows = {grid.getItemPosition(grid.indexOf(button))[0] for button in self.window.task_buttons}
        self.assertEqual(rows, {0, 1}, "只剩两行（此前 13 个按钮是 4 行）")

    def test_tool_menu_contains_global_entries(self):
        self.assertEqual(sorted(self.window.tool_actions), sorted(TOOL_ENTRIES))
        self.assertIs(self.window.tool_button.menu(), self.window.tool_menu)

    # ---------- 右键菜单 ----------

    def test_list_uses_custom_context_menu(self):
        self.assertEqual(self.window.task_list.contextMenuPolicy(),
                         Qt.ContextMenuPolicy.CustomContextMenu)

    def test_menu_disables_actions_without_selection(self):
        g.TASKS[:] = []
        self.window.refresh_task_list()
        menu = self.window._build_task_list_menu()
        actions = {action.text(): action for action in menu.actions() if action.text()}
        self.assertIn("新增步骤", actions)
        self.assertIn("打开蓝图流程", actions)
        for label in ("复制", "删除", "上移", "下移"):
            self.assertFalse(actions[label].isEnabled(), f"无选中时「{label}」应禁用")
        self.assertFalse(actions["清空"].isEnabled(), "空列表时「清空」应禁用")

    def test_menu_enables_actions_with_selection(self):
        g.TASKS[:] = [_task("A"), _task("B")]
        self.window.refresh_task_list()
        self.window.task_list.setCurrentItem(self.window._find_task_item(0))
        self.window.task_list.topLevelItem(0).child(0).setSelected(True)
        menu = self.window._build_task_list_menu()
        actions = {action.text(): action for action in menu.actions() if action.text()}
        for label in ("复制", "删除", "上移", "下移"):
            self.assertTrue(actions[label].isEnabled(), f"有选中时「{label}」应可用")
        self.assertTrue(actions["清空"].isEnabled())

    def test_right_click_on_a_step_selects_it_first(self):
        g.TASKS[:] = [_task("A"), _task("B")]
        self.window.refresh_task_list()
        self.window.current_task_index = -1
        item = self.window._find_task_item(1)
        self.assertIsNotNone(item)
        rect = self.window.task_list.visualItemRect(item)
        point = rect.center()
        fake_menu = mock.MagicMock()
        with mock.patch.object(self.window, "_build_task_list_menu", lambda: fake_menu):
            self.window.show_task_list_menu(point)
        self.assertEqual(self.window.current_task_index, 1, "右键某一步应先把它选中，再弹菜单")
        self.assertTrue(fake_menu.exec.called)

    # ---------- 快捷键 ----------

    def test_list_shortcuts_are_scoped_to_the_list(self):
        pairs = {(action.shortcut().toString(), action.shortcutContext())
                 for action in self.window.list_shortcuts}
        expected = {("Del", Qt.ShortcutContext.WidgetShortcut),
                    ("Ctrl+C", Qt.ShortcutContext.WidgetShortcut),
                    ("Ctrl+A", Qt.ShortcutContext.WidgetShortcut),
                    ("Ins", Qt.ShortcutContext.WidgetShortcut),
                    ("Ctrl+Up", Qt.ShortcutContext.WidgetShortcut),
                    ("Ctrl+Down", Qt.ShortcutContext.WidgetShortcut)}
        self.assertEqual(pairs, expected, "快捷键必须限定在列表控件上，不能抢编辑框里的按键")

    def test_shortcut_actions_are_attached_to_the_list(self):
        attached = {action.shortcut().toString() for action in self.window.task_list.actions()}
        self.assertTrue({"Del", "Ctrl+C", "Ctrl+A", "Ins", "Ctrl+Up", "Ctrl+Down"} <= attached)


if __name__ == "__main__":
    unittest.main()
