# -*- coding: utf-8 -*-
"""主界面交互修复回归测试（2026-10-27 审计后）：

  · 父组勾选要级联到子组里的步骤
  · 组可以新增子组 / 上移下移 / 双击折叠 / 拖动改变层级
  · 步骤可以拖进组（含空组）、拖出到根层、拖到别的组的步骤上改归属
  · 复制支持多选与分组；片段只有分组也能插入
  · persist_task_order 不能再丢嵌套组里的步骤
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

GROUP_ID = "g1"
CHILD_ID = "g2"
OTHER_ID = "g3"


def _task(task_id, group_id=None):
    task = {"id": task_id, "type": "normal", "description": task_id, "enabled": True, "template": "1"}
    if group_id:
        task["group_id"] = group_id
        task["group_name"] = group_id
    return task


class MainInteractionBase(unittest.TestCase):
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
            "names": {GROUP_ID: "父组", CHILD_ID: "子组", OTHER_ID: "另一组"},
            "colors": {GROUP_ID: "#111111", CHILD_ID: "#222222", OTHER_ID: "#333333"},
            "parents": {GROUP_ID: None, CHILD_ID: GROUP_ID, OTHER_ID: None},
            "children": {GROUP_ID: [CHILD_ID], CHILD_ID: [], OTHER_ID: []},
            "order": [GROUP_ID, OTHER_ID],
        }
        g.TASKS[:] = [_task("A", GROUP_ID), _task("B", CHILD_ID), _task("C", OTHER_ID)]
        self.window.refresh_task_list()
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    # ---------- 工具 ----------

    def group_item(self, group_id):
        return self.window._group_items().get(group_id)

    def metadata(self):
        return self.window.mode_group_metadata[self.mode]

    def tree_texts(self):
        lines = []

        def walk(item, depth):
            kind = "G" if item.data(0, Qt.UserRole) == "group" else "T"
            lines.append("  " * depth + f"[{kind}] {item.text(0)}")
            for i in range(item.childCount()):
                walk(item.child(i), depth + 1)

        for i in range(self.window.task_list.topLevelItemCount()):
            walk(self.window.task_list.topLevelItem(i), 0)
        return lines


class GroupCheckCascadeTests(MainInteractionBase):
    def test_parent_group_uncheck_cascades_to_child_group_steps(self):
        item = self.group_item(GROUP_ID)
        self.window.task_list.blockSignals(True)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        self.window.task_list.blockSignals(False)
        self.window.on_item_changed(item, 0)
        _APP.processEvents()
        enabled = {task["description"]: task["enabled"] for task in g.TASKS}
        self.assertFalse(enabled["A"], "父组自己的步骤应被停用")
        self.assertFalse(enabled["B"], "子组里的步骤也要被停用（此前不级联）")
        self.assertTrue(enabled["C"], "不相关的组不受影响")

    def test_child_group_checkbox_is_synced(self):
        item = self.group_item(GROUP_ID)
        self.window.task_list.blockSignals(True)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        self.window.task_list.blockSignals(False)
        self.window.on_item_changed(item, 0)
        _APP.processEvents()
        self.assertEqual(self.group_item(CHILD_ID).checkState(0), Qt.CheckState.Unchecked)


class GroupOperationTests(MainInteractionBase):
    def test_add_group_with_parent_creates_child(self):
        self.window.add_group(GROUP_ID)
        parents = self.metadata()["parents"]
        new_ids = [gid for gid in parents if gid not in (GROUP_ID, CHILD_ID, OTHER_ID)]
        self.assertEqual(len(new_ids), 1)
        self.assertEqual(parents[new_ids[0]], GROUP_ID, "「在此组中新增子组」应挂在当前组下")
        self.assertIn(new_ids[0], self.metadata()["children"][GROUP_ID])

    def test_group_names_are_unique(self):
        self.window.add_group()
        self.window.add_group()
        names = list(self.metadata()["names"].values())
        self.assertEqual(len(names), len(set(names)), "新增组名不能重复")

    def test_group_menu_add_subgroup_action_uses_parent(self):
        item = self.group_item(GROUP_ID)
        menu = self.window._build_task_list_menu(item)
        action = next(a for a in menu.actions() if a.text() == "在此组中新增子组")
        action.trigger()
        _APP.processEvents()
        parents = self.metadata()["parents"]
        new_ids = [gid for gid in parents if gid not in (GROUP_ID, CHILD_ID, OTHER_ID)]
        self.assertEqual(parents[new_ids[0]], GROUP_ID)

    def test_move_group_up_and_down(self):
        self.window.task_list.clearSelection()
        self.group_item(OTHER_ID).setSelected(True)
        self.window.move_selected_item(-1)
        self.assertEqual(self.metadata()["order"], [OTHER_ID, GROUP_ID])
        top = [self.window.task_list.topLevelItem(i).text(0)
               for i in range(self.window.task_list.topLevelItemCount())]
        self.assertLess(top.index("分组: 另一组"), top.index("分组: 父组"), "界面顺序要跟着变")
        self.window.move_selected_item(1)
        self.assertEqual(self.metadata()["order"], [GROUP_ID, OTHER_ID])

    def test_double_click_group_toggles_collapse(self):
        item = self.group_item(GROUP_ID)
        item.setExpanded(False)
        _APP.processEvents()
        self.window._on_task_item_double_clicked(item, 0)
        _APP.processEvents()
        self.assertTrue(self.group_item(GROUP_ID).isExpanded())
        self.assertIs(self.metadata().get("expanded", {}).get(GROUP_ID), True)

    def test_delete_group_only_registers_child_in_order(self):
        with mock.patch.object(g.QMessageBox, "question",
                               return_value=g.QMessageBox.StandardButton.Yes):
            self.window.delete_group_only(GROUP_ID)
        _APP.processEvents()
        metadata = self.metadata()
        self.assertEqual(metadata["parents"].get(CHILD_ID), None)
        self.assertIn(CHILD_ID, metadata["order"], "子组变顶层后要登记进 order")

    def test_add_task_uses_current_step_group(self):
        item = self.window._find_task_item(0)   # A 在父组
        self.window.task_list.setCurrentItem(item)
        _APP.processEvents()
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("delay", True))):
            self.window.add_task()
        created = [task for task in g.TASKS if task.get("type") == "delay"]
        self.assertEqual(len(created), 1)
        self.assertEqual(str(created[0].get("group_id")), GROUP_ID,
                         "选中组内步骤后新增的步骤应跟着进同一个组")


class DragDropTests(MainInteractionBase):
    def test_move_task_into_empty_group(self):
        self.metadata()["names"]["empty"] = "空组"
        self.metadata()["parents"]["empty"] = None
        self.metadata()["children"]["empty"] = []
        self.metadata()["order"].append("empty")
        self.window.refresh_task_list()
        _APP.processEvents()
        self.window.move_task_into_group(0, "empty")   # 拖 A 到空组
        task = next(task for task in g.TASKS if task["id"] == "A")
        self.assertEqual(task["group_id"], "empty")
        self.assertIn(task["id"], [item.data(0, Qt.UserRole) for item in
                                   (self.window._find_task_item(i) for i in range(len(g.TASKS)))
                                   if item is not None])

    def test_move_task_to_root(self):
        self.window.move_task_to_root(1)     # B 拖到空白处
        task = next(task for task in g.TASKS if task["id"] == "B")
        self.assertNotIn("group_id", task)
        self.assertNotIn("group_name", task)

    def test_drop_on_other_group_step_changes_group(self):
        # 把 A（父组）拖到 C（另一组）后面
        self.window.reorder_task_from_tree(0, 2, True, OTHER_ID)
        task = next(task for task in g.TASKS if task["id"] == "A")
        self.assertEqual(task["group_id"], OTHER_ID, "拖到别的组的步骤上应改变归属")
        self.assertEqual(g.TASKS[-1]["id"], "A", "并插到目标步骤之后")

    def test_reparent_group_into_another_group(self):
        self.window.reparent_group(OTHER_ID, GROUP_ID)
        metadata = self.metadata()
        self.assertEqual(metadata["parents"][OTHER_ID], GROUP_ID)
        self.assertIn(OTHER_ID, metadata["children"][GROUP_ID])
        self.assertNotIn(OTHER_ID, metadata["order"])

    def test_reparent_group_rejects_cycle(self):
        before = copy.deepcopy(self.metadata())
        self.window.reparent_group(GROUP_ID, CHILD_ID)   # 父组拖进自己的子组里
        self.assertEqual(self.metadata()["parents"][GROUP_ID], before["parents"][GROUP_ID],
                         "不能把组挂到自己的后代里（会成环）")

    def test_reparent_group_to_root(self):
        self.window.reparent_group(CHILD_ID, None)
        metadata = self.metadata()
        self.assertIsNone(metadata["parents"][CHILD_ID])
        self.assertIn(CHILD_ID, metadata["order"])


class CopyAndSnippetTests(MainInteractionBase):
    def test_copy_multiple_steps(self):
        self.window.task_list.clearSelection()
        for index in (0, 1):
            self.window._find_task_item(index).setSelected(True)
        before = len(g.TASKS)
        self.window.copy_selected_item()
        self.assertEqual(len(g.TASKS), before + 2, "多选复制应复制全部选中步骤")
        self.assertEqual([task["description"] for task in g.TASKS[2:4]], ["A 副本", "B 副本"])

    def test_copy_group_copies_steps_and_group_metadata(self):
        self.window.task_list.clearSelection()
        self.group_item(GROUP_ID).setSelected(True)
        before = len(g.TASKS)
        self.window.copy_selected_item()
        _APP.processEvents()
        self.assertEqual(len(g.TASKS), before + 2, "父组 + 子组里的 2 个步骤都要复制")
        metadata = self.metadata()
        new_groups = [gid for gid, name in metadata["names"].items() if name.endswith("副本")]
        self.assertEqual(len(new_groups), 2, "父组与子组各复制一份（含嵌套关系）")
        top = next(gid for gid in new_groups if metadata["parents"][gid] is None)
        child = next(gid for gid in new_groups if gid != top)
        self.assertEqual(metadata["parents"][child], top, "副本内部嵌套关系要保留")

    def test_insert_group_only_snippet_creates_groups(self):
        snippet = {"version": 2, "tasks": [],
                   "groups": {"sg": {"name": "空片段组", "color": "", "parent": None}},
                   "group_order": ["sg"]}
        self.window.snippets = {"空组片段": snippet}
        before = len(self.metadata()["names"])
        with mock.patch.object(self.window, "_selected_task_indices", lambda: []), \
                mock.patch.object(self.window, "_selected_group_ids", lambda: []), \
                mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("空组片段", True))):
            self.window.insert_snippet_at_selection()
        self.assertEqual(len(self.metadata()["names"]), before + 1, "只含分组的片段应建出分组")

    def test_persist_task_order_keeps_nested_steps(self):
        self.window.persist_task_order()
        self.assertEqual(len(g.TASKS), 3, "persist_task_order 不能丢掉嵌套组里的步骤")
        self.assertEqual(sorted(task["id"] for task in g.TASKS), ["A", "B", "C"])

    def test_menu_labels_describe_enable_disable(self):
        menu = self.window._build_task_list_menu(None)
        texts = [action.text() for action in menu.actions() if action.text()]
        self.assertIn("全部启用", texts)
        self.assertIn("全部停用", texts)
        self.assertNotIn("全选", texts)
        self.assertNotIn("清空", texts)


if __name__ == "__main__":
    unittest.main()
