# -*- coding: utf-8 -*-
"""P0 中保留的自动能力：分辨率归一化 + 粗到细搜索（无新增步骤字段）。"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np

import config
from core import template_match as tm

from tests.test_recognition_enhancements import _pattern


class ResolutionNormalizationTests(unittest.TestCase):
    def setUp(self):
        tm.clear_template_cache()
        tm.clear_last_hits()

    def test_template_is_matched_after_resolution_change(self):
        tpl = _pattern(40, seed=21)
        screen1 = np.zeros((240, 320, 3), dtype=np.uint8)
        screen1[60:100, 80:120] = tpl
        first = tm.match_all_templates(screen1, {"t": tpl}, threshold=0.9, use_multi_scale=False)
        self.assertIsNotNone(first["t"][0])
        self.assertEqual(tm._LAST_HIT_SCREEN_HEIGHTS.get("t"), 240)

        # 分辨率翻倍：模板内容也放大 2 倍
        tpl2 = cv2.resize(tpl, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)
        screen2 = np.zeros((480, 640, 3), dtype=np.uint8)
        screen2[120:200, 160:240] = tpl2
        second = tm.match_all_templates(screen2, {"t": tpl}, threshold=0.8, use_multi_scale=False)
        center = second["t"][0]
        self.assertIsNotNone(center, "换分辨率后应能通过归一化匹配")
        self.assertLessEqual(abs(center[0] - 200), 3)
        self.assertLessEqual(abs(center[1] - 160), 3)


class CoarseToFineTests(unittest.TestCase):
    def setUp(self):
        tm.clear_template_cache()
        tm.clear_last_hits()

    def test_coarse_to_fine_on_large_screen(self):
        tpl = _pattern(30, seed=25)
        screen = np.zeros((1000, 1000, 3), dtype=np.uint8)
        screen[400:430, 500:530] = tpl
        with mock.patch.object(config, "COARSE_TO_FINE", True), \
                mock.patch.object(config, "COARSE_MIN_AREA", 100000):
            result = tm.match_all_templates(screen, {"t": tpl}, threshold=0.9,
                                            use_multi_scale=False)
        center = result["t"][0]
        self.assertIsNotNone(center, "粗到细应能找到目标")
        self.assertLessEqual(abs(center[0] - 515), 3)
        self.assertLessEqual(abs(center[1] - 415), 3)


if __name__ == "__main__":
    unittest.main()
