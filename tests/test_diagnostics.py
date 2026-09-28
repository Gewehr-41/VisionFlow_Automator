"""运行期诊断信息「必须真的可见」的测试。

背景
----
`启动新界面.bat` 用 `-WindowStyle Hidden` 跑 python，**stdout / stderr 用户都看不到**，
而项目里根本没有任何重定向（无 `excepthook`、无 `redirect_stderr`）。所以"打印一行"
并不等于"用户会知道"。此前：

  - `tasks.py` 的 6 个数据加载器里，4 个是**静默** `except Exception: return ...`；
  - `core/template_match.py`（模板文件读不了）与 `core/hotkey.py`（**全局热键注册
    失败**）都是裸 `print()`，同样看不见 —— 而热键那条一旦失败，脚本运行期间按
    停止键不会有任何反应，用户却无从得知；
  - `core/template_match.py` 每加载一张模板还打印一行，绑定图片后每次刷 60 多行，
    在隐藏窗口下纯属噪音。

现在所有诊断都走 `diagnostics.warn()`（写 stderr **并**收集），界面用
`_log_pending_warnings()` 把它们搬进日志框 —— 那才是唯一可见的路径。

本文件锁住三件事：
  1. 每个数据加载器读取失败都会产生警告（且有后果说明）
  2. `core/` 的热键与模板警告同样走这条通道；模板加载不再往 stdout 刷屏
  3. 界面会把警告搬进日志框，运行期新增的也能搬（定时器调用同一方法），且不重复
"""

import contextlib
import io
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
from PySide6.QtWidgets import QApplication

import diagnostics
import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g
from core import hotkey as hotkey_module
from core.hotkey import GlobalHotkey
from core import template_match as core_template_match


class _WarnIsolation(unittest.TestCase):
    """清空共享的收集列表，跑完再还原。"""

    def setUp(self):
        self._saved_warnings = diagnostics.snapshot()
        diagnostics.clear()

    def tearDown(self):
        diagnostics.clear()
        with diagnostics._LOCK:
            diagnostics._WARNINGS.extend(self._saved_warnings)


class WarnCollectorTests(_WarnIsolation):
    def test_warn_collects_and_returns_the_message(self):
        message = diagnostics.warn("警告: 测试")
        self.assertEqual(message, "警告: 测试")
        self.assertIn("警告: 测试", diagnostics.snapshot())

    def test_warn_also_writes_stderr(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            diagnostics.warn("警告: 到 stderr")
        self.assertIn("警告: 到 stderr", buffer.getvalue())

    def test_snapshot_returns_a_copy(self):
        diagnostics.warn("警告: 一")
        snapshot = diagnostics.snapshot()
        snapshot.append("外部改动")
        self.assertEqual(len(diagnostics.snapshot()), 1, "snapshot 应是副本，外部改动不影响收集列表")


class LoaderWarningTests(_WarnIsolation):
    """每个数据加载器读取损坏/非预期内容时都必须产生警告。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.corrupt = os.path.join(self._tmp.name, "corrupt.json")
        with open(self.corrupt, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是合法 JSON")
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
        self._tmp.cleanup()
        super().tearDown()

    def _assert_warns(self, loader, expected_fallback):
        result = loader()
        self.assertEqual(result, expected_fallback, f"{loader.__name__} 的回退值不对")
        self.assertTrue(
            diagnostics.snapshot(),
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
            any("重新出现" in message for message in diagnostics.snapshot()),
            f"提示没有说明后果，实际为: {diagnostics.snapshot()}",
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
                diagnostics.clear()
                self.assertEqual(loader(), fallback)
                self.assertEqual(diagnostics.snapshot(), [], "文件不存在不该产生警告")


class CoreModuleWarningTests(_WarnIsolation):
    """`core/` 的警告也要走可见通道，且不再往 stdout 刷屏。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def _make_icon_dir(self, valid=2, broken=1):
        directory = os.path.join(self._tmp.name, "icons")
        os.makedirs(directory, exist_ok=True)
        for index in range(valid):
            ok, buffer = cv2.imencode(".png", np.zeros((6, 6, 3), dtype=np.uint8))
            self.assertTrue(ok)
            with open(os.path.join(directory, f"ok{index}.png"), "wb") as handle:
                handle.write(buffer.tobytes())
        for index in range(broken):
            with open(os.path.join(directory, f"broken{index}.png"), "wb") as handle:
                handle.write("这不是 PNG 内容".encode("utf-8"))
        return directory

    def test_unreadable_template_produces_a_visible_warning(self):
        directory = self._make_icon_dir(valid=1, broken=1)
        with contextlib.redirect_stderr(io.StringIO()):
            core_template_match.load_templates(directory)
        self.assertTrue(
            any("broken0.png" in message for message in diagnostics.snapshot()),
            f"模板读不了却没告警: {diagnostics.snapshot()}",
        )

    def test_loading_templates_is_silent_on_stdout(self):
        """逐张模板打印「加载模板: …」已移除（绑定图片后每次刷 60+ 行，毫无意义）。"""
        directory = self._make_icon_dir(valid=3, broken=0)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            core_template_match.load_templates(directory)
        self.assertEqual(buffer.getvalue().strip(), "",
                         "加载模板不该往 stdout 打印任何东西")

    def test_loading_templates_collects_nothing_when_all_are_fine(self):
        directory = self._make_icon_dir(valid=2, broken=0)
        core_template_match.load_templates(directory)
        self.assertEqual(diagnostics.snapshot(), [], "全部正常时不该产生警告")

    def test_directory_without_usable_templates_still_raises(self):
        """原有行为：一张都读不出来时抛错（不是静默返回空字典）。"""
        directory = self._make_icon_dir(valid=0, broken=2)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError):
                core_template_match.load_templates(directory)

    def test_invalid_hotkey_combo_warns_and_explains_the_consequence(self):
        hotkey = GlobalHotkey()
        with contextlib.redirect_stderr(io.StringIO()):
            hotkey.start("ctrl+不存在的键", lambda: None)
            hotkey.stop()
        messages = diagnostics.snapshot()
        self.assertTrue(any("热键" in message for message in messages),
                        f"无效热键组合没有告警: {messages}")
        self.assertTrue(
            any("停止" in message for message in messages),
            f"提示应说明「热键不可用、请改用停止按钮」这一后果: {messages}",
        )
        self.assertFalse(hotkey.active, "无效组合下不该留下监听线程")

    def test_hotkey_registration_failure_warns(self):
        """热键被别的程序占用时同样要告警。

        这是用户最可能真正遇到的情况：热键没注册上，脚本运行期间按停止键毫无反应，
        而此前那条提示只是裸 print（隐藏窗口下看不见）。
        """

        class _FailingUser32:
            @staticmethod
            def RegisterHotKey(*args):
                return 0

        original_user32 = hotkey_module.user32
        hotkey_module.user32 = _FailingUser32()
        try:
            hotkey = GlobalHotkey()
            with contextlib.redirect_stderr(io.StringIO()):
                hotkey.start("ctrl+alt+f9", lambda: None)
                hotkey.stop()
        finally:
            hotkey_module.user32 = original_user32

        messages = diagnostics.snapshot()
        self.assertTrue(any("注册全局热键失败" in message for message in messages),
                        f"注册失败没有告警: {messages}")
        self.assertTrue(
            any("停止" in message for message in messages),
            f"提示应说明「该热键无法停止脚本、请改用停止按钮」: {messages}",
        )


class WindowShowsWarningsTests(_WarnIsolation):
    """界面必须把诊断信息搬进日志框（启动时 + 运行期新增）。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_presets_file = tasks.PRESETS_FILE
        self._orig_tasks_file = tasks.TASKS_FILE
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks

    def tearDown(self):
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()
        super().tearDown()

    def _window(self):
        window = g.PySide6ScriptWindow()
        self.addCleanup(window.close)
        return window

    def test_startup_logs_collected_warnings(self):
        sentinel = "警告: 测试用的启动期诊断"
        diagnostics.warn(sentinel)
        window = self._window()
        self.assertIn(sentinel, window.log_box.toPlainText())

    def test_startup_delivers_every_collected_warning(self):
        diagnostics.warn("警告: 一")
        diagnostics.warn("警告: 二")
        window = self._window()
        text = window.log_box.toPlainText()
        self.assertIn("警告: 一", text)
        self.assertIn("警告: 二", text)

    def test_no_warnings_means_clean_startup(self):
        window = self._window()
        self.assertEqual(window._log_pending_warnings(), 0)

    def test_warnings_added_after_startup_are_delivered(self):
        """运行期新增的诊断（来自 worker / 热键线程）要能被搬运进日志框。"""
        window = self._window()
        self.assertEqual(window._log_pending_warnings(), 0, "启动时的应已搬完")

        diagnostics.warn("警告: 运行期新增")

        self.assertEqual(window._log_pending_warnings(), 1)
        self.assertIn("警告: 运行期新增", window.log_box.toPlainText())

    def test_delivery_is_idempotent(self):
        window = self._window()
        diagnostics.warn("警告: 只应出现一次")

        window._log_pending_warnings()
        first = window.log_box.toPlainText().count("警告: 只应出现一次")
        window._log_pending_warnings()
        second = window.log_box.toPlainText().count("警告: 只应出现一次")

        self.assertEqual(first, 1)
        self.assertEqual(second, first, "同一批诊断不该被重复搬进日志框")

    def test_warning_timer_is_running(self):
        """运行期诊断靠定时器搬运（跨线程只取快照，不直接碰控件）。"""
        window = self._window()
        self.assertTrue(hasattr(window, "_warning_timer"))
        self.assertTrue(window._warning_timer.isActive())
        self.assertEqual(window._warning_timer.interval(), 500)


if __name__ == "__main__":
    unittest.main()
