# -*- coding: utf-8 -*-
"""F7「全局兜底处理步骤」：引擎分支、数据持久化、worker 透传、编辑对话框。"""

import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main  # noqa: E402
import tasks  # noqa: E402
from PySide6.QtWidgets import QPushButton, QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
from execution_control import ExecutionControlMixin  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class FallbackEngineTests(unittest.TestCase):
    """兜底分支：位置（在迂回之前）、只触发一次、成功则重试当前步骤。"""

    def setUp(self):
        self.logs = []
        self.detour_calls = 0
        self.fallback_labels = []
        self.match_sequence = []      # 依次返回的识别结果；弹尽后返回未命中
        self._patches = [
            mock.patch.object(main, "capture_screen", lambda *a, **k: object()),
            mock.patch.object(main, "match_task_templates", lambda *a, **k: {}),
            mock.patch.object(main, "find_match_by_strategy", self._strategy),
            mock.patch.object(main, "interruptible_sleep", lambda s, f: False),
            mock.patch.object(main, "click_global_position", lambda *a, **k: None),
            mock.patch.object(main, "wait_for_step_result", lambda *a, **k: True),
            mock.patch.object(main, "detour_active", lambda task: True),
            mock.patch.object(main, "execute_detour_task", self._detour),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()

    def _strategy(self, results, template_names, strategy=None):
        if self.match_sequence:
            return self.match_sequence.pop(0)
        return None, None, 0.0

    def _detour(self, *args, **kwargs):
        self.detour_calls += 1
        return True

    def _run(self, task, fallback_steps):
        return main.execute_task(task, log_callback=self.logs.append,
                                 fallback_steps=fallback_steps)

    def _main_step(self, **extra):
        step = {"type": "normal", "template": "main", "timeout": 0, "description": "主步骤"}
        step.update(extra)
        return step

    def test_fallback_runs_before_detour_and_retries_the_step(self):
        # 第 1 次主步骤未命中 → 兜底步骤命中（第 2 次）→ 主步骤再识别命中（第 3 次）
        self.match_sequence = [
            (None, None, 0.0),
            ("popup_close", (5, 5), 0.99),
            ("main", (10, 10), 0.99),
        ]
        result = self._run(self._main_step(), [{"type": "normal", "template": "popup_close",
                                                "timeout": 0, "description": "关弹窗"}])
        self.assertTrue(result)
        self.assertEqual(self.detour_calls, 0, "兜底成功就不该再走迂回")
        self.assertTrue(any("兜底" in line for line in self.logs))

    def test_all_fallbacks_failing_falls_through_to_detour(self):
        # 主步骤未命中 → 兜底步骤永远超时（RuntimeError 被吞掉）→ 走迂回
        self.match_sequence = [(None, None, 0.0)]
        result = self._run(self._main_step(),
                           [{"type": "normal", "template": "nope", "timeout": 0.01,
                             "description": "无效兜底"}])
        self.assertTrue(result)
        self.assertEqual(self.detour_calls, 1, "兜底都没成功时仍要走迂回")
        self.assertTrue(any("兜底步骤出错" in line for line in self.logs))

    def test_fallback_only_once_per_step(self):
        """兜底成功但主步骤仍然识别不到时，不能无限兜底。"""
        self.match_sequence = []      # 主步骤永远未命中
        task = self._main_step(timeout=0.05)
        fallback = [{"type": "delay", "description": "等待", "duration": 0}]
        result = self._run(task, fallback)
        self.assertTrue(result)
        fallback_logs = [line for line in self.logs if "兜底 1/" in line]
        self.assertEqual(len(fallback_logs), 1, "每个步骤只允许一轮兜底")
        self.assertEqual(self.detour_calls, 1, "兜底之后仍失败 → 回到迂回")

    def test_without_fallback_steps_behaviour_is_unchanged(self):
        self.match_sequence = []
        result = self._run(self._main_step(), [])
        self.assertTrue(result)
        self.assertEqual(self.detour_calls, 1)
        self.assertFalse(any("兜底" in line for line in self.logs))


class FallbackStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_presets = tasks.PRESETS_FILE
        self._orig_save_presets = g.save_presets
        self._orig_tasks_file = tasks.TASKS_FILE
        self._orig_save_tasks = g.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        self._presets_snapshot = copy.deepcopy(g.USER_PRESETS)
        self._metadata_snapshot = copy.deepcopy(g.PRESET_METADATA)
        self._fallback_snapshot = copy.deepcopy(g.FALLBACK_STEPS)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks

    def tearDown(self):
        tasks.PRESETS_FILE = self._orig_presets
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        g.TASKS[:] = self._tasks_snapshot
        g.USER_PRESETS.clear()
        g.USER_PRESETS.update(self._presets_snapshot)
        g.PRESET_METADATA.clear()
        g.PRESET_METADATA.update(self._metadata_snapshot)
        g.FALLBACK_STEPS.clear()
        g.FALLBACK_STEPS.update(self._fallback_snapshot)
        self._tmp.cleanup()

    def _write_presets(self, payload):
        with io.open(tasks.PRESETS_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

    def test_loader_reads_reserved_key(self):
        self._write_presets({
            "preset_a": [{"id": "1", "type": "delay"}],
            "__deleted__": [],
            "__group_metadata__": {},
            "__fallback__": {"preset_a": [{"type": "normal", "template": "popup"}]},
        })
        loaded = tasks.load_fallback_steps()
        self.assertEqual(list(loaded), ["preset_a"])
        self.assertEqual(loaded["preset_a"][0]["template"], "popup")

    def test_reserved_key_is_not_listed_as_a_preset(self):
        self._write_presets({
            "preset_a": [{"id": "1", "type": "delay"}],
            "__fallback__": {"preset_a": [{"type": "delay"}]},
        })
        presets = tasks.load_presets()
        self.assertIn("preset_a", presets)
        self.assertNotIn("__fallback__", presets, "保留键不能被当成预设")

    def test_dialog_saves_steps_into_mode_storage(self):
        window = g.PySide6ScriptWindow()
        window.show()

        def fake_exec(dialog):
            # 点「新增步骤」再点「保存」，模拟真实操作
            buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
            buttons["新增步骤"].click()
            buttons["保存"].click()
            return 0

        try:
            with mock.patch.object(g.QDialog, "exec", fake_exec):
                window.open_fallback_editor()
            mode = window.mode_combo.currentText() or "custom"
            self.assertEqual(len(window.mode_fallback_steps.get(mode, [])), 1)
            with io.open(tasks.PRESETS_FILE, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertIn("__fallback__", payload)
            self.assertEqual(len(payload["__fallback__"].get(mode, [])), 1)
        finally:
            window.close()


class FallbackWorkerPlumbingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        self.window = g.PySide6ScriptWindow()
        for patcher in (
            mock.patch.object(ExecutionControlMixin, "_start_stop_hotkey", lambda self: None),
            mock.patch("execution_control.QThread", _FakeThread),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def test_worker_receives_fallback_steps(self):
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.mode_fallback_steps[mode] = [{"type": "delay", "duration": 0}]
        g.TASKS[:] = [{"id": "ok", "type": "delay", "enabled": True, "duration": 0}]
        captured = {}

        class _Worker:
            def __init__(self, *args):
                captured["args"] = args
                for name in ("log", "execution_started", "execution_result", "completed", "finished"):
                    setattr(self, name, _FakeSignal())

            def moveToThread(self, *args):
                pass

            def run(self):
                pass

        with mock.patch("execution_control.TaskWorker", _Worker), \
                mock.patch.object(self.window, "selected_tasks", lambda: list(g.TASKS)), \
                mock.patch.object(self.window, "_target_window_exists", lambda title: True):
            self.window._start_worker(None)
        self.assertEqual(len(captured["args"]), 7, "TaskWorker 应收到 7 个参数（含兜底步骤）")
        self.assertEqual(captured["args"][6][0]["type"], "delay")


class _FakeSignal:
    def connect(self, *args, **kwargs):
        pass


class _FakeThread:
    def __init__(self, *args, **kwargs):
        self.started = _FakeSignal()
        self.finished = _FakeSignal()

    def isRunning(self):
        return False

    def start(self):
        pass

    def quit(self):
        pass

    def deleteLater(self):
        pass


if __name__ == "__main__":
    unittest.main()
