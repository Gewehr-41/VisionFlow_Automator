# -*- coding: utf-8 -*-
"""步骤级重试（F1）与执行前体检（F2）。

F1 语义（2026-10-26 新增）
------------------------
`retry_count` / `retry_interval`：**连续未识别到** retry_count 次之后才判定失败
（走迂回 / 超时 / 可选跳过）。默认 0 次 = 关闭，老数据行为不变。
重试只插入"等待 + 再抓一次"，且**不重置 start_time**，所以总时长仍受 timeout 约束。

F2 语义
-------
`preflight.check_tasks()` 纯数据检查；`_preflight_before_run()` 在开跑前弹一个确认框。
没有问题时完全不弹窗（与加这个功能之前一模一样）。
"""

import copy
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import execution_control  # noqa: E402
import main  # noqa: E402
import preflight  # noqa: E402
import tasks  # noqa: E402
from execution_control import ExecutionControlMixin  # noqa: E402

_APP = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# F1：引擎里的重试
# ---------------------------------------------------------------------------

class StepRetryEngineTests(unittest.TestCase):
    """用替身替换截图/匹配/点击，验证重试的触发时机与顺序。"""

    def setUp(self):
        self.logs = []
        self.miss_count = 0        # 前 N 次识别失败
        self.match_calls = 0
        self.detour_calls = 0
        self.click_calls = 0
        self._patches = {
            "capture_screen": mock.patch.object(main, "capture_screen", lambda *a, **k: object()),
            "match_task_templates": mock.patch.object(main, "match_task_templates", self._match),
            "find_match_by_strategy": mock.patch.object(main, "find_match_by_strategy", self._strategy),
            "click_global_position": mock.patch.object(main, "click_global_position", self._click),
            "wait_for_step_result": mock.patch.object(main, "wait_for_step_result", lambda *a, **k: True),
            "interruptible_sleep": mock.patch.object(main, "interruptible_sleep", lambda seconds, flag: False),
            "detour_active": mock.patch.object(main, "detour_active", lambda task: True),
            "execute_detour_task": mock.patch.object(main, "execute_detour_task", self._detour),
        }
        for patcher in self._patches.values():
            patcher.start()

    def tearDown(self):
        for patcher in self._patches.values():
            patcher.stop()

    def _match(self, *args, **kwargs):
        self.match_calls += 1
        return {}

    def _strategy(self, results, template_names, strategy=None):
        if self.match_calls > self.miss_count:
            return template_names[0], (10, 10), 0.99
        return None, None, 0.0

    def _click(self, *args, **kwargs):
        self.click_calls += 1

    def _detour(self, *args, **kwargs):
        self.detour_calls += 1
        return True

    def _run(self, task, **kwargs):
        return main.execute_task(task, log_callback=self.logs.append, **kwargs)

    def _retry_logs(self):
        return [line for line in self.logs if "次重试" in line]

    def test_retry_then_success_does_not_reach_detour(self):
        self.miss_count = 2
        task = {"type": "normal", "template": "a.png", "timeout": 0,
                "retry_count": 3, "retry_interval": 0.0}
        self.assertTrue(self._run(task))
        self.assertEqual(len(self._retry_logs()), 2, "连续失败 2 次应产生 2 条重试日志")
        self.assertEqual(self.detour_calls, 0, "重试成功就不该进入迂回")
        self.assertEqual(self.click_calls, 1)

    def test_retries_exhausted_then_detour(self):
        self.miss_count = 99
        task = {"type": "normal", "template": "a.png", "timeout": 0,
                "retry_count": 2, "retry_interval": 0.0}
        self.assertTrue(self._run(task))
        self.assertEqual(len(self._retry_logs()), 2, "重试次数用完后必须停止重试")
        self.assertEqual(self.detour_calls, 1, "重试用完之后才走迂回")

    def test_without_retry_fields_detour_fires_as_before(self):
        """零行为变更验证：不写 retry_count 时，第一次没认到就走迂回。"""
        self.miss_count = 99
        task = {"type": "normal", "template": "a.png", "timeout": 0}
        self.assertTrue(self._run(task))
        self.assertEqual(self._retry_logs(), [], "没配重试就不该出现重试日志")
        self.assertEqual(self.detour_calls, 1)
        self.assertEqual(self.match_calls, 1, "第一次没认到 → 立即进迂回（旧行为）")

    def test_retry_respects_the_timeout_budget(self):
        """重试不能突破 timeout：超时后仍应抛「必需步骤未出现」。

        这条**不替身 interruptible_sleep**：要让真实时间前进，才能验证
        "重试只在本次尝试的时间预算内进行"。
        """
        self.miss_count = 10 ** 6
        task = {"type": "normal", "template": "a.png", "timeout": 0.05,
                "retry_count": 1000, "retry_interval": 0.02}
        self._patches["interruptible_sleep"].stop()
        try:
            with self.assertRaises(RuntimeError):
                self._run(task, allow_detour=False)
        finally:
            self._patches["interruptible_sleep"].start()
        retries = len(self._retry_logs())
        self.assertGreaterEqual(retries, 1, "应当真的重试过")
        self.assertLess(retries, 1000, "受 timeout 约束，重试不可能真的跑满 1000 次")

    def test_retry_count_zero_is_off(self):
        self.miss_count = 1
        task = {"type": "normal", "template": "a.png", "timeout": 0,
                "retry_count": 0, "retry_interval": 5.0}
        self.assertTrue(self._run(task))
        self.assertEqual(len(self._retry_logs()), 0)


# ---------------------------------------------------------------------------
# F1：编辑器面板的读写
# ---------------------------------------------------------------------------

class RetryEditorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks = copy.deepcopy(g.TASKS)
        g.TASKS[:] = []
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def test_fields_roundtrip(self):
        task = {"type": "normal", "template": "a", "retry_count": 3, "retry_interval": 2.5}
        self.window._load_common_editor_fields(task)
        self.assertEqual(self.window.retry_count_edit.text(), "3")
        self.assertEqual(self.window.retry_interval_edit.text(), "2.5")

        target = {"type": "normal", "template": "a"}
        self.window._apply_common_editor_fields(target)
        self.assertEqual(target["retry_count"], 3)
        self.assertEqual(target["retry_interval"], 2.5)

    def test_missing_fields_default_to_off(self):
        self.window._load_common_editor_fields({"type": "normal", "template": "a"})
        self.assertEqual(self.window.retry_count_edit.text(), "0", "未配置时应显示 0（关闭）")
        self.assertEqual(self.window.retry_interval_edit.text(), str(1.0))

    def test_negative_values_are_clamped(self):
        self.window.retry_count_edit.setText("-5")
        self.window.retry_interval_edit.setText("-1")
        target = {"type": "normal", "template": "a"}
        self.window._apply_common_editor_fields(target)
        self.assertEqual(target["retry_count"], 0)
        self.assertEqual(target["retry_interval"], 0.0)


# ---------------------------------------------------------------------------
# F2：体检规则
# ---------------------------------------------------------------------------

class PreflightRuleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.icon_dir = os.path.join(self._tmp.name, "icons")
        os.makedirs(self.icon_dir)
        for name in ("a", "b", "next"):
            with open(os.path.join(self.icon_dir, name + ".png"), "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _check(self, tasks, screen_size=(1920, 1080)):
        return preflight.check_tasks(tasks, icon_dir=self.icon_dir, screen_size=screen_size)

    def _codes(self, issues):
        return [issue.code for issue in issues]

    def test_clean_step_has_no_issues(self):
        task = {"id": "x", "type": "normal", "template": "a", "threshold": 0.8,
                "timeout": 10, "click_x": 100, "click_y": 200,
                "match_rects": [[0, 0, 100, 100]]}
        self.assertEqual(self._check([task]), [])

    def test_template_unset_is_fatal(self):
        issues = self._check([{"id": "x", "type": "normal", "template": ""}])
        self.assertIn("template_unset", self._codes(issues))
        self.assertEqual(issues[0].level, preflight.FATAL)

    def test_template_missing_is_warning(self):
        issues = self._check([{"id": "x", "type": "normal", "template": "zzz"}])
        self.assertIn("template_missing", self._codes(issues))
        self.assertEqual(issues[0].level, preflight.WARNING)

    def test_template_placeholder_is_flagged(self):
        for value in ("None", "new_step"):
            issues = self._check([{"id": "x", "type": "normal", "template": value}])
            self.assertIn("template_placeholder", self._codes(issues), value)

    def test_next_template_required_for_next_appear(self):
        issues = self._check([{"id": "x", "type": "normal", "template": "a", "wait_for": "next_appear"}])
        self.assertIn("next_template_missing", self._codes(issues))
        self.assertEqual(issues[0].level, preflight.FATAL)

    def test_placeholder_next_template_is_fixable(self):
        task = {"id": "x", "type": "normal", "template": "a", "wait_for": "next_appear",
                "next_template": "None", "next_templates": ["None"]}
        issues = self._check([task])
        self.assertIn("next_template_placeholder", self._codes(issues))
        issues[0].fixable = True

        fixes = preflight.apply_safe_fixes([task], icon_dir=self.icon_dir)
        self.assertEqual(len(fixes), 1)
        self.assertNotIn("next_template", task)
        self.assertEqual(task["wait_for"], "time")
        self.assertEqual(self._check([task]), [], "修完之后不该再报同一问题")

    def test_duplicate_id(self):
        issues = self._check([
            {"id": "same", "type": "delay"},
            {"id": "same", "type": "delay"},
        ])
        self.assertIn("duplicate_id", self._codes(issues))

    def test_jump_target_rules(self):
        tasks = [
            {"id": "a", "type": "normal", "template": "a", "timeout": 5},
            {"id": "b", "type": "delay", "enabled": False},
            {"id": "c", "type": "normal", "template": "a", "timeout": 5},
        ]
        tasks[0]["timeout_jump_to"] = 99        # 越界
        tasks[0]["detour_jump_to"] = 1          # 指向自己
        tasks[0]["detour_success_jump_to"] = 2  # 指向已禁用步骤
        codes = self._codes(self._check(tasks))
        self.assertIn("jump_out_of_range", codes)
        self.assertIn("jump_to_self", codes)
        self.assertIn("jump_to_disabled", codes)

    def test_flow_next_missing_is_fixable(self):
        task = {"id": "a", "type": "normal", "template": "a", "timeout": 5,
                "flow_next": "not-exist"}
        issues = self._check([task])
        self.assertIn("flow_next_missing", self._codes(issues))
        self.assertTrue([i for i in issues if i.code == "flow_next_missing"][0].fixable)

        fixes = preflight.apply_safe_fixes([task], icon_dir=self.icon_dir)
        self.assertEqual(len(fixes), 1)
        self.assertNotIn("flow_next", task)
        self.assertTrue(task["flow_next_disabled"])

    def test_coordinate_out_of_screen(self):
        issues = self._check([{"id": "x", "type": "normal", "template": "a",
                              "click_x": 5000, "click_y": 10}])
        self.assertIn("coord_out_of_screen", self._codes(issues))

    def test_match_rect_invalid(self):
        issues = self._check([{"id": "x", "type": "normal", "template": "a",
                              "match_rects": [[100, 100, 10, 10]]}])
        self.assertIn("match_rect_invalid", self._codes(issues))

    def test_click_until_gone_without_template_is_ok_when_stop_on_change(self):
        issues = self._check([{"id": "x", "type": "click_until_gone", "stop_on_change": True}])
        self.assertNotIn("template_unset", self._codes(issues))

    def test_disabled_steps_are_not_checked(self):
        issues = self._check([{"id": "x", "type": "normal", "enabled": False, "template": ""}])
        self.assertEqual(issues, [])


# ---------------------------------------------------------------------------
# F2：开跑前的闸门（对话框交互）
# ---------------------------------------------------------------------------

class _FakeSignal:
    def connect(self, *args, **kwargs):
        pass


class _FakeWorker:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.log = _FakeSignal()
        self.execution_started = _FakeSignal()
        self.execution_result = _FakeSignal()
        self.completed = _FakeSignal()
        self.finished = _FakeSignal()

    def moveToThread(self, *args):
        pass

    def run(self):
        pass


class _FakeThread:
    def __init__(self, *args, **kwargs):
        self.started = types.SimpleNamespace(connect=lambda *a, **k: None)
        self.finished = types.SimpleNamespace(connect=lambda *a, **k: None)

    def isRunning(self):
        return False

    def start(self):
        pass

    def quit(self):
        pass

    def deleteLater(self):
        pass


class PreflightGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks = copy.deepcopy(g.TASKS)
        self.window = g.PySide6ScriptWindow()
        for patcher in (
            mock.patch.object(ExecutionControlMixin, "_start_stop_hotkey", lambda self: None),
            mock.patch("execution_control.QThread", _FakeThread),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def _start(self, decision):
        with mock.patch("execution_control.TaskWorker", _FakeWorker), \
                mock.patch.object(execution_control.PreflightDialog, "exec", lambda self: 0), \
                mock.patch.object(execution_control.PreflightDialog, "decision", lambda self: decision), \
                mock.patch.object(self.window, "selected_tasks", lambda: list(g.TASKS)):
            self.window._start_worker(None)
        return self.window.worker

    def test_clean_data_starts_without_dialog(self):
        g.TASKS[:] = [{"id": "ok", "type": "delay", "enabled": True, "duration": 0.1}]
        with mock.patch("execution_control.PreflightDialog", side_effect=AssertionError("不该弹窗")), \
                mock.patch("execution_control.TaskWorker", _FakeWorker), \
                mock.patch.object(self.window, "selected_tasks", lambda: list(g.TASKS)):
            self.window._start_worker(None)
        self.assertIsNotNone(self.window.worker)

    def test_cancel_does_not_start(self):
        g.TASKS[:] = [{"id": "bad", "type": "normal", "enabled": True, "template": ""}]
        self.assertIsNone(self._start(execution_control.PreflightDialog.CANCEL))

    def test_keep_going_still_starts(self):
        g.TASKS[:] = [{"id": "bad", "type": "normal", "enabled": True, "template": ""}]
        self.assertIsNotNone(self._start(execution_control.PreflightDialog.CONTINUE))

    def test_safe_fix_button_repairs_and_starts(self):
        g.TASKS[:] = [{"id": "bad", "type": "normal", "enabled": True, "template": "a",
                       "wait_for": "next_appear", "next_template": "None"}]
        self.assertIsNotNone(self._start(execution_control.PreflightDialog.FIX))
        self.assertEqual(g.TASKS[0]["wait_for"], "time")
        self.assertNotIn("next_template", g.TASKS[0])


if __name__ == "__main__":
    unittest.main()
