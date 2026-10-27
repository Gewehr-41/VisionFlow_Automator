# -*- coding: utf-8 -*-
"""主界面 L1：预设管理收进「预设 ▾」下拉（2026-10-27）。

目标（对比 MaaEnd/MXU：主操作常驻、低频管理动作收进菜单）：
  · 原来 7 个预设管理按钮 → 1 个「预设 ▾」下拉（6 个管理动作）+ 1 个「保存当前任务」；
  · 运行控制保留 开始/从当前/暂停/停止 四个主按钮，「单步」降为次要样式；
  · 运行中「预设 ▾」与「保存」都要禁用（防止运行中改配置）；
  · 保存给 Ctrl+S 快捷键（提示里写了就要真的有）。
"""

import copy
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton, QToolButton  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class MainToolbarLayoutTests(unittest.TestCase):
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

    def test_preset_menu_has_all_management_actions(self):
        labels = [action.text() for action in self.window.preset_menu.actions() if action.text()]
        self.assertEqual(labels, ["新建预设", "重命名预设", "复制到预设", "导出预设", "导入预设", "删除预设"])

    def test_preset_entry_is_a_tool_button_not_seven_push_buttons(self):
        self.assertIsInstance(self.window.preset_menu_button, QToolButton)
        push_labels = [button.text() for button in self.window.findChildren(QPushButton)]
        for label in ("新建预设", "重命名预设", "复制到预设", "导出预设", "导入预设", "删除预设"):
            self.assertNotIn(label, push_labels, f"{label} 不应还是常驻按钮")

    def test_save_button_remains_visible(self):
        self.assertEqual(self.window.save_button.text(), "保存当前任务")
        self.assertTrue(self.window.save_button.isEnabled())

    def test_ctrl_s_shortcut_is_registered(self):
        self.assertEqual(self.window.save_shortcut.shortcut().toString(), "Ctrl+S")
        self.assertIn(self.window.save_shortcut, self.window.actions())

    def test_run_controls_stay(self):
        labels = [button.text() for button in
                  (self.window.start_button, self.window.start_current_button,
                   self.window.stop_button, self.window.pause_button, self.window.step_button)]
        self.assertEqual(labels, ["开始执行", "从当前步骤执行", "停止", "暂停", "单步"])

    def test_single_step_is_secondary_style(self):
        self.assertEqual(self.window.step_button.objectName(), "secondaryButton")

    def test_running_state_disables_preset_entries(self):
        self.window._set_running_state(True)
        try:
            self.assertFalse(self.window.preset_menu_button.isEnabled(), "运行中预设菜单应禁用")
            self.assertFalse(self.window.save_button.isEnabled(), "运行中保存按钮应禁用")
        finally:
            self.window._set_running_state(False)
        self.assertTrue(self.window.preset_menu_button.isEnabled())
        self.assertTrue(self.window.save_button.isEnabled())

    def test_tool_buttons_unchanged(self):
        self.assertEqual([button.text() for button in self.window.tool_buttons],
                         ["打开蓝图流程", "识别调试", "兜底处理", "片段", "检查更新"])


class L1GuardTests(unittest.TestCase):
    def test_no_leftover_preset_loop_buttons(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("self.preset_menu_button = QToolButton()", source)
        self.assertIn("preset_menu.addAction(label)", source)
        self.assertNotIn('("保存当前任务", self.save_current_tasks)', source,
                         "保存按钮应独立创建，不能再混在预设循环里")

    def test_qss_has_toolbutton_style(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn('"QToolButton { min-height: 26px;', source)
        self.assertIn('"#secondaryButton {', source)


if __name__ == "__main__":
    unittest.main()
