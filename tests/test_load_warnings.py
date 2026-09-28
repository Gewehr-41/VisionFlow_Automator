"""数据文件读取阶段「警告必须真的可见」的测试。

背景
----
`启动新界面.bat` 用 `-WindowStyle Hidden` 跑 python，**stderr 用户根本看不到**，
而 `tasks._warn` 的注释曾写着「GUI 会捕获并显示」——全项目根本没有任何 stderr
捕获（无 excepthook、无 redirect_stderr）。也就是说 6 个数据加载器的失败提示
实际全都是隐形的，用户的预设/蓝图数据可以在毫无提示的情况下"无声消失"。

本文件锁住两件事：
  1. 每个加载器在读取失败时都会产生警告（此前 6 个里有 4 个是静默 return）
  2. 这些警告会被界面写进日志框（真正可见的那条路径）
"""

import json
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g


class WarnCollectorTests(unittest.TestCase):
    def setUp(self):
        self._saved = list(tasks.LOAD_WARNINGS)
        tasks.LOAD_WARNINGS.clear()

    def tearDown(self):
        tasks.LOAD_WARNINGS[:] = self._saved

    def test_warn_appends_and_returns_the_message(self):
        message = tasks._warn("警告: 测试")
        self.assertEqual(message, "警告: 测试")
        self.assertIn("警告: 测试", tasks.LOAD_WARNINGS)

    def test_warn_does_not_swallow_the_return_value(self):
        self.assertEqual(tasks._warn("x"), "x")


class LoaderWarningTests(unittest.TestCase):
    """每个加载器读取损坏/非预期内容时都必须产生警告。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.corrupt = os.path.join(self._tmp.name, "corrupt.json")
        with open(self.corrupt, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是合法 JSON")
        self._saved_warnings = list(tasks.LOAD_WARNINGS)
        tasks.LOAD_WARNINGS.clear()
        self._orig = {
            "TASKS_FILE": tasks.TASKS_FILE,
            "PRESETS_FILE": tasks.PRESETS_FILE,
            "BLUEPRINT_LAYOUT_FILE": tasks.BLUEPRINT_LAYOUT_FILE,
            "BLUEPRINT_GRAPH_FILE": tasks.BLUEPRINT_GRAPH_FILE,
        }
        for name in self._orig:
            setattr(tasks, name, self.corrupt)

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(tasks, name, value)
        tasks.LOAD_WARNINGS[:] = self._saved_warnings
        self._tmp.cleanup()

    def _assert_warns(self, loader, expected_fallback):
        result = loader()
        self.assertEqual(result, expected_fallback, f"{loader.__name__} 的回退值不对")
        self.assertTrue(
            tasks.LOAD_WARNINGS,
            f"{loader.__name__} 读取失败时没有产生任何警告（用户会毫无察觉）",
        )

    def test_load_tasks_warns(self):
        self._assert_warns(tasks.load_tasks, tasks.DEFAULT_TASKS)

    def test_load_presets_warns(self):
        # load_presets 的回退值不含 custom（custom 的数据在 TASKS 里）
        expected = {name: value for name, value in tasks.TASK_PRESETS.items() if name != "custom"}
        self._assert_warns(tasks.load_presets, expected)

    def test_load_deleted_preset_names_warns(self):
        self._assert_warns(tasks.load_deleted_preset_names, set())

    def test_load_preset_metadata_warns(self):
        self._assert_warns(tasks.load_preset_metadata, {})

    def test_load_blueprint_layouts_warns(self):
        self._assert_warns(tasks.load_blueprint_layouts, {})

    def test_load_blueprint_graphs_warns(self):
        self._assert_warns(tasks.load_blueprint_graphs, {})

    def test_deleted_preset_warning_explains_the_real_consequence(self):
        """删除名单读不出来会导致已删预设复活，提示里应说清这一点。"""
        tasks.load_deleted_preset_names()
        self.assertTrue(
            any("重新出现" in message for message in tasks.LOAD_WARNINGS),
            f"提示没有说明后果，实际为: {tasks.LOAD_WARNINGS}",
        )

    def test_missing_file_is_not_a_warning(self):
        """文件不存在属于正常首次运行，不应报错。"""
        for name in self._orig:
            setattr(tasks, name, os.path.join(self._tmp.name, "不存在.json"))
        for loader, fallback in (
            (tasks.load_deleted_preset_names, set()),
            (tasks.load_preset_metadata, {}),
            (tasks.load_blueprint_layouts, {}),
            (tasks.load_blueprint_graphs, {}),
        ):
            with self.subTest(loader=loader.__name__):
                tasks.LOAD_WARNINGS.clear()
                self.assertEqual(loader(), fallback)
                self.assertEqual(tasks.LOAD_WARNINGS, [], "文件不存在不该产生警告")


class WindowShowsLoadWarningsTests(unittest.TestCase):
    """界面启动时必须把读取警告写进日志框（stderr 在隐藏窗口下不可见）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_presets_file = tasks.PRESETS_FILE
        self._orig_tasks_file = tasks.TASKS_FILE
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        self._saved_warnings = list(tasks.LOAD_WARNINGS)

    def tearDown(self):
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        tasks.LOAD_WARNINGS[:] = self._saved_warnings
        self._tmp.cleanup()

    def test_startup_logs_collected_warnings(self):
        sentinel = "警告: 测试用的加载失败提示"
        tasks.LOAD_WARNINGS.clear()
        tasks.LOAD_WARNINGS.append(sentinel)
        window = g.PySide6ScriptWindow()
        try:
            self.assertIn(sentinel, window.log_box.toPlainText())
        finally:
            window.close()

    def test_log_load_warnings_returns_the_count(self):
        tasks.LOAD_WARNINGS.clear()
        tasks.LOAD_WARNINGS.extend(["警告: 一", "警告: 二"])
        window = g.PySide6ScriptWindow()
        try:
            self.assertEqual(window._log_load_warnings(), 2)
            self.assertIn("警告: 一", window.log_box.toPlainText())
            self.assertIn("警告: 二", window.log_box.toPlainText())
        finally:
            window.close()

    def test_no_warnings_means_clean_startup(self):
        tasks.LOAD_WARNINGS.clear()
        window = g.PySide6ScriptWindow()
        try:
            self.assertEqual(window._log_load_warnings(), 0)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
