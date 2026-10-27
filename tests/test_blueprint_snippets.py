# -*- coding: utf-8 -*-
"""蓝图界面：Blender 式群组（片段）——保存选中为片段 / 插入片段成群组节点（2026-10-27）。

语义：
  * 片段 = 可复用的步骤 + 分组结构（保存在主窗口同一份 __snippets__ 里）；
  * 蓝图中选中节点/组 →「保存选中为片段」；分组右键 →「保存该组为片段」；
  * 插入片段 → 生成一个**新的外层组**（名字=片段名）把整个片段装起来，画布上就是一个
    可以双击进入的群组节点；片段内部原有分组嵌套原样保留；
  * 插入/保存走蓝图撤销栈与保存回调。
"""

import copy
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

import blueprint_snippets  # noqa: E402
import gui_pyside6 as g  # noqa: E402
import snippets  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(task_id, group_id=None):
    task = {"id": task_id, "type": "normal", "description": task_id, "enabled": True, "template": "1"}
    if group_id:
        task["group_id"] = group_id
        task["group_name"] = group_id
    return task


def make_window():
    metadata = {
        "names": {"g1": "组1"},
        "colors": {"g1": "#eab308"},
        "parents": {"g1": None},
        "children": {"g1": []},
        "order": ["g1"],
    }
    tasks = [_task("A", "g1"), _task("B", "g1"), _task("C")]
    layout = {"positions": {"0": [60, 120], "1": [340, 120], "2": [60, 300]}}
    store = {}
    saved = []
    window = g.BlueprintWindow(
        tasks, layout, lambda *args, **kwargs: None, metadata, None, {},
        snippets_provider=lambda: store,
        snippets_save=lambda: saved.append(True),
    )
    window.resize(1100, 700)
    window.show()
    QApplication.processEvents()
    window.snippets_store = store
    window.snippets_saved = saved
    return window


def nested_snippet_payload():
    """一个带内部组的片段：X、Y 都在「内部组」里。"""
    src = [_task("X", "sg1"), _task("Y", "sg1")]
    meta = {
        "names": {"sg1": "内部组"},
        "colors": {"sg1": "#38bdf8"},
        "parents": {"sg1": None},
        "children": {"sg1": []},
        "order": ["sg1"],
    }
    return snippets.extract_snippet(src, [0, 1], meta, ("sg1",))


def flat_snippet_payload():
    """没有分组的片段：X、Y 都是根步骤。"""
    return {"version": 2, "tasks": [_task("X"), _task("Y")], "groups": {}, "group_order": []}


class SaveSnippetTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QApplication.processEvents()

    def _select_nodes(self, indices):
        self.window.scene.clearSelection()
        for index in indices:
            self.window._node_items[index].setSelected(True)
        QApplication.processEvents()

    def test_save_selected_nodes_as_snippet(self):
        self._select_nodes([0, 1])
        with mock.patch.object(blueprint_snippets.QInputDialog, "getText",
                               staticmethod(lambda *a, **k: ("测试片段", True))):
            name = self.window.save_selection_as_snippet()
        self.assertEqual(name, "测试片段")
        payload = self.window.snippets_store["测试片段"]
        self.assertEqual(payload["version"], 2)
        self.assertEqual([task["id"] for task in payload["tasks"]], ["A", "B"])
        self.assertIn("g1", payload["groups"], "选中的步骤所属分组应一起保存")
        self.assertTrue(self.window.snippets_saved, "保存片段要触发持久化回调")

    def test_save_single_step_does_not_carry_group(self):
        self._select_nodes([0])
        with mock.patch.object(blueprint_snippets.QInputDialog, "getText",
                               staticmethod(lambda *a, **k: ("单步片段", True))):
            name = self.window.save_selection_as_snippet()
        payload = self.window.snippets_store[name]
        self.assertEqual([task["id"] for task in payload["tasks"]], ["A"])
        self.assertEqual(payload["groups"], {}, "单个步骤的片段不应带上所在分组（避免插入时自动建组）")
        self.assertNotIn("group_id", payload["tasks"][0])
        self.assertNotIn("group_name", payload["tasks"][0])

    def test_save_group_as_snippet(self):
        with mock.patch.object(blueprint_snippets.QInputDialog, "getText",
                               staticmethod(lambda *a, **k: ("组片段", True))):
            name = self.window.save_group_as_snippet("g1")
        self.assertEqual(name, "组片段")
        payload = self.window.snippets_store["组片段"]
        self.assertEqual([task["id"] for task in payload["tasks"]], ["A", "B"])
        self.assertEqual(payload["groups"]["g1"]["name"], "组1")

    def test_save_without_selection_reports_and_does_not_save(self):
        self.window.scene.clearSelection()
        with mock.patch.object(blueprint_snippets.QMessageBox, "information") as info:
            name = self.window.save_selection_as_snippet()
        self.assertIsNone(name)
        self.assertEqual(self.window.snippets_store, {})
        info.assert_called_once()

    def test_save_group_without_steps_reports(self):
        with mock.patch.object(blueprint_snippets.QMessageBox, "information") as info:
            name = self.window.save_group_as_snippet("missing")
        self.assertIsNone(name)
        info.assert_called_once()


class InsertSnippetTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()
        self.window.snippets_store["片段A"] = nested_snippet_payload()
        self.window.snippets_store["扁平片段"] = flat_snippet_payload()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QApplication.processEvents()

    def _wrapper_id(self, name):
        names = self.window.group_metadata["names"]
        return next((gid for gid, value in names.items() if value == name), None)

    def _names(self):
        return list(self.window.group_metadata["names"].values())

    def test_insert_nested_snippet_keeps_its_own_groups_without_wrapper(self):
        self.window.insert_snippet_into_blueprint("片段A", (100, 200))
        self.assertEqual(len(self.window.tasks), 5)
        names = self._names()
        self.assertNotIn("片段A", names, "插入片段不应再自动包一层同名的组")
        inner = next((gid for gid, value in self.window.group_metadata["names"].items()
                      if value == "内部组"), None)
        self.assertIsNotNone(inner, "片段自带的分组要保留")
        metadata = self.window.group_metadata
        self.assertIsNone(metadata["parents"][inner], "片段内部组保持原层级（不被包装组套住）")
        self.assertEqual(self.window.tasks[3].get("group_id"), inner)
        self.assertEqual(self.window.tasks[4].get("group_id"), inner)

    def test_insert_flat_snippet_creates_no_group(self):
        before = self._names()
        self.window.insert_snippet_into_blueprint("扁平片段", (100, 200))
        self.assertEqual(self._names(), before, "扁平片段（无分组）插入后不应创建任何组")
        for task in self.window.tasks[3:]:
            self.assertIsNone(task.get("group_id"), "扁平片段的步骤应保持根层")

    def test_insert_assigns_layout_positions(self):
        self.window.insert_snippet_into_blueprint("扁平片段", (100, 200))
        positions = self.window.layout_data["positions"]
        self.assertIn("3", positions)
        self.assertIn("4", positions)
        self.assertEqual(positions["3"], (100.0, 200.0))
        self.assertEqual(positions["4"], (380.0, 200.0))

    def test_insert_inside_focus_group_puts_steps_in_that_group(self):
        before = self._names()
        self.window.focus_group = "g1"
        self.window.insert_snippet_into_blueprint("扁平片段", (100, 200))
        self.assertEqual(self._names(), before, "聚焦组里插入扁平片段也不应新建组")
        for task in self.window.tasks[3:]:
            self.assertEqual(task.get("group_id"), "g1", "聚焦组里插入的步骤应归入当前组")
        self.assertEqual(self.window.tasks[3].get("group_name"), "组1")

    def test_insert_same_snippet_twice_dedupes_inner_group_names(self):
        self.window.insert_snippet_into_blueprint("片段A", (100, 200))
        self.window.insert_snippet_into_blueprint("片段A", (400, 200))
        names = self._names()
        self.assertIn("内部组", names)
        self.assertIn("内部组 2", names)

    def test_undo_after_insert_restores_tasks_and_metadata(self):
        before_tasks = [task["id"] for task in self.window.tasks]
        before_names = self._names()
        self.window.insert_snippet_into_blueprint("扁平片段", (100, 200))
        self.assertEqual(len(self.window.tasks), 5)
        self.window.undo()
        QApplication.processEvents()
        self.assertEqual([task["id"] for task in self.window.tasks], before_tasks)
        self.assertEqual(self._names(), before_names)

    def test_insert_unknown_snippet_is_noop(self):
        before = len(self.window.tasks)
        self.assertIsNone(self.window.insert_snippet_into_blueprint("不存在", None))
        self.assertEqual(len(self.window.tasks), before)


class SnippetMenuTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QApplication.processEvents()

    def test_canvas_menu_has_snippet_entries(self):
        menu = QMenu(self.window)
        self.window._add_snippet_menu(menu, (0, 0))
        texts = [action.text() for action in menu.actions()]
        self.assertIn("片段（群组）", texts)
        snippet_menu = next(action.menu() for action in menu.actions() if action.text() == "片段（群组）")
        sub_texts = [action.text() for action in snippet_menu.actions()]
        self.assertIn("插入片段", sub_texts)
        self.assertIn("保存选中为片段…", sub_texts)

    def test_insert_submenu_disabled_when_no_snippets(self):
        menu = QMenu(self.window)
        self.window._add_snippet_menu(menu, (0, 0))
        snippet_menu = next(action.menu() for action in menu.actions() if action.text() == "片段（群组）")
        insert_action = next(action for action in snippet_menu.actions() if action.text() == "插入片段")
        self.assertFalse(insert_action.menu().isEnabled())

    def test_insert_submenu_lists_snippets(self):
        self.window.snippets_store["片段A"] = flat_snippet_payload()
        menu = QMenu(self.window)
        self.window._add_snippet_menu(menu, (0, 0))
        snippet_menu = next(action.menu() for action in menu.actions() if action.text() == "片段（群组）")
        insert_action = next(action for action in snippet_menu.actions() if action.text() == "插入片段")
        self.assertTrue(insert_action.menu().isEnabled())
        self.assertEqual([action.text() for action in insert_action.menu().actions()], ["片段A"])

    def test_group_menu_has_save_and_insert(self):
        menu = QMenu(self.window)
        self.window._add_group_snippet_menu(menu, "g1")
        texts = [action.text() for action in menu.actions()]
        self.assertIn("保存该组为片段…", texts)
        self.assertIn("插入片段到该组", texts)


class SnippetGuardTests(unittest.TestCase):
    def test_blueprint_window_inherits_snippet_mixin(self):
        from blueprint_snippets import BlueprintSnippetMixin
        self.assertTrue(issubclass(g.BlueprintWindow, BlueprintSnippetMixin))

    def test_main_window_injects_snippet_store(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("snippets_provider=lambda: self.snippets", source)
        self.assertIn("snippets_save=self._save_presets", source)

    def test_insert_uses_shared_snippets_module(self):
        source = open("blueprint_snippets.py", encoding="utf-8").read()
        self.assertIn("snippet_store.insert_snippet(", source)
        self.assertIn("snippet_store.extract_snippet(", source)


if __name__ == "__main__":
    unittest.main()
