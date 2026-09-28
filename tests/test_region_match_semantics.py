"""识别区域匹配语义：1 个区域与多个区域必须共用同一份兜底。

背景
----
`main.match_task_templates` 此前按区域**条数**走两套不同策略：

  - 恰好 1 个区域 -> `match_in_expanding_rect`：先在该区域内找，未命中就以区域中心
    为基准**逐次扩大到整张截图**（所有者依赖这个兜底来补救"框不准"）；
  - 2 个及以上   -> 逐条尝试并取每张模板的最高分，**完全不扩容**。

于是"多框一段区域"反而会让原本靠扩容能补救的第一步失效 —— 条数一变，语义就变。
现在两者统一为：

  先逐条尝试所有区域（取最高分）→ 全都没命中才从**第 1 个区域**起逐次扩大到全屏。

1 个区域时与旧行为完全一致（`match_in_expanding_rect` 本身就是"先试该区域再扩大"）。

本文件用替身替换 `main.match_all_templates`，直接观察**调用序列**，
不依赖真实的 OpenCV 模板匹配。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

import main

SCREEN = np.zeros((60, 60, 3), dtype=np.uint8)
R1 = (10, 10, 30, 30)
R2 = (20, 20, 40, 40)
R3 = (30, 30, 50, 50)
FULL_SCREEN = (0, 0, 60, 60)


class FakeMatcher:
    """记录每次调用的 (search_rect, threshold)，并只在指定矩形里"命中"。"""

    def __init__(self, hits=None):
        # hits: {rect: (center, score)}；rect 为 None 表示全屏调用
        self.hits = dict(hits or {})
        self.calls = []
        self.thresholds = []

    def __call__(self, screen_img, templates, *args, **kwargs):
        rect = kwargs.get("search_rect")
        self.calls.append(rect)
        # match_all_templates(screen_img, templates, threshold, use_multi_scale, ...)
        self.thresholds.append(args[0] if args else kwargs.get("threshold"))
        if not templates:
            return {}
        # ⚠️ 必须与真实 match_all_templates 的契约一致：**每张**模板都会有一条记录，
        #    未命中时是 (None, 最高分)，而不是空字典。
        #    此前这里未命中返回 {}，于是"区域没命中 -> 扩大到全屏"的兜底在测试里能跑到、
        #    在生产里跑不到（上层用 `if merged_results:` 当判据），7 个测试全绿却漏掉了
        #    2026-09-28 的识别回归（见 §8.1 #68）。
        hit = None
        for rect_key, payload in self.hits.items():
            if rect_key == rect:
                hit = payload
                break
        return {name: (hit if hit is not None else (None, -1.0)) for name in templates}

    @property
    def rects(self):
        """只保留带区域的调用（便于断言"逐条尝试了哪些区域"）。"""
        return [rect for rect in self.calls if rect is not None]


class RegionMatchSemanticsTests(unittest.TestCase):
    def setUp(self):
        self._original_templates = main.templates
        self._original_match = main.match_all_templates
        main.templates = {"t": np.zeros((4, 4, 3), dtype=np.uint8)}

    def tearDown(self):
        main.templates = self._original_templates
        main.match_all_templates = self._original_match

    def _run(self, rects, hits=None):
        matcher = FakeMatcher(hits)
        main.match_all_templates = matcher
        task = {"type": "normal", "template": "t"}
        if rects is not None:
            task["match_rects"] = [list(rect) for rect in rects]
        result = main.match_task_templates(task, SCREEN, template_names="t")
        return result, matcher

    # ---------- 命中：不该走扩容 ----------

    def test_single_region_hit_returns_without_expanding(self):
        result, matcher = self._run([R1], hits={R1: ((20, 20), 0.9)})
        self.assertTrue(result, "应返回命中结果")
        self.assertEqual(matcher.calls, [R1], "命中时只该试一次该区域，不该进入扩容循环")

    def test_multiple_regions_hit_returns_without_expanding(self):
        result, matcher = self._run([R1, R2, R3], hits={R2: ((30, 30), 0.9)})
        self.assertTrue(result)
        self.assertEqual(matcher.rects, [R1, R2, R3], "应逐条尝试全部区域")
        self.assertNotIn(FULL_SCREEN, matcher.calls, "命中后不该再扩容到全屏")

    def test_multiple_regions_merge_keeps_the_highest_score(self):
        result, _ = self._run([R1, R2], hits={R1: ((1, 1), 0.70), R2: ((2, 2), 0.95)})
        self.assertEqual(result["t"], ((2, 2), 0.95), "合并时应保留置信度更高的那次")

    # ---------- 未命中：1 个与多个都要走同一份扩容兜底 ----------

    def test_single_region_miss_expands_to_the_full_screen(self):
        result, matcher = self._run([R1])
        # 未命中的真实形态：结果里**有**这条模板，但位置是 None（不是空字典）
        self.assertIsNone(result["t"][0], "未命中时该模板不应有位置")
        self.assertEqual(matcher.rects[0], R1, "第一步应是该区域本身")
        self.assertEqual(matcher.calls[-1], FULL_SCREEN, "应逐次扩大直到整张截图")

    def test_multiple_regions_miss_also_expands_from_the_first_region(self):
        result, matcher = self._run([R1, R2, R3])
        self.assertIsNone(result["t"][0], "未命中时该模板不应有位置")
        self.assertEqual(matcher.rects[:3], [R1, R2, R3], "应先把三条都试一遍")
        self.assertEqual(matcher.rects[3], R1, "全都没命中后应从**第 1 个**区域开始扩容")
        self.assertEqual(matcher.calls[-1], FULL_SCREEN, "扩容应一路到整张截图")

    def test_single_and_multiple_regions_share_the_same_fallback_anchor(self):
        """这是本次修复的核心：条数不该改变兜底的起点与序列。

        注意 1 个区域时序列里 R1 会出现两次：第一次是"逐条尝试"那一趟，第二次是
        `match_in_expanding_rect` 自己的区域内首试（它本来就是"先试该区域再扩大"）。
        多花的这一趟只在区域内未命中时发生、区域又很小，换来的是两种条数共用同一条
        兜底；命中时反而比旧版少走一趟扩容循环。
        """
        _, single = self._run([R1])
        _, multiple = self._run([R1, R2, R3])

        single_expansion = single.rects[1:]      # 跳过"逐条尝试"那一趟
        multiple_expansion = multiple.rects[3:]  # 同上：前三个是三条区域各试一次
        self.assertEqual(single_expansion, [R1, (0, 0, 40, 40), FULL_SCREEN])
        self.assertEqual(
            single_expansion, multiple_expansion,
            "只配 R1 与配 R1+R2+R3 时的扩容序列应完全一致",
        )

    def test_expansion_anchor_is_the_first_region_not_the_last(self):
        """锚点必须是第 1 条——框选是追加式的，第 1 条才是用户最初圈定的位置。"""
        _, matcher = self._run([R1, R2])
        expansion = matcher.rects[2:]
        self.assertTrue(expansion, "未命中时应进入扩容")
        self.assertEqual(expansion[0], R1)

    # ---------- 没有区域：全屏（行为不变） ----------

    def test_no_region_falls_back_to_full_screen_match(self):
        result, matcher = self._run(None, hits={None: ((5, 5), 0.8)})
        self.assertTrue(result)
        self.assertEqual(matcher.calls, [None], "没有区域时只做全屏匹配")
        self.assertNotIn(R1, matcher.calls)

    def test_custom_threshold_is_passed_through(self):
        matcher = FakeMatcher(hits={R1: ((20, 20), 0.9)})
        main.match_all_templates = matcher
        task = {"type": "normal", "template": "t", "match_rects": [list(R1)], "threshold": 0.63}
        main.match_task_templates(task, SCREEN, template_names="t")
        self.assertEqual(matcher.calls, [R1])
        self.assertEqual(matcher.thresholds, [0.63], "任务自定义阈值应透传给匹配函数")


class RegionFallbackIntegrationTests(unittest.TestCase):
    """端到端（真实 match_all_templates + 合成画面）验证"区域没命中仍能扩大到全屏"。

    为什么单独一组：上面那组用的是替身，替身与真实实现的契约一旦漂移就会漏过整条路径
    （2026-09-28 的识别回归就是这么漏掉的）。本组不替换任何东西，直接喂真实图像，
    因此能挡住"判据写错导致兜底不可达"这一类问题。
    """

    TEMPLATE_H, TEMPLATE_W = 18, 26

    def setUp(self):
        self._original_templates = main.templates
        rng = np.random.RandomState(20260928)
        self.tpl = rng.randint(0, 255, (self.TEMPLATE_H, self.TEMPLATE_W, 3), dtype=np.uint8)
        self.screen = np.zeros((480, 640, 3), dtype=np.uint8)
        self.true_left, self.true_top = 500, 350
        self.screen[self.true_top:self.true_top + self.TEMPLATE_H,
                    self.true_left:self.true_left + self.TEMPLATE_W] = self.tpl
        main.templates = {"probe_tpl": self.tpl}

    def tearDown(self):
        main.templates = self._original_templates

    def _task(self, rects):
        return {
            "type": "normal", "description": "合成画面", "template": "probe_tpl",
            "templates": ["probe_tpl"],
            "match_rects": [list(rect) for rect in rects],
            "threshold": 0.8, "click": True,
        }

    def test_target_outside_region_is_found_by_expansion(self):
        """区域完全不含目标时，必须靠扩容兜底找到它（本次回归的核心场景）。"""
        result = main.match_task_templates(self._task([(0, 0, 120, 100)]),
                                           self.screen, ["probe_tpl"])
        center, conf = result.get("probe_tpl", (None, -1.0))
        self.assertIsNotNone(center, "目标在区域外时也必须能识别（扩容兜底不可达就是回归）")
        self.assertGreaterEqual(float(conf), 0.8)
        # 命中点是模板中心：左上角 + 半宽/半高
        self.assertAlmostEqual(center[0], self.true_left + self.TEMPLATE_W // 2, delta=2)
        self.assertAlmostEqual(center[1], self.true_top + self.TEMPLATE_H // 2, delta=2)

    def test_target_outside_all_regions_is_found_by_expansion(self):
        """多个区域全部未命中时同样要扩容到全屏。"""
        rects = [(0, 0, 80, 60), (100, 0, 180, 60), (0, 400, 80, 470)]
        result = main.match_task_templates(self._task(rects), self.screen, ["probe_tpl"])
        center, _ = result.get("probe_tpl", (None, -1.0))
        self.assertIsNotNone(center, "多区域全未命中时也必须扩容")

    def test_target_inside_region_is_found_at_full_confidence(self):
        """目标就在区域里时，分数应接近满分，且不需要扩容。"""
        result = main.match_task_templates(self._task([(490, 340, 540, 380)]),
                                           self.screen, ["probe_tpl"])
        center, conf = result.get("probe_tpl", (None, -1.0))
        self.assertIsNotNone(center)
        self.assertGreaterEqual(float(conf), 0.99, "区域内命中应是近乎完美匹配")

    def test_missing_template_name_yields_no_position(self):
        """模板名不存在时不应误报命中（match_task_templates 会返回空字典）。"""
        result = main.match_task_templates(self._task([(0, 0, 120, 100)]),
                                           self.screen, ["不存在的模板"])
        self.assertEqual(result, {}, "模板名解析不到时没有可匹配的对象")


if __name__ == "__main__":
    unittest.main()
