"""config 处理的两处结构缺陷回归测试。

1. 运行期改写 config 不还原
   `_start_worker` 会把 `config.TARGET_WINDOW_TITLE` / `config.USE_WINDOW_MODE`
   改成当前选择的目标窗口，而这两个是**模块级全局**。此前改完就不管，于是
   脚本停止后 config 仍指向上次的目标窗口，影响之后所有读取它的代码。
   现在运行前记快照、`on_worker_finished` 收尾时还原。

2. 模板目录职责混乱
   `_save_captured_image`（截图保存）此前硬编码 `<脚本目录>/icons`，而模板
   加载走 `config.ICON_DIR`。一旦 ICON_DIR 被改过，截图存到 A 而加载读 B，
   新绑定的图片会"存了却找不到"。
"""

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

import tasks

_APP = QApplication.instance() or QApplication([])

import config
import execution_control
import gui_pyside6 as g

_ROOT = os.path.dirname(os.path.abspath(g.__file__))
_SCRIPT_ICON_DIR = os.path.join(_ROOT, "icons")


class _FakeSignal:
    def connect(self, *args, **kwargs):
        pass


class _FakeThread:
    """替身线程：不真的起线程，只让 _start_worker 能跑完。"""

    def __init__(self, *args, **kwargs):
        self.started = _FakeSignal()
        self.finished = _FakeSignal()

    def isRunning(self):
        return False

    def start(self):
        pass

    def quit(self):
        pass

    def wait(self, *args):
        return True

    def deleteLater(self):
        pass


class _FakeWorker:
    """替身 worker：不起线程、不执行任何任务。"""

    def __init__(self, *args, **kwargs):
        self.log = _FakeSignal()
        self.execution_started = _FakeSignal()
        self.execution_result = _FakeSignal()
        self.completed = _FakeSignal()
        self.finished = _FakeSignal()

    def moveToThread(self, thread):
        pass

    def run(self):
        pass


class _GuiTestCase(unittest.TestCase):
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
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()


class ConfigRestoreUnitTests(_GuiTestCase):
    def test_snapshot_is_none_before_any_run(self):
        self.assertIsNone(self.window._saved_config)

    def test_restore_is_noop_when_never_started(self):
        title_before, mode_before = config.TARGET_WINDOW_TITLE, config.USE_WINDOW_MODE
        self.window._restore_config_after_run()
        self.assertEqual(config.TARGET_WINDOW_TITLE, title_before)
        self.assertEqual(config.USE_WINDOW_MODE, mode_before)

    def test_restore_puts_back_the_snapshot(self):
        self.window._saved_config = ("原来的窗口", False)
        config.TARGET_WINDOW_TITLE = "被改过的窗口"
        config.USE_WINDOW_MODE = True

        self.window._restore_config_after_run()

        self.assertEqual(config.TARGET_WINDOW_TITLE, "原来的窗口")
        self.assertFalse(config.USE_WINDOW_MODE)
        self.assertIsNone(self.window._saved_config, "还原后应清空快照")

    def test_restore_is_idempotent(self):
        self.window._saved_config = ("原值", False)
        config.TARGET_WINDOW_TITLE = "改过"
        self.window._restore_config_after_run()
        config.TARGET_WINDOW_TITLE = "又改了一次"
        self.window._restore_config_after_run()
        self.assertEqual(config.TARGET_WINDOW_TITLE, "又改了一次",
                         "第二次还原不该再动 config（快照已清空）")

    def test_restore_handles_none_title(self):
        self.window._saved_config = (None, False)
        config.TARGET_WINDOW_TITLE = "某窗口"
        config.USE_WINDOW_MODE = True
        self.window._restore_config_after_run()
        self.assertIsNone(config.TARGET_WINDOW_TITLE)
        self.assertFalse(config.USE_WINDOW_MODE)


class ConfigRestoreWiringTests(_GuiTestCase):
    """真的调用 _start_worker / on_worker_finished，验证接线。

    ⚠️ 拦 worker / 线程必须替换 **`execution_control`** 里的名字：这两个类现在是
    由 `execution_control.py` 导入并实例化的（`_start_worker` 已从窗口类外移）。
    只替换 `g.TaskWorker` 在代码搬走后会失效 —— 测试会**真的启动一个 worker 线程**
    （去截屏、点击），而断言因为恰好仍成立而**不会失败**。
    所以下面还显式断言"用到的确实是替身"，把这个缝锁死。
    """

    def setUp(self):
        super().setUp()
        self._orig_thread = execution_control.QThread
        self._orig_worker = execution_control.TaskWorker
        self._orig_g_thread = g.QThread
        self._orig_g_worker = g.TaskWorker
        self._orig_start_hotkey = self.window._start_stop_hotkey
        self._orig_stop_hotkey = self.window._stop_stop_hotkey
        execution_control.QThread = _FakeThread
        execution_control.TaskWorker = _FakeWorker
        g.QThread = _FakeThread
        g.TaskWorker = _FakeWorker
        self.window._start_stop_hotkey = lambda: None
        self.window._stop_stop_hotkey = lambda: None
        self.orig_title = config.TARGET_WINDOW_TITLE
        self.orig_mode = config.USE_WINDOW_MODE

    def tearDown(self):
        execution_control.QThread = self._orig_thread
        execution_control.TaskWorker = self._orig_worker
        g.QThread = self._orig_g_thread
        g.TaskWorker = self._orig_g_worker
        config.TARGET_WINDOW_TITLE = self.orig_title
        config.USE_WINDOW_MODE = self.orig_mode
        super().tearDown()

    def _start(self):
        self.window.window_combo.clear()
        self.window.window_combo.addItem("测试目标窗口")
        self.window.window_combo.setCurrentIndex(0)
        self.window._start_worker(None)
        # 缝是否真的生效：用到的必须是替身，绝不能是真 worker/真线程
        self.assertIsInstance(self.window.worker, _FakeWorker,
                              "worker 替身没生效 —— 会真的启动脚本（截屏+点击）")
        self.assertIsInstance(self.window.worker_thread, _FakeThread, "线程替身没生效")

    def test_start_worker_snapshots_config_before_overwriting(self):
        self.assertTrue(self.window.selected_tasks(), "预设里没有启用步骤，无法测试")
        self._start()

        self.assertEqual(
            self.window._saved_config, (self.orig_title, self.orig_mode),
            "启动时必须先记录 config 原值",
        )
        self.assertEqual(config.TARGET_WINDOW_TITLE, "测试目标窗口")
        self.assertTrue(config.USE_WINDOW_MODE)

    def test_worker_finished_restores_config(self):
        self.assertTrue(self.window.selected_tasks(), "预设里没有启用步骤，无法测试")
        self._start()
        self.assertEqual(config.TARGET_WINDOW_TITLE, "测试目标窗口")

        self.window.on_worker_finished()

        self.assertEqual(config.TARGET_WINDOW_TITLE, self.orig_title,
                         "脚本结束后 config 应还原，不该留上次的目标窗口")
        self.assertEqual(config.USE_WINDOW_MODE, self.orig_mode)
        self.assertIsNone(self.window._saved_config)


class IconDirTests(_GuiTestCase):
    """截图保存必须落在 config.ICON_DIR（与模板加载同一个目录）。

    本测试的判定方式就是"检测有没有文件落到真实 icons 目录"，所以在实现退回
    硬编码目录时，**文件确实会被写进去**（然后断言失败）。为了让测试在任何
    实现下都不污染用户的模板目录，tearDown 会把真实 icons 目录里**本测试期间
    新增**的文件删掉（原有模板一律不动）。
    """

    def setUp(self):
        super().setUp()
        self._orig_icon_dir = config.ICON_DIR
        self._orig_primary = g.QApplication.primaryScreen
        self._orig_warning = g.QMessageBox.warning
        self._orig_reload = g.reload_templates
        self.warnings = []
        self._script_icons_before = self._script_icons_snapshot()
        config.ICON_DIR = os.path.join(self._tmp.name, "我的模板")
        g.QMessageBox.warning = staticmethod(
            lambda *args: self.warnings.append(args[2] if len(args) > 2 else ""))
        g.reload_templates = lambda: None
        g.QApplication.primaryScreen = staticmethod(lambda: self)

    def tearDown(self):
        self._cleanup_script_icons()
        config.ICON_DIR = self._orig_icon_dir
        g.QApplication.primaryScreen = self._orig_primary
        g.QMessageBox.warning = self._orig_warning
        g.reload_templates = self._orig_reload
        super().tearDown()

    # ---------- 真实 icons 目录的保护 ----------

    def _script_icons_snapshot(self):
        if not os.path.isdir(_SCRIPT_ICON_DIR):
            return set()
        return set(os.listdir(_SCRIPT_ICON_DIR))

    def _cleanup_script_icons(self):
        """删掉本测试期间往真实 icons 目录新增的文件。"""
        for name in self._script_icons_snapshot() - self._script_icons_before:
            target = os.path.join(_SCRIPT_ICON_DIR, name)
            if os.path.isfile(target):
                os.remove(target)

    # 供 fake primaryScreen 使用
    def grabWindow(self, *args):
        pixmap = QPixmap(8, 8)
        pixmap.fill()
        return pixmap

    def _icons_dir_file_count(self):
        return len(self._script_icons_snapshot())

    def test_captured_image_lands_in_config_icon_dir(self):
        before = self._icons_dir_file_count()

        self.window._save_captured_image(0, 0, 8, 8, None)

        self.assertEqual(self.warnings, [], f"保存过程报了警告: {self.warnings}")
        saved = os.listdir(config.ICON_DIR)
        self.assertEqual(len(saved), 1, f"截图应落在 config.ICON_DIR，实际: {saved}")
        self.assertTrue(saved[0].startswith("captured_"))
        self.assertTrue(saved[0].endswith(".png"))
        self.assertEqual(
            self._icons_dir_file_count(), before,
            "截图不该再落到 <脚本目录>/icons（那会与加载目录分叉）",
        )

    def test_callback_receives_name_that_resolves_inside_icon_dir(self):
        """回调给的是 ("image", 图片名)——该名字必须能在 config.ICON_DIR 里找到。"""
        received = []
        self.window._save_captured_image(0, 0, 8, 8, lambda result: received.append(result))

        self.assertEqual(len(received), 1, f"回调未被调用: {received}")
        kind, image_name = received[0]
        self.assertEqual(kind, "image")
        self.assertTrue(
            os.path.isfile(os.path.join(config.ICON_DIR, f"{image_name}.png")),
            f"回调返回的名字在 config.ICON_DIR 里找不到: {image_name}",
        )


if __name__ == "__main__":
    unittest.main()
