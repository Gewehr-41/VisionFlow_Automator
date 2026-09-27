"""模板按名过滤测试（批次 4 性能修复）。

背景：wait_until_* / click_template_name / execute_condition_task 只需要
1 个或几个模板，却曾把整个模板库（清理后 62 个）传进 match_all_templates。
多尺度下每轮会做「模板总数 × 尺度数」次 matchTemplate，实测在 800x600 的
画面上单轮耗时约 9 秒，而配置的轮询间隔是 0.1 秒 —— 实际慢了约 90 倍。

本测试锁定 templates_for_names 的语义，并验证它确实缩小了匹配集合。
"""

import unittest

import numpy as np

import main


class TemplatesForNamesTests(unittest.TestCase):
    def setUp(self):
        # 隔离全局模板库，避免依赖 icons 目录的真实内容
        self._original = main.templates
        main.templates = {
            "alpha": np.zeros((10, 10, 3), dtype=np.uint8),
            "beta": np.ones((10, 10, 3), dtype=np.uint8),
            "gamma": np.full((10, 10, 3), 2, dtype=np.uint8),
        }

    def tearDown(self):
        main.templates = self._original

    def test_single_name(self):
        result = main.templates_for_names("alpha")
        self.assertEqual(list(result), ["alpha"])

    def test_comma_separated_names(self):
        result = main.templates_for_names("alpha,beta")
        self.assertEqual(sorted(result), ["alpha", "beta"])

    def test_list_input(self):
        result = main.templates_for_names(["alpha", "gamma"])
        self.assertEqual(sorted(result), ["alpha", "gamma"])

    def test_missing_names_are_ignored(self):
        """不存在的模板名应被跳过，而不是让调用方拿到 KeyError。"""
        result = main.templates_for_names(["alpha", "不存在的模板"])
        self.assertEqual(list(result), ["alpha"])

    def test_all_missing_returns_empty(self):
        self.assertEqual(main.templates_for_names(["甲", "乙"]), {})

    def test_empty_input_returns_empty(self):
        for value in (None, "", [], (), "  ", ","):
            with self.subTest(value=value):
                self.assertEqual(main.templates_for_names(value), {})

    def test_duplicates_collapse(self):
        result = main.templates_for_names(["alpha", "alpha", "alpha"])
        self.assertEqual(list(result), ["alpha"])

    def test_result_is_strict_subset_of_library(self):
        """按名过滤后应显著小于整个模板库。"""
        result = main.templates_for_names("alpha")
        self.assertLess(len(result), len(main.templates))

    def test_chinese_comma_separator(self):
        result = main.templates_for_names("alpha，beta")
        self.assertEqual(sorted(result), ["alpha", "beta"])

    def test_whitespace_is_stripped(self):
        result = main.templates_for_names("  alpha ,  beta  ")
        self.assertEqual(sorted(result), ["alpha", "beta"])


class WaitFunctionsUseFilteredTemplatesTests(unittest.TestCase):
    """验证等待类函数只匹配指定模板（通过替换全局模板库观察）。"""

    def setUp(self):
        self._original_templates = main.templates
        self._original_capture = main.capture_screen
        self._original_sleep = main.interruptible_sleep
        self._original_match = main.match_all_templates
        main.templates = {
            "wanted": np.zeros((8, 8, 3), dtype=np.uint8),
            "unwanted": np.zeros((8, 8, 3), dtype=np.uint8),
        }
        self.matched_names = []
        main.capture_screen = lambda: np.zeros((60, 60, 3), dtype=np.uint8)
        # 让等待循环立刻返回，避免测试挂住
        main.interruptible_sleep = lambda duration, stop_flag=None: True

        original_match = self._original_match
        recorded = self.matched_names

        def spy(screen_img, templates_dict, *args, **kwargs):
            recorded.append(set(templates_dict))
            return original_match(screen_img, templates_dict, *args, **kwargs)

        main.match_all_templates = spy

    def tearDown(self):
        main.templates = self._original_templates
        main.capture_screen = self._original_capture
        main.interruptible_sleep = self._original_sleep
        main.match_all_templates = self._original_match

    def test_wait_until_appears_only_matches_named_template(self):
        main.wait_until_template_appears("wanted", timeout=0.01)
        self.assertTrue(self.matched_names, "应至少匹配过一次")
        for names in self.matched_names:
            self.assertEqual(names, {"wanted"}, "不应把整个模板库传进去")

    def test_wait_until_disappears_only_matches_named_template(self):
        main.wait_until_template_disappears("wanted", timeout=0.01)
        self.assertTrue(self.matched_names, "应至少匹配过一次")
        for names in self.matched_names:
            self.assertEqual(names, {"wanted"})

    def test_wait_until_any_only_matches_requested_names(self):
        main.wait_until_any_template_appears(["wanted"], timeout=0.01)
        self.assertTrue(self.matched_names, "应至少匹配过一次")
        for names in self.matched_names:
            self.assertEqual(names, {"wanted"})


if __name__ == "__main__":
    unittest.main()
