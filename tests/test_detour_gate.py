# -*- coding: utf-8 -*-
"""迂回开关的单一判据（§8.1 #76）。

`detour_enabled` 是显式开关：写了就以它为准（False = 明确关闭，即使还留着
`detour_jump_to`）；老数据没有这个字段则沿用「有失败跳转即视为开启」的旧语义。

这里既测判据函数本身，也用替身驱动 `execute_task`，证明**引擎闸门真的用了它** ——
否则「只改判据、闸门还写着旧的『或』」这种漏改不会被发现。
"""

import unittest

import numpy as np

import main
import nodes


class DetourActiveTests(unittest.TestCase):
    def test_explicit_true_wins_even_without_jump(self):
        """只有开关、没有跳转：这是合法的「跑完迂回步骤再继续」。"""
        self.assertTrue(nodes.detour_active({"detour_enabled": True}))

    def test_explicit_false_wins_over_leftover_jump(self):
        self.assertFalse(
            nodes.detour_active({"detour_enabled": False, "detour_jump_to": 7}),
            "显式关闭必须压过残留的跳转字段",
        )

    def test_legacy_row_without_the_field_follows_the_jump(self):
        self.assertTrue(nodes.detour_active({"detour_jump_to": 3}))
        self.assertFalse(nodes.detour_active({}))

    def test_non_dict_input_is_not_active(self):
        for value in (None, "x", 3, []):
            with self.subTest(value=value):
                self.assertFalse(nodes.detour_active(value))


class DetourGateTests(unittest.TestCase):
    """引擎闸门：`execute_task` 在识别不到时必须按 `detour_active` 决定走不走迂回。"""

    def setUp(self):
        self._orig = (main.capture_screen, main.match_task_templates, main.execute_detour_task)
        self.detour_calls = []
        main.capture_screen = lambda: np.zeros((120, 120, 3), dtype=np.uint8)
        # 与真实契约一致：未命中是「每个模板一条 (None, 分数)」，不是空字典
        main.match_task_templates = (
            lambda task, screen_img=None, template_names=None, stop_flag=None, **kwargs:
            {name: (None, 0.0) for name in (template_names or [])}
        )
        main.execute_detour_task = lambda task, **kwargs: (self.detour_calls.append(task), True)[1]
        self.log = lambda *args, **kwargs: None

    def tearDown(self):
        main.capture_screen, main.match_task_templates, main.execute_detour_task = self._orig

    @staticmethod
    def _task(**extra):
        task = {"id": "t", "type": "normal", "description": "闸门测试", "template": "1",
                "click": True, "required": True, "timeout": 0.05, "poll_interval": 0.01,
                "detour_steps": [{"type": "delay", "duration": 0.01}]}
        task.update(extra)
        return task

    def test_explicit_false_never_runs_the_detour(self):
        """关了开关、但跳转字段还在：不能再走迂回，也不该把失败静默吞掉。"""
        task = self._task(detour_enabled=False, detour_jump_to=2)
        with self.assertRaises(RuntimeError):
            main.execute_task(task, log_callback=self.log)
        self.assertEqual(self.detour_calls, [], "显式关闭的步骤不该执行迂回流程")

    def test_legacy_jump_without_the_field_still_runs_the_detour(self):
        task = self._task(detour_jump_to=2)
        result = main.execute_task(task, log_callback=self.log)
        self.assertEqual(len(self.detour_calls), 1, "老数据（无开关字段）仍应按跳转走迂回")
        self.assertEqual(result, {"outer_jump_to": 2})

    def test_explicit_true_runs_the_detour_even_without_any_jump(self):
        task = self._task(detour_enabled=True)
        result = main.execute_task(task, log_callback=self.log)
        self.assertEqual(len(self.detour_calls), 1)
        self.assertTrue(result, "没有跳转目标时应返回 True（继续下一步）")
