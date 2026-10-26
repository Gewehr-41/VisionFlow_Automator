# -*- coding: utf-8 -*-
"""F13 步骤片段：保存/插入时的跳转换算、持久化、界面流程。

核心不变量：
  · 保存时，指向片段**内部**的跳转改写成片段内相对编号，指向**外部**的丢弃；
  · 插入时，插入点之后所有现有步骤的跳转整体后移，片段的相对编号换算成绝对编号；
  · 片段内部按 id 寻址的 flow_next 保持连接；所有 id 重新生成。
"""

import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import preflight  # noqa: E402
import snippets  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(description, **extra):
    task = {"id": description, "type": "normal", "description": description, "template": description}
    task.update(extra)
    return task


class ExtractSnippetTests(unittest.TestCase):
    def test_internal_jumps_become_relative(self):
        tasks = [
            _task("A"),
            _task("B", condition_true_jump_to=3, condition_false_jump_to=1),
            _task("C"),
        ]
        snippet = snippets.extract_snippet(tasks, [0, 1, 2])
        self.assertEqual([task["description"] for task in snippet], ["A", "B", "C"])
        self.assertEqual(snippet[1]["condition_true_jump_to"], 3)
        self.assertEqual(snippet[1]["condition_false_jump_to"], 1)

    def test_external_jumps_are_dropped(self):
        tasks = [_task("A", timeout_jump_to=4), _task("B"), _task("C")]
        snippet = snippets.extract_snippet(tasks, [0])
        self.assertNotIn("timeout_jump_to", snippet[0], "指向片段外部的跳转没有意义，应丢弃")

    def test_partial_selection_remaps_correctly(self):
        tasks = [_task("A"), _task("B"), _task("C", condition_true_jump_to=2)]
        snippet = snippets.extract_snippet(tasks, [1, 2])   # B, C
        self.assertEqual([task["description"] for task in snippet], ["B", "C"])
        self.assertEqual(snippet[1]["condition_true_jump_to"], 1, "原指向 B（片段内第 1 个）")

    def test_internal_fields_are_stripped(self):
        tasks = [_task("A", _outer_step_number=7, __runtime="x")]
        snippet = snippets.extract_snippet(tasks, [0])
        self.assertNotIn("_outer_step_number", snippet[0])
        self.assertNotIn("__runtime", snippet[0])

    def test_deepcopy_isolation(self):
        tasks = [_task("A", match_rects=[[0, 0, 10, 10]])]
        snippet = snippets.extract_snippet(tasks, [0])
        snippet[0]["match_rects"][0][0] = 99
        self.assertEqual(tasks[0]["match_rects"][0][0], 0, "片段必须是深拷贝")

    def test_empty_selection(self):
        self.assertEqual(snippets.extract_snippet([_task("A")], []), [])
        self.assertEqual(snippets.extract_snippet([], [0]), [])

    def test_summary(self):
        self.assertIn("2 步", snippets.snippet_summary([_task("A"), _task("B")]))
        self.assertEqual(snippets.snippet_summary([]), "空片段")


class InsertSnippetTests(unittest.TestCase):
    def setUp(self):
        self.target = [
            _task("T0", timeout_jump_to=2),
            _task("T1"),
        ]
        # 3 步片段：条件跳转指向片段首尾（指向自己会被体检判成 jump_to_self，那是无效数据）
        self.snippet = [
            _task("S0"),
            _task("S1", condition_true_jump_to=3, condition_false_jump_to=1),
            _task("S2"),
        ]

    def test_insert_shifts_existing_jumps(self):
        result = snippets.insert_snippet(self.target, self.snippet, position=1)
        self.assertEqual([task["description"] for task in result],
                         ["T0", "S0", "S1", "S2", "T1"])
        self.assertEqual(result[0]["timeout_jump_to"], 5, "原来指向 T1(2)，插入 3 步后应变成 5")

    def test_snippet_relative_jumps_become_absolute(self):
        result = snippets.insert_snippet(self.target, self.snippet, position=1)
        condition = result[2]
        self.assertEqual(condition["condition_true_jump_to"], 4, "片段第 3 个 → 绝对 4（S2）")
        self.assertEqual(condition["condition_false_jump_to"], 2, "片段第 1 个 → 绝对 2（S0）")

    def test_ids_are_regenerated_and_flow_next_remapped(self):
        snippet = [_task("S0"), _task("S1")]
        snippet[0]["flow_next"] = "S1"          # 片段内部按 id 连接
        result = snippets.insert_snippet([], snippet, position=0)
        ids = [task["id"] for task in result]
        self.assertEqual(len(set(ids)), 2, "id 必须重新生成且唯一")
        self.assertEqual(result[0]["flow_next"], result[1]["id"], "内部连接应指向副本，而不是原片段")

    def test_dangling_flow_next_is_dropped(self):
        snippet = [_task("S0")]
        snippet[0]["flow_next"] = "不存在"
        result = snippets.insert_snippet([], snippet, position=0)
        self.assertNotIn("flow_next", result[0])

    def test_insert_positions(self):
        self.assertEqual([t["description"] for t in snippets.insert_snippet(self.target, [_task("S")], position=0)],
                         ["S", "T0", "T1"])
        self.assertEqual([t["description"] for t in snippets.insert_snippet(self.target, [_task("S")])],
                         ["T0", "T1", "S"], "不传 position 时追加到末尾")
        self.assertEqual(len(snippets.insert_snippet(self.target, [_task("S")], position=99)), 3,
                         "越界位置应夹到末尾")
        self.assertEqual([t["description"] for t in snippets.insert_snippet(self.target, [])], ["T0", "T1"])

    def test_inserted_list_keeps_all_jumps_in_range(self):
        result = snippets.insert_snippet(self.target, self.snippet, position=1)
        issues = preflight.check_tasks(result, icon_dir=tempfile.gettempdir(), screen_size=(1920, 1080))
        jump_codes = {issue.code for issue in issues}
        self.assertFalse({"jump_out_of_range", "jump_to_self"} & jump_codes, jump_codes)


class SnippetStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks

    def tearDown(self):
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def _write(self, payload):
        with io.open(tasks.PRESETS_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

    def test_loader_reads_reserved_key(self):
        self._write({"__snippets__": {"常用": [_task("A")]}})
        loaded = tasks.load_snippets()
        self.assertEqual(list(loaded), ["常用"])
        self.assertEqual(loaded["常用"][0]["description"], "A")

    def test_reserved_key_is_not_listed_as_preset(self):
        self._write({"预设A": [_task("A")], "__snippets__": {"常用": [_task("B")]}})
        presets = tasks.load_presets()
        self.assertIn("预设A", presets)
        self.assertNotIn("__snippets__", presets, "保留键不能被当成预设")

    def test_reserved_key_is_dropped_from_preset_export_payload_extraction(self):
        self._write({"__snippets__": {"常用": [_task("B")]}})
        self.assertEqual(tasks.load_snippets()["常用"][0]["description"], "B")


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

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def _logs(self):
        return self.window.log_box.toPlainText()

    def test_save_selection_writes_snippet_and_persists(self):
        g.TASKS[:] = [_task("A"), _task("B", condition_true_jump_to=1), _task("C")]
        with mock.patch.object(self.window, "_selected_task_indices", lambda: [0, 1]), \
                mock.patch.object(g.QInputDialog, "getText", staticmethod(lambda *a, **k: ("常用组", True))):
            self.window.save_selection_as_snippet()
        self.assertIn("常用组", self.window.snippets)
        self.assertEqual(len(self.window.snippets["常用组"]), 2)
        with io.open(tasks.PRESETS_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIn("__snippets__", payload)
        self.assertEqual(len(payload["__snippets__"]["常用组"]), 2)
        self.assertIn("已保存片段", self._logs())

    def test_insert_snippet_after_current_step(self):
        self.window.snippets = {"常用组": [_task("S0"), _task("S1")]}
        g.TASKS[:] = [_task("A"), _task("B", timeout_jump_to=2)]
        self.window.current_task_index = 0
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("常用组", True))):
            self.window.insert_snippet_at_selection()
        self.assertEqual([task["description"] for task in g.TASKS], ["A", "S0", "S1", "B"])
        self.assertEqual(g.TASKS[3]["timeout_jump_to"], 4, "B 原本指向自己（第 2 步），插入 2 步后应变第 4 步")
        self.assertIn("已插入片段", self._logs())

    def test_save_without_selection_warns(self):
        with mock.patch.object(self.window, "_selected_task_indices", lambda: []), \
                mock.patch.object(g.QMessageBox, "warning", return_value=None) as warning:
            self.window.save_selection_as_snippet()
        self.assertTrue(warning.called)

    def test_insert_without_snippets_warns(self):
        with mock.patch.object(g.QMessageBox, "warning", return_value=None) as warning:
            self.window.insert_snippet_at_selection()
        self.assertTrue(warning.called)

    def test_delete_snippet(self):
        self.window.snippets = {"临时": [_task("S")]}
        with mock.patch.object(g.QInputDialog, "getItem", staticmethod(lambda *a, **k: ("临时", True))), \
                mock.patch.object(g.QMessageBox, "question",
                                  return_value=g.QMessageBox.StandardButton.Yes):
            self.window.delete_snippet()
        self.assertEqual(self.window.snippets, {})
        self.assertIn("已删除片段", self._logs())

    def test_tool_menu_has_snippet_entry(self):
        """「片段」从按钮区移进了「工具」菜单（2026-10-26 按钮区瘦身）。"""
        self.assertIn("片段", self.window.tool_actions)
        self.assertIsNotNone(self.window.tool_button.menu())


if __name__ == "__main__":
    unittest.main()
