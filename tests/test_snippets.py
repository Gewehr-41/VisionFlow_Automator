# -*- coding: utf-8 -*-
"""F13 片段（第二轮）：可保存分组、可插入到选中步骤之后、组内可再包含组。

数据格式 v2（兼容 v1 的纯步骤列表）：

    {片段名: {"version": 2, "tasks": [...], "groups": {gid: {name,color,parent}}, "group_order": [...]}}

关键不变量：
  · 保存：内部跳转改相对编号、外部跳转丢弃；选中分组会带上组内全部步骤与子组；
  · 插入：插入点之后现有步骤的跳转整体后移；分组生成新 id 并重映射父子关系；
    顶层组挂到插入点所在的组（或根层），片段里没有分组的步骤也归入该组；
  · 插入位置：选中步骤 → 紧接其后；选中分组 → 该组最后一步之后；无选中 → 末尾。
"""

import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import snippets  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(description, group_id=None, **extra):
    task = {"id": description, "type": "normal", "description": description, "template": description}
    if group_id:
        task["group_id"] = group_id
        task["group_name"] = group_id
    task.update(extra)
    return task


def _metadata():
    return {
        "names": {"g1": "战斗", "g2": "弹窗"},
        "colors": {"g1": "#111111", "g2": "#222222"},
        "parents": {"g1": None, "g2": "g1"},
        "children": {"g1": ["g2"], "g2": []},
        "order": ["g1"],
    }


class PayloadTests(unittest.TestCase):
    def test_v1_list_is_normalised(self):
        tasks_in, groups, order = snippets.normalize_payload([_task("A")])
        self.assertEqual(len(tasks_in), 1)
        self.assertEqual(groups, {})
        self.assertEqual(order, [])

    def test_v2_dict_is_normalised(self):
        payload = {"version": 2, "tasks": [_task("A")],
                   "groups": {"g1": {"name": "战斗", "color": "", "parent": None}},
                   "group_order": ["g1"]}
        tasks_in, groups, order = snippets.normalize_payload(payload)
        self.assertEqual(tasks_in[0]["description"], "A")
        self.assertEqual(order, ["g1"])

    def test_garbage_is_empty(self):
        for value in (None, 42, "x", {}, []):
            tasks_in, groups, _order = snippets.normalize_payload(value)
            self.assertEqual(tasks_in, [])
            self.assertEqual(groups, {})


class ExtractSnippetTests(unittest.TestCase):
    def test_selected_steps_only(self):
        task_list = [_task("A"), _task("B"), _task("C")]
        payload = snippets.extract_snippet(task_list, [0, 2])
        self.assertEqual([t["description"] for t in payload["tasks"]], ["A", "C"])
        self.assertEqual(payload["groups"], {})

    def test_internal_jumps_become_relative(self):
        task_list = [_task("A"), _task("B", condition_true_jump_to=3, condition_false_jump_to=1), _task("C")]
        payload = snippets.extract_snippet(task_list, [0, 1, 2])
        self.assertEqual(payload["tasks"][1]["condition_true_jump_to"], 3)
        self.assertEqual(payload["tasks"][1]["condition_false_jump_to"], 1)

    def test_external_jumps_are_dropped(self):
        task_list = [_task("A", timeout_jump_to=4), _task("B")]
        payload = snippets.extract_snippet(task_list, [0])
        self.assertNotIn("timeout_jump_to", payload["tasks"][0])

    def test_deepcopy_isolation(self):
        task_list = [_task("A", match_rects=[[0, 0, 10, 10]])]
        payload = snippets.extract_snippet(task_list, [0])
        payload["tasks"][0]["match_rects"][0][0] = 99
        self.assertEqual(task_list[0]["match_rects"][0][0], 0)

    def test_empty_selection(self):
        payload = snippets.extract_snippet([_task("A")], [])
        self.assertEqual(payload["tasks"], [])

    def test_internal_fields_are_stripped(self):
        payload = snippets.extract_snippet([_task("A", _outer_step_number=3)], [0])
        self.assertNotIn("_outer_step_number", payload["tasks"][0])

    # ---- 分组 ----

    def test_group_selection_brings_all_steps_including_children(self):
        task_list = [_task("A", "g1"), _task("B", "g2"), _task("C", "g1"), _task("D")]
        payload = snippets.extract_snippet(task_list, [], _metadata(), ["g1"])
        self.assertEqual([t["description"] for t in payload["tasks"]], ["A", "B", "C"])
        self.assertEqual(set(payload["groups"]), {"g1", "g2"})

    def test_group_nesting_kept_inside_snippet(self):
        task_list = [_task("A", "g1"), _task("B", "g2")]
        payload = snippets.extract_snippet(task_list, [0, 1], _metadata())
        self.assertIsNone(payload["groups"]["g1"]["parent"])
        self.assertEqual(payload["groups"]["g2"]["parent"], "g1", "内部父子关系要保留")

    def test_ancestor_groups_are_included_for_nesting(self):
        task_list = [_task("A", "g2")]
        payload = snippets.extract_snippet(task_list, [0], _metadata())
        self.assertIn("g1", payload["groups"], "祖先组也要带上，否则嵌套关系断了")
        self.assertEqual(payload["groups"]["g2"]["parent"], "g1")

    def test_group_metadata_names_and_colors(self):
        payload = snippets.extract_snippet([_task("A", "g1")], [0], _metadata())
        self.assertEqual(payload["groups"]["g1"]["name"], "战斗")
        self.assertEqual(payload["groups"]["g1"]["color"], "#111111")

    def test_summary_counts_groups(self):
        payload = snippets.extract_snippet([_task("A", "g1"), _task("B", "g2")], [0, 1], _metadata())
        text = snippets.snippet_summary(payload)
        self.assertIn("2 步", text)
        self.assertIn("2 个分组", text)


class InsertSnippetTests(unittest.TestCase):
    def setUp(self):
        self.target = [_task("T0", timeout_jump_to=2), _task("T1")]
        self.snippet = {
            "version": 2,
            "tasks": [_task("S0", "sg1"), _task("S1", "sg2", condition_true_jump_to=3, condition_false_jump_to=1),
                      _task("S2", "sg1")],
            "groups": {
                "sg1": {"name": "新战斗", "color": "#abcdef", "parent": None},
                "sg2": {"name": "新弹窗", "color": "", "parent": "sg1"},
            },
            "group_order": ["sg1", "sg2"],
        }

    def test_shifts_existing_jumps(self):
        result, _groups = snippets.insert_snippet(self.target, self.snippet, position=1)
        self.assertEqual([t["description"] for t in result], ["T0", "S0", "S1", "S2", "T1"])
        self.assertEqual(result[0]["timeout_jump_to"], 5, "T0 的跳转应随插入后移")

    def test_relative_jumps_become_absolute(self):
        result, _groups = snippets.insert_snippet(self.target, self.snippet, position=1)
        self.assertEqual(result[2]["condition_true_jump_to"], 4)
        self.assertEqual(result[2]["condition_false_jump_to"], 2)

    def test_groups_get_new_ids_and_keep_nesting(self):
        _result, groups = snippets.insert_snippet(self.target, self.snippet, position=1)
        self.assertEqual(len(groups), 2)
        self.assertNotIn(groups[0]["id"], ("sg1", "sg2"), "必须生成新 id")
        self.assertEqual(groups[1]["parent"], groups[0]["id"], "内部嵌套要重映射到新 id")
        self.assertEqual(groups[0]["name"], "新战斗")
        self.assertEqual(groups[0]["color"], "#abcdef")

    def test_steps_point_to_new_group_ids(self):
        result, groups = snippets.insert_snippet(self.target, self.snippet, position=1)
        ids = {group["id"] for group in groups}
        for task in result[1:4]:
            self.assertIn(task["group_id"], ids)
        self.assertEqual(result[1]["group_id"], groups[0]["id"])
        self.assertEqual(result[2]["group_id"], groups[1]["id"])

    def test_top_level_groups_nest_under_target_group(self):
        _result, groups = snippets.insert_snippet(self.target, self.snippet, position=1, parent_group_id="g_target")
        self.assertEqual(groups[0]["parent"], "g_target", "插入到某个组里时，片段顶层组要变成它的子组")
        self.assertEqual(groups[1]["parent"], groups[0]["id"])

    def test_ungrouped_steps_follow_target_group(self):
        snippet = {"version": 2, "tasks": [_task("S")], "groups": {}, "group_order": []}
        result, _groups = snippets.insert_snippet([], snippet, position=0, parent_group_id="g_target")
        self.assertEqual(result[0]["group_id"], "g_target")

    def test_ids_and_flow_next(self):
        snippet = {"version": 2, "tasks": [_task("S0"), _task("S1")]}
        snippet["tasks"][0]["flow_next"] = "S1"
        result, _groups = snippets.insert_snippet([], snippet, position=0)
        self.assertEqual(len({task["id"] for task in result}), 2)
        self.assertEqual(result[0]["flow_next"], result[1]["id"])

    def test_positions_and_empty(self):
        self.assertEqual([t["description"] for t in snippets.insert_snippet(self.target, [_task("S")], 0)[0]],
                         ["S", "T0", "T1"])
        self.assertEqual([t["description"] for t in snippets.insert_snippet(self.target, [_task("S")])[0]],
                         ["T0", "T1", "S"])
        self.assertEqual(len(snippets.insert_snippet(self.target, [], 0)[0]), 2)

    def test_repeated_inserts_do_not_mutate_payload(self):
        """同一片段连续插入多次：每次都要建组、步骤归属正确，片段数据本身不能被改。"""
        payload = {
            "version": 2,
            "tasks": [_task("S0", "sg1"), _task("S1", "sg1")],
            "groups": {"sg1": {"name": "新组", "color": "", "parent": None}},
            "group_order": ["sg1"],
        }
        snapshot = copy.deepcopy(payload)
        tasks = []
        for _ in range(3):
            tasks, groups = snippets.insert_snippet(tasks, payload, position=None)
            self.assertEqual(len(groups), 1, "每次插入都应新建一个组")
            inserted = tasks[-2:]
            new_group_id = groups[0]["id"]
            self.assertTrue(all(task.get("group_id") == new_group_id for task in inserted),
                            "插入的步骤要归属到本次新建的组")
        self.assertEqual(payload, snapshot, "insert_snippet 不能修改片段数据（否则下次插入会丢组）")

    def test_v1_snippet_still_insertable(self):
        result, groups = snippets.insert_snippet(self.target, [_task("S")], position=1)
        self.assertEqual([t["description"] for t in result], ["T0", "S", "T1"])
        self.assertEqual(groups, [])


class SnippetStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")

    def tearDown(self):
        tasks.PRESETS_FILE, tasks.TASKS_FILE = self._orig
        self._tmp.cleanup()

    def _write(self, payload):
        with io.open(tasks.PRESETS_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

    def test_loader_reads_v1_list(self):
        self._write({"__snippets__": {"老片段": [_task("A")]}})
        loaded = tasks.load_snippets()
        self.assertEqual(loaded["老片段"][0]["description"], "A")

    def test_loader_reads_v2_dict(self):
        self._write({"__snippets__": {"新片段": {"version": 2, "tasks": [_task("A")],
                                                "groups": {"g1": {"name": "组", "color": "", "parent": None}},
                                                "group_order": ["g1"]}}})
        loaded = tasks.load_snippets()
        self.assertEqual(loaded["新片段"]["version"], 2)
        self.assertEqual(list(loaded["新片段"]["groups"]), ["g1"])

    def test_reserved_key_is_not_preset(self):
        self._write({"预设A": [_task("A")], "__snippets__": {"常用": [_task("B")]}})
        presets = tasks.load_presets()
        self.assertIn("预设A", presets)
        self.assertNotIn("__snippets__", presets)


class SnippetGuiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        self.window = g.PySide6ScriptWindow()
        self.window.snippets = {}
        self.mode = self.window.mode_combo.currentText() or "custom"

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def _logs(self):
        return self.window.log_box.toPlainText()

    def test_save_group_includes_steps_and_persists_v2(self):
        self.window.mode_group_metadata[self.mode] = _metadata()
        g.TASKS[:] = [_task("A", "g1"), _task("B", "g2"), _task("C")]
        with mock.patch.object(self.window, "_selected_task_indices", lambda: []), \
                mock.patch.object(self.window, "_selected_group_ids", lambda: ["g1"]), \
                mock.patch.object(g.QInputDialog, "getText", staticmethod(lambda *a, **k: ("带组片段", True))):
            self.window.save_selection_as_snippet()
        payload = self.window.snippets["带组片段"]
        self.assertEqual(len(payload["tasks"]), 2, "g1 与子组 g2 的步骤都要收进片段")
        self.assertEqual(set(payload["groups"]), {"g1", "g2"})
        with io.open(tasks.PRESETS_FILE, encoding="utf-8") as handle:
            stored = json.load(handle)["__snippets__"]["带组片段"]
        self.assertEqual(stored["version"], 2)
        self.assertEqual(set(stored["groups"]), {"g1", "g2"})

    def test_save_empty_group_is_allowed(self):
        """现在允许保存「只有分组、没有步骤」的片段（与插入端语义一致）。"""
        self.window.mode_group_metadata[self.mode] = {"names": {"empty": "空组"}, "colors": {}, "parents": {"empty": None},
                                                      "children": {"empty": []}, "order": ["empty"]}
        g.TASKS[:] = [_task("A")]
        with mock.patch.object(self.window, "_selected_task_indices", lambda: []), \
                mock.patch.object(self.window, "_selected_group_ids", lambda: ["empty"]), \
                mock.patch.object(g.QInputDialog, "getText", staticmethod(lambda *a, **k: ("空片段", True))), \
                mock.patch.object(g.QMessageBox, "warning", return_value=None) as warning:
            self.window.save_selection_as_snippet()
        self.assertFalse(warning.called, "保存空组片段不该再弹警告")
        self.assertIn("空片段", self.window.snippets)
        self.assertEqual(self.window.snippets["空片段"]["tasks"], [])
        self.assertEqual(list(self.window.snippets["空片段"]["groups"]), ["empty"])

    def test_save_single_step_does_not_carry_group(self):
        """只选一个步骤存片段：不带上它所在的分组，插入时就不会凭空建组。"""
        self.window.mode_group_metadata[self.mode] = _metadata()
        g.TASKS[:] = [_task("A", "g1"), _task("B", "g1"), _task("C")]
        with mock.patch.object(self.window, "_selected_task_indices", lambda: [0]), \
                mock.patch.object(self.window, "_selected_group_ids", lambda: []), \
                mock.patch.object(g.QInputDialog, "getText", staticmethod(lambda *a, **k: ("单步片段", True))):
            self.window.save_selection_as_snippet()
        payload = self.window.snippets["单步片段"]
        self.assertEqual([task["id"] for task in payload["tasks"]], ["A"])
        self.assertEqual(payload["groups"], {}, "单个步骤的片段不应保存分组结构")
        self.assertNotIn("group_id", payload["tasks"][0])
        # 插入后不应新增任何分组
        before = len(self.window.mode_group_metadata[self.mode]["names"])
        with mock.patch.object(self.window, "_selected_task_indices", lambda: []), \
                mock.patch.object(self.window, "_selected_group_ids", lambda: []), \
                mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("单步片段", True))):
            self.window.insert_snippet_at_selection()
        self.assertEqual(len(self.window.mode_group_metadata[self.mode]["names"]), before,
                         "插入单步骤片段不应自动创建组")

    def test_insert_after_selected_step(self):
        self.window.snippets = {"常用组": [_task("S0"), _task("S1")]}
        g.TASKS[:] = [_task("A"), _task("B", timeout_jump_to=2)]
        with mock.patch.object(self.window, "_selected_task_indices", lambda: [0]), \
                mock.patch.object(self.window, "_selected_group_ids", lambda: []), \
                mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("常用组", True))):
            self.window.insert_snippet_at_selection()
        self.assertEqual([t["description"] for t in g.TASKS], ["A", "S0", "S1", "B"],
                         "插入的步骤必须紧接在选中步骤之后")
        self.assertEqual(g.TASKS[3]["timeout_jump_to"], 4)

    def test_insert_into_selected_group(self):
        self.window.mode_group_metadata[self.mode] = _metadata()
        self.window.snippets = {"带组片段": snippets.extract_snippet(
            [_task("X", "gx1"), _task("Y", "gx2")], [0, 1],
            {"names": {"gx1": "内层A", "gx2": "内层B"}, "colors": {},
             "parents": {"gx1": None, "gx2": "gx1"}, "children": {"gx1": ["gx2"], "gx2": []}, "order": ["gx1"]})}
        g.TASKS[:] = [_task("A", "g1"), _task("B", "g1"), _task("C")]
        with mock.patch.object(self.window, "_selected_task_indices", lambda: []), \
                mock.patch.object(self.window, "_selected_group_ids", lambda: ["g1"]), \
                mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("带组片段", True))):
            self.window.insert_snippet_at_selection()
        self.assertEqual([t["description"] for t in g.TASKS], ["A", "B", "X", "Y", "C"],
                         "插入分组时接在该组最后一个步骤之后")
        metadata = self.window.mode_group_metadata[self.mode]
        new_ids = [gid for gid in metadata["names"] if gid not in ("g1", "g2")]
        self.assertEqual(len(new_ids), 2, "片段里的两个分组都要登记")
        top = next(gid for gid in new_ids if metadata["parents"][gid] == "g1")
        child = next(gid for gid in new_ids if metadata["parents"][gid] == top)
        self.assertTrue(top and child, "顶层组挂到 g1，嵌套组挂到顶层组")
        self.assertEqual(g.TASKS[2]["group_id"], top)

    def test_insert_without_selection_appends(self):
        self.window.snippets = {"常用组": [_task("S")]}
        g.TASKS[:] = [_task("A")]
        with mock.patch.object(self.window, "_selected_task_indices", lambda: []), \
                mock.patch.object(self.window, "_selected_group_ids", lambda: []), \
                mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("常用组", True))):
            self.window.insert_snippet_at_selection()
        self.assertEqual([t["description"] for t in g.TASKS], ["A", "S"])

    def test_delete_snippet(self):
        self.window.snippets = {"临时": [_task("S")]}
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("临时", True))), \
                mock.patch.object(g.QMessageBox, "question",
                                  return_value=g.QMessageBox.StandardButton.Yes):
            self.window.delete_snippet()
        self.assertEqual(self.window.snippets, {})

    def test_no_selection_warns(self):
        with mock.patch.object(self.window, "_selected_task_indices", lambda: []), \
                mock.patch.object(self.window, "_selected_group_ids", lambda: []), \
                mock.patch.object(g.QMessageBox, "warning", return_value=None) as warning:
            self.window.save_selection_as_snippet()
        self.assertTrue(warning.called)

    def _metadata(self):
        metadata = {
            "names": {"g1": "外层", "g2": "子组"},
            "colors": {},
            "parents": {"g1": None, "g2": "g1"},
            "children": {"g1": ["g2"], "g2": []},
            "order": ["g1"],
        }
        self.window.mode_group_metadata[self.mode] = metadata
        return metadata

    def _tree_lines(self):
        lines = []

        def walk(item, depth):
            kind = "G" if item.data(0, Qt.UserRole) == "group" else "T"
            lines.append("  " * depth + f"[{kind}] {item.text(0)}")
            for i in range(item.childCount()):
                walk(item.child(i), depth + 1)

        for i in range(self.window.task_list.topLevelItemCount()):
            walk(self.window.task_list.topLevelItem(i), 0)
        return lines

    def _save_group(self, group_id, name):
        self.window.task_list.clearSelection()
        self.window._group_items()[group_id].setSelected(True)
        with mock.patch.object(g.QInputDialog, "getText", staticmethod(lambda *a, **k: (name, True))):
            self.window.save_selection_as_snippet()
        _APP.processEvents()

    def _insert(self, name, keep_selection=False):
        if not keep_selection:
            self.window.task_list.clearSelection()
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: (name, True))):
            self.window.insert_snippet_at_selection()
        _APP.processEvents()

    def _group(self, group_id):
        return self.window._group_items()[group_id]

    # ---------- #1 组名去重 ----------

    def test_insert_twice_dedupes_group_names(self):
        self._metadata()
        g.TASKS[:] = [_task("A", "g1"), _task("B", "g2")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self._save_group("g1", "带组片段")
        self._insert("带组片段")
        self._insert("带组片段")
        names = list(self.window.mode_group_metadata[self.mode]["names"].values())
        self.assertEqual(len(names), len(set(names)), f"插入两次后组名不能重复: {names}")
        self.assertTrue(any("副本" in name for name in names), "重复插入的组名应带「副本」后缀")

    # ---------- #2 多选组取最外层 ----------

    def test_multiple_selected_groups_uses_outermost(self):
        metadata = self._metadata()
        g.TASKS[:] = [_task("A", "g1"), _task("B", "g2"), _task("C", "g1")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self._save_group("g1", "带组片段")
        self.window.task_list.clearSelection()
        self._group("g1").setSelected(True)
        self._group("g2").setSelected(True)
        self._insert("带组片段", keep_selection=True)
        new_top_groups = [gid for gid, name in self.window.mode_group_metadata[self.mode]["names"].items()
                          if name.startswith("外层") and gid not in ("g1", "g2")]
        parents = self.window.mode_group_metadata[self.mode]["parents"]
        self.assertTrue(new_top_groups, "应插入一个新外层组")
        self.assertEqual(str(parents[new_top_groups[0]]), "g1", "多选时用最外层父组（g1），不能塞进子组")
        self.assertIn("最外层", self._logs(), "多选时应记录使用的是哪个父组")

    # ---------- #3 折叠组自动展开 ----------

    def test_insert_into_collapsed_group_expands_it(self):
        metadata = self._metadata()
        metadata["expanded"] = {"g1": False}
        g.TASKS[:] = [_task("A", "g1"), _task("B", "g2")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self._save_group("g1", "带组片段")
        self._group("g1").setExpanded(False)
        _APP.processEvents()
        self.window.task_list.clearSelection()
        self._group("g1").setSelected(True)
        self._insert("带组片段", keep_selection=True)
        self.assertEqual(self.window.mode_group_metadata[self.mode]["expanded"].get("g1"), True,
                         "往折叠组里插入后应自动展开，否则看不到结果")

    # ---------- #4 蓝图布局字段不带走 ----------

    def test_snippet_drops_blueprint_layout_fields(self):
        self._metadata()
        g.TASKS[:] = [_task("A", "g1", blueprint_bends={"flow": [[10, 20]]}, blueprint_collapsed=True)]
        self.window.refresh_task_list()
        _APP.processEvents()
        self._save_group("g1", "带折线片段")
        payload = self.window.snippets["带折线片段"]
        saved = payload["tasks"][0]
        self.assertNotIn("blueprint_bends", saved, "折线坐标依赖连线对象，不能跨片段带走")
        self.assertNotIn("blueprint_collapsed", saved)
        self._insert("带折线片段")
        inserted = [task for task in g.TASKS if task["id"] != "A"]
        self.assertTrue(inserted)
        self.assertNotIn("blueprint_bends", inserted[0])

    # ---------- #5 只有分组的片段可保存 ----------

    def test_save_group_only_snippet(self):
        metadata = self._metadata()
        metadata["names"]["empty"] = "空组"
        metadata["parents"]["empty"] = None
        metadata["children"]["empty"] = []
        metadata["order"].append("empty")
        g.TASKS[:] = [_task("A", "g1"), _task("C")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self._save_group("empty", "空组片段")
        self.assertIn("空组片段", self.window.snippets, "只有分组的片段应该能保存")
        payload = self.window.snippets["空组片段"]
        self.assertEqual(payload["tasks"], [])
        self.assertEqual(list(payload["groups"]), ["empty"])
        names_before = set(self.window.mode_group_metadata[self.mode]["names"].values())
        self._insert("空组片段")
        names_after = set(self.window.mode_group_metadata[self.mode]["names"].values())
        self.assertGreater(len(names_after), len(names_before), "插入后应建出分组")

    # ---------- 顺序往返：组与步骤的交错关系 ----------

    def test_roundtrip_preserves_group_step_interleaving(self):
        self._metadata()
        g.TASKS[:] = [_task("A", "g1"), _task("B", "g2"), _task("C", "g1")]
        self.window.refresh_task_list()
        _APP.processEvents()
        before = [line.strip() for line in self._tree_lines()]
        self._save_group("g1", "交错片段")
        g.TASKS[:] = []
        # 模拟插入到一个"全新预设"：清掉源分组元数据，否则它们会作为空组渲染在末尾
        self.window.mode_group_metadata[self.mode] = {
            "names": {}, "colors": {}, "parents": {}, "children": {}, "order": [], "expanded": {},
        }
        self.window.refresh_task_list()
        _APP.processEvents()
        self._insert("交错片段")
        lines = [line for line in self._tree_lines()]
        kinds = [("G" if "[G]" in line else "T") for line in lines]
        # 原图：G 外层 / T A / G 子组 / T B / T C -> 插入后应保持同样的 G,T,G,T,T 顺序
        self.assertEqual(kinds, ["G", "T", "G", "T", "T"], f"组与步骤交错顺序被破坏: {lines}")
        self.assertIn("A", lines[1])
        self.assertIn("子组", lines[2])
        self.assertIn("B", lines[3])
        self.assertIn("C", lines[4])

    def test_insert_same_group_snippet_three_times_keeps_groups(self):
        """连续插入同一「组片段」多次，每次都应有新组、步骤不能掉到根层。"""
        self._metadata()
        g.TASKS[:] = [_task("A", "g1"), _task("B", "g1")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self._save_group("g1", "组片段")
        metadata = self.window.mode_group_metadata[self.mode]
        before = len(metadata["names"])
        for _ in range(3):
            self._insert("组片段")
        self.assertEqual(len(metadata["names"]), before + 3,
                         f"每次插入都应新建组（当前 {metadata['names']}）")
        for task in g.TASKS:
            group_id = task.get("group_id")
            self.assertIn(str(group_id), metadata["names"],
                          f"步骤 {task['description']} 的组 {group_id} 不存在了（掉到根层/组消失）")
        kinds = ["G" if "[G]" in line else "T" for line in self._tree_lines()]
        self.assertEqual(kinds.count("G"), before + 3)
        self.assertEqual(kinds.count("T"), 2 + 3 * 2, "原 2 步 + 3 次 × 2 步")

    # ---------- 所有者 2026-10-27 复现场景：组 + 两个步骤 ----------

    def test_group_with_steps_keeps_membership_after_insert(self):
        """选中「组 + 组内两个步骤」保存，插入后两个步骤必须还在新组里。"""
        self._metadata()
        g.TASKS[:] = [_task("S1", "g1"), _task("S2", "g1")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self._save_group("g1", "组合片段")
        # 再显式把两个步骤也选中（模拟用户同时选中组和步骤）
        self.window._group_items()["g1"].setSelected(True)
        for index in range(2):
            self.window._find_task_item(index).setSelected(True)
        payload = self.window.snippets["组合片段"]
        self.assertTrue(payload["tasks"], "片段里应有步骤")
        self.assertEqual({str(t.get("group_id")) for t in payload["tasks"]}, {"g1"})

        # 插入到一个全新预设
        g.TASKS[:] = []
        self.window.mode_group_metadata[self.mode] = {
            "names": {}, "colors": {}, "parents": {}, "children": {}, "order": [], "expanded": {},
        }
        self.window.refresh_task_list()
        _APP.processEvents()
        self._insert("组合片段")
        kinds = ["G" if "[G]" in line else "T" for line in self._tree_lines()]
        self.assertEqual(kinds, ["G", "T", "T"], f"步骤应留在组内，不能变成空组+根层步骤: {self._tree_lines()}")

    def test_insert_ignores_memberless_groups_from_old_snippet(self):
        """旧片段：步骤是根层 + 夹带两个没有步骤的分组（其中一个名字就是 group_xxx）。"""
        self.window.snippets = {
            "旧片段": {
                "version": 2,
                "tasks": [{"id": "x1", "type": "normal", "description": "X1", "template": "1"},
                          {"id": "x2", "type": "normal", "description": "X2", "template": "1"}],
                "groups": {
                    "group_5fae0cff": {"name": "组 1", "color": "", "parent": None},
                    "group_ee59d652": {"name": "group_ee59d652", "color": "", "parent": None},
                },
                "group_order": ["group_5fae0cff", "group_ee59d652"],
            }
        }
        self.window.mode_group_metadata[self.mode] = {
            "names": {}, "colors": {}, "parents": {}, "children": {}, "order": [], "expanded": {},
        }
        g.TASKS[:] = []
        self.window.refresh_task_list()
        _APP.processEvents()
        self._insert("旧片段")
        self.assertEqual(len(g.TASKS), 2)
        self.assertEqual(self.window._group_items(), {}, "没有成员步骤的分组不应被创建成空壳组")
        names = list(self.window.mode_group_metadata[self.mode].get("names", {}).values())
        self.assertFalse(any(str(name).startswith("group_") for name in names), f"不该出现 group_xxx 组名: {names}")

    def test_save_mixed_selection_drops_empty_groups(self):
        """同时选中"空组 + 根层步骤"保存时，忽略空组并提示。"""
        metadata = self._metadata()
        metadata["names"]["empty"] = "空组"
        metadata["parents"]["empty"] = None
        metadata["children"]["empty"] = []
        metadata["order"].append("empty")
        g.TASKS[:] = [_task("R1"), _task("R2")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self.window.task_list.clearSelection()
        self.window._group_items()["empty"].setSelected(True)
        self.window._find_task_item(0).setSelected(True)
        self.window._find_task_item(1).setSelected(True)
        with mock.patch.object(g.QInputDialog, "getText", staticmethod(lambda *a, **k: ("混合片段", True))):
            self.window.save_selection_as_snippet()
        payload = self.window.snippets["混合片段"]
        self.assertEqual(len(payload["tasks"]), 2)
        self.assertEqual(payload["groups"], {}, "空组不应写进片段")
        self.assertIn("没有步骤的分组", self._logs(), "应提示忽略了空组")

    # ---------- 元数据自愈 ----------

    def test_refresh_heals_orphan_group_reference(self):
        """children 里引用了没有元数据、也没有步骤的组 id：刷新时清掉，不再出现 group_xxx。"""
        self.window.mode_group_metadata[self.mode] = {
            "names": {"g1": "组1"},
            "colors": {},
            "parents": {"g1": None},
            "children": {"g1": ["group_deadbeef"]},
            "order": ["g1"],
        }
        g.TASKS[:] = [_task("A", "g1")]
        self.window.refresh_task_list()
        _APP.processEvents()
        labels = " ".join(item.text(0) for item in self.window._group_items().values())
        self.assertNotIn("group_deadbeef", labels, f"失效引用不该显示成组名: {labels}")
        self.assertNotIn("group_deadbeef", self.window.mode_group_metadata[self.mode]["children"]["g1"])

    def test_refresh_names_missing_group_used_by_steps(self):
        """步骤指向一个元数据里没有名字的组：刷新时补「分组」占位名，不显示 group_xxx。"""
        self.window.mode_group_metadata[self.mode] = {
            "names": {}, "colors": {}, "parents": {}, "children": {}, "order": [],
        }
        g.TASKS[:] = [_task("A", "group_abc12345")]
        self.window.refresh_task_list()
        _APP.processEvents()
        labels = " ".join(item.text(0) for item in self.window._group_items().values())
        self.assertIn("分组", labels)
        self.assertNotIn("group_abc12345", labels)

    def test_insert_log_reports_target_group(self):
        self._metadata()
        g.TASKS[:] = [_task("A", "g1")]
        self.window.refresh_task_list()
        _APP.processEvents()
        self._save_group("g1", "目标组片段")
        self.window.task_list.clearSelection()
        self._group("g1").setSelected(True)
        self._insert("目标组片段", keep_selection=True)
        self.assertIn("插入到分组", self._logs(), "日志要说明插到了哪个组")

    def test_tool_button_exists(self):
        self.assertIn("片段", self.window.tool_actions)


if __name__ == "__main__":
    unittest.main()
