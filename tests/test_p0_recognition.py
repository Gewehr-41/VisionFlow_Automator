# -*- coding: utf-8 -*-
"""P0 识别能力：分辨率归一化、green_mask、method、order_by/index、屏幕稳定、OCR only_rec/replace。"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np

import config
import main
import ocr
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


class GreenMaskAndOrderingTests(unittest.TestCase):
    def setUp(self):
        tm.clear_template_cache()
        tm.clear_last_hits()

    def test_green_mask_ignores_green_background(self):
        tpl = np.zeros((40, 40, 3), dtype=np.uint8)
        tpl[:, :] = (0, 255, 0)                      # 纯绿背景：BGR
        cv2.rectangle(tpl, (10, 10), (30, 30), (255, 255, 255), -1)
        screen = np.random.default_rng(22).integers(0, 255, (160, 220, 3), dtype=np.uint8)
        screen[50:90, 70:110] = tpl
        result = tm.match_all_templates(screen, {"t": tpl}, threshold=0.6,
                                        use_multi_scale=False, green_mask=True)
        self.assertIsNotNone(result["t"][0], "green_mask 应忽略纯绿背景")
        self.assertLessEqual(abs(result["t"][0][0] - 90), 3)

    def test_order_by_and_index_pick_the_nth_identical_target(self):
        tpl = _pattern(24, seed=23)
        screen = np.zeros((120, 360, 3), dtype=np.uint8)
        for index, x in enumerate((20, 130, 240)):
            screen[40:64, x:x + 24] = tpl
        result = tm.match_all_templates(screen, {"t": tpl}, threshold=0.9,
                                        use_multi_scale=False,
                                        order_by="horizontal", match_index=1)
        center = result["t"][0]
        self.assertIsNotNone(center)
        self.assertLessEqual(abs(center[0] - (130 + 12)), 3, "index=1 应取第二个目标")

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

    def test_method_can_be_selected(self):
        tpl = _pattern(24, seed=24)
        screen = np.zeros((120, 160, 3), dtype=np.uint8)
        screen[30:54, 40:64] = tpl
        for method in ("ccoeff", "ccorr", "sqdiff"):
            result = tm.match_all_templates(screen, {"t": tpl}, threshold=0.5,
                                            use_multi_scale=False, method=method)
            self.assertIsNotNone(result["t"][0], f"{method} 应能找到完全一致的目标")


class P0WiringTests(unittest.TestCase):
    def test_match_task_templates_forwards_p0_fields(self):
        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        task = {
            "template": "t",
            "match_rect": (0, 0, 50, 50),
            "threshold": 0.8,
            "green_mask": True,
            "match_method": "sqdiff",
            "order_by": "horizontal",
            "match_index": 1,
        }
        with mock.patch.object(main, "templates", {"t": _pattern(24, seed=26)}), \
                mock.patch.object(main, "match_all_templates", return_value={"t": ((5, 5), 0.9)}) as patched:
            main.match_task_templates(task, screen)
        kwargs = patched.call_args.kwargs
        self.assertTrue(kwargs["green_mask"])
        self.assertEqual(kwargs["method"], "sqdiff")
        self.assertEqual(kwargs["order_by"], "horizontal")
        self.assertEqual(kwargs["match_index"], 1)


class WaitFreezesTests(unittest.TestCase):
    def test_disabled_returns_immediately(self):
        self.assertTrue(main.wait_screen_stable(0))

    def test_waits_until_screen_is_stable(self):
        frame = np.zeros((60, 80, 3), dtype=np.uint8)
        with mock.patch.object(main, "capture_screen", lambda: frame), \
                mock.patch.object(main, "interruptible_sleep", lambda *a: False):
            self.assertTrue(main.wait_screen_stable(40, timeout_ms=500, threshold=2.0))


class OcrP0Tests(unittest.TestCase):
    def test_parse_and_apply_replace_rules(self):
        rules = main._parse_ocr_replace("开女台=开始|战斗=战")
        self.assertEqual(main._apply_ocr_replace("开女台战斗", rules), "开始战")

    def test_recognize_text_step_uses_replace(self):
        blocks = [ocr.TextBlock("开女台战斗", 0.9, (10, 10))]
        task = {"ocr_text": "开始战斗", "ocr_replace": "开女台=开始"}
        with mock.patch.object(main.ocr, "recognize", lambda image, rect=None: blocks):
            text, center, _score = main._recognize_text_step(task, object())
        self.assertEqual(text, "开始战斗")
        self.assertEqual(center, (10, 10))

    def test_recognize_text_step_passes_only_rec(self):
        blocks = [ocr.TextBlock("确认", 0.9, (5, 5))]
        seen = {}

        def fake_recognize(image, rect=None, only_rec=False):
            seen["only_rec"] = only_rec
            return blocks

        task = {"ocr_text": "确认", "ocr_only_rec": True}
        with mock.patch.object(main.ocr, "recognize", fake_recognize):
            self.assertIsNotNone(main._recognize_text_step(task, object())[1])
        self.assertTrue(seen["only_rec"])

    def test_ocr_only_rec_uses_text_rec_engine(self):
        class Engine:
            def text_rec(self, images):
                return [("开始战斗", 0.99)], 0.2

        with mock.patch.object(ocr, "available", lambda: True), \
                mock.patch.object(ocr, "_get_engine", lambda: Engine()):
            blocks = ocr.recognize(np.zeros((40, 160, 3), dtype=np.uint8),
                                   (10, 20, 170, 60), only_rec=True)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, "开始战斗")
        self.assertEqual(blocks[0].center, (85, 30))  # rect 被图像边界裁剪后的中心


if __name__ == "__main__":
    unittest.main()
