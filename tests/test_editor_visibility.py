# -*- coding: utf-8 -*-
"""右侧编辑面板的显隐（§8.1 #80）：

  · "步骤名称 / 启用状态 / 多模板时"整块只在**选中步骤**时出现——
    未选中步骤、选中分组时都应收起（此前这一块是裸 QFormLayout 挂在布局上，
    没有控件句柄，清空面板与组设置时藏不掉）；
  · 「多模板时」只对真正按模板识别、且会按策略挑图的步骤（normal/advanced）显示，
    延迟 / 按键 / 移动 / 拖曳 / 持续点击等步骤不显示它。

两个窗口共用同一套面板构造与显隐逻辑，所以两边都要满足。
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


def make_task(step_type="normal", description="测试步骤"):
    return {"id": f"t-{step_type}", "type": step_type, "description": description,
            "enabled": True, "template": "1", "click": True, "required": True,
            "group_id": GROUP_ID}


class EditorVisibilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_presets = tasks.PRESETS_FILE
        self._orig_tasks_file = tasks.TASKS_FILE
        self._orig_task_list = copy.deepcopy(g.TASKS)
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        self._windows = []

    def tearDown(self):
        for window in self._windows:
            window.close()
        g.TASKS[:] = self._orig_task_list
        tasks.PRESETS_FILE = self._orig_presets
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()

    # ---------- 夹具 ----------

    def _main_window(self, task):
        window = g.PySide6ScriptWindow()
        self._windows.append(window)
        self.mode = window.mode_combo.currentText() or "custom"
        g.TASKS[:] = [copy.deepcopy(task), copy.deepcopy(task)]
        window.mode_group_metadata[self.mode] = {
            "names": {GROUP_ID: "组一"},
            "order": [GROUP_ID],
            "expanded": {},
        }
        window.refresh_task_list()
        window.select_task_index(0)
        _APP.processEvents()
        return window

    def _blueprint_window(self, task):
        window = g.BlueprintWindow([copy.deepcopy(task), copy.deepcopy(task)], {},
                                   lambda *a, **k: None, {}, None, {})
        self._windows.append(window)
        window._select_node(0)
        _APP.processEvents()
        return window

    def _both(self, task):
        return (("主窗口", self._main_window(task)),
                ("蓝图窗口", self._blueprint_window(task)))

    def _visible(self, window):
        return {
            "name_block": window.name_block.isVisibleTo(window),
            "description": window.description_edit.isVisibleTo(window),
            "enabled": window.enabled_checkbox.isVisibleTo(window),
            "strategy_label": window.strategy_label.isVisibleTo(window),
            "strategy_combo": window.match_strategy_combo.isVisibleTo(window),
        }

    # ---------- 用例 ----------

    def test_selected_step_shows_the_name_block(self):
        for label, window in self._both(make_task("normal")):
            with self.subTest(window=label):
                visible = self._visible(window)
                self.assertTrue(visible["name_block"], f"{label}：选中步骤应显示名称/启用状态")
                self.assertTrue(visible["description"])
                self.assertTrue(visible["enabled"])

    def test_recognition_step_shows_the_match_strategy(self):
        for label, window in self._both(make_task("normal")):
            with self.subTest(window=label):
                visible = self._visible(window)
                self.assertTrue(visible["strategy_label"], f"{label}：识别型步骤应显示「多模板时」")
                self.assertTrue(visible["strategy_combo"])

    def test_steps_without_template_strategy_hide_the_combo(self):
        for step_type in ("delay", "key_press", "keyboard_move", "drag", "click_until_gone"):
            for label, window in self._both(make_task(step_type)):
                with self.subTest(window=label, step_type=step_type):
                    visible = self._visible(window)
                    self.assertTrue(visible["name_block"], f"{step_type}：名称/启用状态仍应显示")
                    self.assertTrue(visible["enabled"], f"{step_type}：启用步骤仍应显示")
                    self.assertFalse(visible["strategy_label"],
                                     f"{step_type}：不该显示「多模板时」")
                    self.assertFalse(visible["strategy_combo"],
                                     f"{step_type}：不该显示「多模板时」下拉框")
                    window.close()

    def test_group_selection_hides_the_name_block(self):
        for label, window in (("主窗口", self._main_window(make_task())),
                              ("蓝图窗口", self._blueprint_window(make_task()))):
            with self.subTest(window=label):
                window._load_editor_for_group(GROUP_ID)
                _APP.processEvents()
                visible = self._visible(window)
                self.assertFalse(visible["name_block"], f"{label}：组设置不该显示步骤名称/启用状态")
                self.assertFalse(visible["description"])
                self.assertFalse(visible["enabled"])
                self.assertFalse(visible["strategy_combo"])

    def test_no_selection_hides_the_name_block(self):
        window = self._main_window(make_task())
        window.clear_editor()
        _APP.processEvents()
        visible = self._visible(window)
        self.assertFalse(visible["name_block"], "未选择步骤时不该显示步骤名称/启用状态")
        self.assertFalse(visible["strategy_combo"])

    def test_switching_back_from_group_to_step_restores_the_block(self):
        window = self._main_window(make_task())
        group_item = window.task_list.topLevelItem(0)
        step_item = window._find_task_item(0)

        window.load_selected_task(group_item, None)     # 真实槽：点组 → 组设置
        _APP.processEvents()
        self.assertFalse(self._visible(window)["name_block"])

        window.load_selected_task(step_item, None)      # 真实槽：再点步骤 → 步骤面板
        _APP.processEvents()
        self.assertTrue(self._visible(window)["name_block"], "从组设置切回步骤时应重新显示")
        self.assertTrue(self._visible(window)["strategy_combo"], "识别型步骤应重新显示「多模板时」")


if __name__ == "__main__":
    unittest.main()
