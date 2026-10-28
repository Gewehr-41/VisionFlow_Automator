# -*- coding: utf-8 -*-
"""OCR 速度优化：大图缩放、单行 ROI 走 text_rec、同画面结果缓存。"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

import config
import ocr


class _FakeEngine:
    def __init__(self, text="商城"):
        self.seen = []
        self.text_rec_calls = []
        self.text = text

    def __call__(self, image, use_cls=False):
        self.seen.append(image.shape)
        return [], [0.0]

    def text_rec(self, images):
        self.text_rec_calls.append(images[0].shape)
        return [(self.text, 0.99)], [0.1]


class OcrSpeedTests(unittest.TestCase):
    def setUp(self):
        ocr._LAST_RESULT_KEY = None
        ocr._LAST_RESULT_BLOCKS = None
        self.engine = _FakeEngine()
        self._patches = [
            mock.patch.object(ocr, "available", lambda: True),
            mock.patch.object(ocr, "_get_engine", lambda: self.engine),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()

    def test_large_image_is_downscaled_before_detection(self):
        image = np.zeros((1080, 1920, 3), dtype=np.uint8)
        with mock.patch.object(config, "OCR_CACHE", False):
            ocr.recognize(image)
        self.assertTrue(self.engine.seen)
        self.assertEqual(self.engine.seen[0][1], config.OCR_MAX_WIDTH)

    def test_single_line_uses_text_rec_and_skips_detection(self):
        image = np.zeros((60, 220, 3), dtype=np.uint8)
        blocks = ocr.recognize(image)
        self.assertTrue(self.engine.text_rec_calls, "单行 ROI 应走 text_rec")
        self.assertFalse(self.engine.seen, "单行 ROI 不应跑完整检测")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, "商城")
        self.assertEqual(blocks[0].center, (110, 30))

    def test_same_screen_and_rect_reuse_cached_result(self):
        image = np.zeros((1080, 1920, 3), dtype=np.uint8)
        with mock.patch.object(config, "OCR_CACHE", True):
            first = ocr.recognize(image)
            second = ocr.recognize(image)
        self.assertEqual(len(self.engine.seen), 1, "同一画面第二次 OCR 应直接复用缓存")
        self.assertEqual(first, second)

    def test_cache_is_invalidated_when_image_changes(self):
        image1 = np.zeros((1080, 1920, 3), dtype=np.uint8)
        image2 = image1.copy()
        image2[500:520, 800:820] = 255
        with mock.patch.object(config, "OCR_CACHE", True):
            ocr.recognize(image1)
            ocr.recognize(image2)
        self.assertEqual(len(self.engine.seen), 2, "画面变了必须重新 OCR")


if __name__ == "__main__":
    unittest.main()
