# -*- coding: utf-8 -*-
"""多模板候选时的选择策略（步骤字段 `match_strategy`）测试。

背景（所有者 2026-09-28 决定）：normal 与 advanced 的唯一差别就是"绑定了多张候选模板
时挑哪一张"，与其让用户去猜两种类型的区别，不如把这个差别做成显式的下拉框：
    选置信度最高的那张 / 选屏幕最左的 / 最右的 / 最上的 / 最下的
默认"置信度最高"；旧的 advanced 步骤语义等于"最左"，所以未迁移的数据也保持原行为。
"""
import unittest

import numpy as np

import main


class MatchStrategySelectionTests(unittest.TestCase):
    """`get_task_match_strategy` 的取值与回退。"""

    def test_defaults_to_confidence(self):
        self.assertEqual(main.get_task_match_strategy({"type": "normal"}), "confidence")
        self.assertEqual(main.get_task_match_strategy({}), "confidence")
        self.assertEqual(main.get_task_match_strategy(None), "confidence")
        self.assertEqual(main.DEFAULT_MATCH_STRATEGY, "confidence")

    def test_explicit_value_wins(self):
        for key, _label in main.MATCH_STRATEGIES:
            self.assertEqual(
                main.get_task_match_strategy({"type": "normal", "match_strategy": key}), key
            )

    def test_invalid_value_falls_back_to_default(self):
        self.assertEqual(
            main.get_task_match_strategy({"type": "normal", "match_strategy": "随便写的"}),
            "confidence",
        )

    def test_legacy_advanced_without_the_field_means_leftmost(self):
        """迁移前的老数据：`advanced` 没有 match_strategy，历史语义就是最左。"""
        self.assertEqual(main.get_task_match_strategy({"type": "advanced"}), "leftmost")
        # 显式写了策略时以字段为准（迁移后的 normal + leftmost 也走这里）
        self.assertEqual(
            main.get_task_match_strategy({"type": "advanced", "match_strategy": "rightmost"}),
            "rightmost",
        )


class FindMatchByStrategyTests(unittest.TestCase):
    """`find_match_by_strategy` 的挑法（用构造好的候选表，不涉及真实截图）。"""

    def setUp(self):
        # 三个候选：左边分低、下边分高、右上分最高
        self.results = {
            "left_low": ((100, 500), 0.82),
            "bottom_high": ((600, 900), 0.95),
            "top_right_highest": ((1200, 200), 0.99),
            "no_hit": (None, -1.0),
        }
        self.names = ["left_low", "bottom_high", "top_right_highest", "no_hit"]

    def test_confidence_picks_the_highest(self):
        name, center, conf = main.find_match_by_strategy(self.results, self.names, "confidence")
        self.assertEqual(name, "top_right_highest")
        self.assertEqual(center, (1200, 200))
        self.assertEqual(conf, 0.99)

    def test_leftmost(self):
        name, center, _ = main.find_match_by_strategy(self.results, self.names, "leftmost")
        self.assertEqual((name, center), ("left_low", (100, 500)))

    def test_rightmost(self):
        name, center, _ = main.find_match_by_strategy(self.results, self.names, "rightmost")
        self.assertEqual((name, center), ("top_right_highest", (1200, 200)))

    def test_topmost(self):
        name, center, _ = main.find_match_by_strategy(self.results, self.names, "topmost")
        self.assertEqual((name, center), ("top_right_highest", (1200, 200)))

    def test_bottommost(self):
        name, center, _ = main.find_match_by_strategy(self.results, self.names, "bottommost")
        self.assertEqual((name, center), ("bottom_high", (600, 900)))

    def test_axis_tie_prefers_higher_confidence(self):
        """同一条轴上并列时，位置类策略也要挑置信度更高的那个。"""
        results = {"a": ((300, 100), 0.81), "b": ((300, 100), 0.93)}
        name, _center, conf = main.find_match_by_strategy(results, ["a", "b"], "leftmost")
        self.assertEqual((name, conf), ("b", 0.93))
        name, _center, conf = main.find_match_by_strategy(results, ["a", "b"], "topmost")
        self.assertEqual((name, conf), ("b", 0.93))

    def test_no_hit_returns_none(self):
        name, center, conf = main.find_match_by_strategy(
            {"a": (None, 0.4), "b": (None, -1.0)}, ["a", "b"], "leftmost"
        )
        self.assertIsNone(name)
        self.assertIsNone(center)
        self.assertEqual(conf, -1.0)

    def test_unknown_strategy_behaves_like_confidence(self):
        name, _center, _conf = main.find_match_by_strategy(self.results, self.names, "未知")
        self.assertEqual(name, "top_right_highest")

    def test_find_leftmost_match_is_kept_as_compatibility_alias(self):
        """旧的 `find_leftmost_match` 仍可用，且与 leftmost 策略结果一致。"""
        legacy = main.find_leftmost_match(self.results, self.names)
        new = main.find_match_by_strategy(self.results, self.names, "leftmost")
        self.assertEqual(legacy, new)

    def test_find_first_match_keeps_its_own_semantics(self):
        """`find_first_match`（等待/条件等路径在用）仍是"置信度最高"，未被本次改动改掉。"""
        name, _center, conf = main.find_first_match(self.results, self.names)
        self.assertEqual((name, conf), ("top_right_highest", 0.99))


class StrategyUsedByTaskTests(unittest.TestCase):
    """步骤字典 -> 策略 -> 挑哪张，端到端（仍不涉及真实截图与窗口）。"""

    def test_task_level_strategy_is_honoured(self):
        results = {"a": ((900, 100), 0.99), "b": ((100, 800), 0.85)}
        names = ["a", "b"]
        pick = lambda task: main.find_match_by_strategy(
            results, names, main.get_task_match_strategy(task)
        )[0]
        self.assertEqual(pick({"type": "normal"}), "a")
        self.assertEqual(pick({"type": "normal", "match_strategy": "leftmost"}), "b")
        self.assertEqual(pick({"type": "normal", "match_strategy": "bottommost"}), "b")
        self.assertEqual(pick({"type": "advanced"}), "b", "旧 advanced 未迁移时仍是'最左'")


if __name__ == "__main__":
    unittest.main()
