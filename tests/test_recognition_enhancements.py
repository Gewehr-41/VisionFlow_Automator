# -*- coding: utf-8 -*-
"""识别算法增强（2026-10-28）：缓存/上次位置、掩膜/颜色、NMS/多帧、OCR 预处理/模糊、ORB 兜底。"""

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


def _pattern(size=30, seed=0):
    rng = np.random.default_rng(seed)
    img = rng.integers(40, 220, (size, size, 3), dtype=np.uint8)
    img[::4, :, :] = 255
    img[:, ::4, :] = 0
    return img


class TemplateCacheAndPreferredTests(unittest.TestCase):
    def setUp(self):
        tm.clear_template_cache()

    def test_results_are_deterministic_and_cache_is_reused(self):
        tpl = _pattern(24, seed=1)
        screen = np.zeros((120, 160, 3), dtype=np.uint8)
        screen[40:64, 50:74] = tpl
        templates = {"t": tpl}
        first = tm.match_all_templates(screen, templates, threshold=0.9,
                                       use_multi_scale=True,
                                       scale_range=(1.0, 1.0), scale_step=0.1)
        cache_size = len(tm._SCALED_CACHE)
        second = tm.match_all_templates(screen, templates, threshold=0.9,
                                        use_multi_scale=True,
                                        scale_range=(1.0, 1.0), scale_step=0.1)
        self.assertIsNotNone(first["t"][0])
        self.assertEqual(first["t"][0], second["t"][0], "结果应确定性（不再随机）")
        self.assertGreater(cache_size, 0, "缩放模板应被缓存")
        self.assertEqual(cache_size, len(tm._SCALED_CACHE), "第二次不应重复计算缩放模板")

    def test_preferred_search_falls_back_to_full_screen(self):
        tpl = _pattern(20, seed=2)
        screen1 = np.zeros((120, 160, 3), dtype=np.uint8)
        screen1[20:40, 20:40] = tpl
        templates = {"t": tpl}
        first = tm.match_all_templates(screen1, templates, threshold=0.9, use_multi_scale=False)
        self.assertIsNotNone(first["t"][0])
        # 目标移动到很远的位置：优先窗口会失效，必须能兜底全屏找到
        screen2 = np.zeros((120, 160, 3), dtype=np.uint8)
        screen2[80:100, 110:130] = tpl
        second = tm.match_all_templates(screen2, templates, threshold=0.9, use_multi_scale=False)
        self.assertIsNotNone(second["t"][0], "上次位置优先不能导致移动后找不到目标")
        self.assertEqual(second["t"][0], (120, 90))


class MaskAndColorTests(unittest.TestCase):
    def setUp(self):
        tm.clear_template_cache()

    def test_color_check_rejects_same_shape_different_color(self):
        tpl = _pattern(24, seed=3)
        red = tpl.copy()
        red[:, :, 0] = 255
        red[:, :, 1] = 0
        red[:, :, 2] = 0
        screen = np.zeros((120, 160, 3), dtype=np.uint8)
        blue = tpl.copy()
        blue[:, :, 0] = 0
        blue[:, :, 1] = 0
        blue[:, :, 2] = 255
        screen[30:54, 40:64] = blue
        with mock.patch.object(config, "COLOR_CHECK_ENABLED", True), \
                mock.patch.object(config, "COLOR_CHECK_TOLERANCE", 20.0):
            result = tm.match_all_templates(screen, {"t": red}, threshold=0.5, use_multi_scale=False)
        self.assertIsNone(result["t"][0], "颜色差过大的命中应被颜色校验拒绝")

    def test_alpha_mask_ignores_transparent_background(self):
        size = 80
        screen = np.random.default_rng(4).integers(0, 255, (200, 200, 3), dtype=np.uint8)
        cv2.circle(screen, (120, 80), 14, (250, 250, 250), -1)

        bgra = np.zeros((size, size, 4), dtype=np.uint8)
        cv2.circle(bgra, (40, 40), 14, (255, 255, 255, 255), -1)
        with mock.patch.object(config, "USE_TEMPLATE_MASK", True):
            result = tm.match_all_templates(screen, {"round": bgra}, threshold=0.8, use_multi_scale=False)
        center = result["round"][0]
        self.assertIsNotNone(center, "带透明背景的模板应能用 alpha 掩膜命中")
        self.assertLessEqual(abs(center[0] - 120), 3)
        self.assertLessEqual(abs(center[1] - 80), 3)


class FeatureFallbackTests(unittest.TestCase):
    def setUp(self):
        tm.clear_template_cache()

    def test_orb_fallback_finds_out_of_range_scale(self):
        # 真实 UI 模板通常 60~100 px；30 px 以下特征点太少，不属于兜底能覆盖的范围
        tpl = _pattern(64, seed=7)
        screen = np.zeros((320, 360, 3), dtype=np.uint8)
        scaled = cv2.resize(tpl, None, fx=1.4, fy=1.4)
        screen[60:60 + scaled.shape[0], 120:120 + scaled.shape[1]] = scaled
        with mock.patch.object(config, "FEATURE_FALLBACK", True):
            result = tm.match_all_templates(screen, {"t": tpl}, threshold=0.95,
                                            use_multi_scale=True,
                                            scale_range=(0.8, 1.2), scale_step=0.1,
                                            allow_feature_fallback=True)
        center = result["t"][0]
        self.assertIsNotNone(center, "超出缩放范围时应由 ORB 特征匹配兜底")
        expected = (120 + scaled.shape[1] // 2, 60 + scaled.shape[0] // 2)
        self.assertLessEqual(abs(center[0] - expected[0]), 6)
        self.assertLessEqual(abs(center[1] - expected[1]), 6)

    def test_feature_fallback_is_opt_in(self):
        tpl = _pattern(30, seed=8)
        screen = np.zeros((120, 160, 3), dtype=np.uint8)
        result = tm.match_all_templates(screen, {"t": tpl}, threshold=0.95,
                                        use_multi_scale=False, allow_feature_fallback=False)
        self.assertIsNone(result["t"][0])


class OcrPreprocessAndFuzzyTests(unittest.TestCase):
    def test_ocr_preprocess_upscales_small_crop(self):
        seen = {}

        class _Engine:
            def __call__(self, image, use_cls=False):
                seen["shape"] = image.shape
                return [], 0.0

        with mock.patch.object(ocr, "available", lambda: True), \
                mock.patch.object(ocr, "_get_engine", lambda: _Engine()), \
                mock.patch.object(config, "OCR_PREPROCESS", True):
            ocr.recognize(np.zeros((60, 200, 3), dtype=np.uint8))
        self.assertEqual(seen["shape"][:2], (120, 400), "小区域应放大 2 倍再识别")

    def test_fuzzy_match_catches_minor_ocr_error(self):
        self.assertTrue(main._ocr_fuzzy_matches("开始战斗", "开始战", ratio=0.8))
        self.assertFalse(main._ocr_fuzzy_matches("完全不同", "开始战斗", ratio=0.86))
        self.assertFalse(main._ocr_fuzzy_matches("abc", "a.c", mode="regex"), "正则不做模糊")

    def test_recognize_text_step_uses_fuzzy_fallback(self):
        blocks = [ocr.TextBlock("开始战斗", 0.9, (10, 10))]
        task = {"ocr_text": "开始战", "ocr_fuzzy": True, "ocr_min_confidence": 0.0}
        with mock.patch.object(main.ocr, "recognize", lambda image, rect=None: blocks):
            text, center, _score = main._recognize_text_step(task, object())
        self.assertEqual(text, "开始战斗")
        self.assertEqual(center, (10, 10))


class ConfirmFramesTests(unittest.TestCase):
    def test_confirm_frames_requires_consecutive_hits(self):
        clicks = []

        def fake_capture():
            return object()

        with mock.patch.object(main, "capture_screen", fake_capture), \
                mock.patch.object(main, "match_task_templates",
                                  lambda *a, **k: {"t": ((50, 50), 0.95)}), \
                mock.patch.object(main, "click_global_position",
                                  lambda *a, **k: clicks.append(a)), \
                mock.patch.object(main, "interruptible_sleep", lambda *a: False), \
                mock.patch.object(main, "wait_for_step_result", lambda *a, **k: True):
            task = {"type": "normal", "template": "t", "description": "多帧确认",
                    "threshold": 0.8, "timeout": 5, "after_wait": 0,
                    "confirm_frames": 2, "click": True}
            self.assertTrue(main.execute_task(task, log_callback=lambda *_: None))
        self.assertEqual(len(clicks), 1, "确认通过后只点击一次")


if __name__ == "__main__":
    unittest.main()
