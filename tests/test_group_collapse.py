# -*- coding: utf-8 -*-
"""分组折叠状态不该被"程序化选中步骤"破坏（§8.1 #79）。

所有者 2026-10-26 反馈：4 个组、折叠上面 3 个，点最后一个组里步骤的启用勾选框，
**每点一次第 1 组就自动展开**。

真因（实测确认）：点勾选框 → itemChanged → 0ms 后 refresh_task_list() 重建树 →
最后由 select_task_index() 调 setCurrentItem()；QTreeView 为了让"当前项"可见，
会在内部 scrollTo 时**展开被折叠的祖先组**。更糟的是这次展开发生在
`blockSignals(False)` 之后，会触发 itemExpanded → 把"展开"写回分组元数据并落盘，
于是折叠状态被真的改坏（实测元数据从 `{'g1': False}` 变成 `{'g1': True}`）。

修法：select_task_index 内部先快照各组的展开状态、期间禁止写回元数据，
选完再按快照恢复。
"""

import copy
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])

GROUP_IDS = ["g1", "g2", "g3", "g4"]


class GroupCollapsePersistenceTests(unittest.TestCase):
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

        self.window = g.PySide6ScriptWindow()
        self.window.show()        # 关键：真实窗口才会走 QTreeView 的 scrollTo 逻辑
        _APP.processEvents()
        self.mode = self.window.mode_combo.currentText() or "custom"
        self._install_four_groups()
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._orig_task_list
        tasks.PRESETS_FILE = self._orig_presets
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()

    # ---------- 夹具 ----------

    def _install_four_groups(self):
        g.TASKS[:] = [
            {"id": f"{gid}-{index}", "type": "normal", "description": f"{gid} 步骤{index}",
             "enabled": True, "template": "1", "click": True, "required": True,
             "group_id": gid}
            for gid in GROUP_IDS for index in range(2)
        ]
        self.window.mode_group_metadata[self.mode] = {
            "names": {gid: gid for gid in GROUP_IDS},
            "order": list(GROUP_IDS),
            "expanded": {},
        }
        self.window.refresh_task_list()

    def _groups(self):
        return self.window._group_items()

    def _collapse_first_three(self):
        for gid in GROUP_IDS[:3]:
            self._groups()[gid].setExpanded(False)
        _APP.processEvents()
        self.assertEqual(self._groups()["g1"].isExpanded(), False)

    def _expanded_map(self):
        return {gid: self._groups()[gid].isExpanded() for gid in GROUP_IDS}

    def _metadata_expanded(self):
        return dict(self.window.mode_group_metadata[self.mode].get("expanded") or {})

    # ---------- 用例 ----------

    def test_refresh_keeps_the_collapsed_state_of_the_current_group(self):
        self.window.select_task_index(0)      # 当前步骤落在第 1 组里
        _APP.processEvents()
        self._collapse_first_three()

        self.window.refresh_task_list()
        _APP.processEvents()

        self.assertEqual(self._expanded_map(),
                         {"g1": False, "g2": False, "g3": False, "g4": True},
                         "重建列表不该把折叠的组展开（当前选中项在 g1 里）")

    def test_toggling_a_step_in_the_last_group_does_not_expand_the_first(self):
        """所有者反馈的原始场景：折叠前 3 组，点第 4 组里步骤的勾选框。"""
        self.window.select_task_index(0)
        _APP.processEvents()
        self._collapse_first_three()
        metadata_before = self._metadata_expanded()

        step = self._groups()["g4"].child(0)
        step.setCheckState(0, Qt.CheckState.Unchecked)
        _APP.processEvents()
        _APP.processEvents()      # 让 0ms 的 _run_task_list_commit 跑完

        self.assertEqual(self._expanded_map(),
                         {"g1": False, "g2": False, "g3": False, "g4": True},
                         "点第 4 组的勾选框不该展开第 1 组")
        self.assertEqual(self._metadata_expanded(), metadata_before,
                         "折叠状态不能被这次刷新写坏（元数据应与操作前一致）")
        self.assertFalse(g.TASKS[6].get("enabled"), "勾选框本身应当真的生效")

    def test_selecting_a_step_inside_a_collapsed_group_keeps_it_collapsed(self):
        self._collapse_first_three()
        metadata_before = self._metadata_expanded()

        self.window.select_task_index(1)      # g1 里的第二个步骤
        _APP.processEvents()

        self.assertFalse(self._groups()["g1"].isExpanded(), "选中折叠组内的步骤不该展开该组")
        self.assertEqual(self._metadata_expanded(), metadata_before, "元数据不该被改写")

    def test_clicking_the_last_group_checkbox_keeps_other_groups_collapsed(self):
        self._collapse_first_three()
        metadata_before = self._metadata_expanded()

        group = self._groups()["g4"]
        group.setCheckState(0, Qt.CheckState.Unchecked)
        _APP.processEvents()
        _APP.processEvents()

        self.assertEqual(self._expanded_map(),
                         {"g1": False, "g2": False, "g3": False, "g4": True},
                         "整组开关也不该展开其它组")
        self.assertEqual(self._metadata_expanded(), metadata_before)
        self.assertTrue(all(not task.get("enabled") for task in g.TASKS[6:8]),
                        "第 4 组里的步骤应被整体禁用")


if __name__ == "__main__":
    unittest.main()
