# -*- coding: utf-8 -*-
"""上次命中点：跨启动持久化、换图后失效、优先于框选区域。"""

import json
import os
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

import config
from core import template_match as tm

from tests.test_recognition_enhancements import _pattern


class PersistedLastHitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hits_path = os.path.join(self._tmp.name, "saved_last_hits.json")
        tm.clear_template_cache()
        tm.set_last_hits_path(self.hits_path)
        tm.clear_last_hits()

    def tearDown(self):
        tm.set_last_hits_path(None)
        tm.clear_last_hits()
        tm.clear_template_cache()
        self._tmp.cleanup()

    def _match(self, screen, tpl, **kwargs):
        return tm.match_all_templates(screen, {"t": tpl}, threshold=0.9,
                                      use_multi_scale=False, **kwargs)

    def test_hit_is_saved_and_restored_after_restart(self):
        tpl = _pattern(24, seed=11)
        screen = np.zeros((120, 160, 3), dtype=np.uint8)
        screen[20:44, 30:54] = tpl
        self.assertIsNotNone(self._match(screen, tpl)["t"][0])
        self.assertTrue(os.path.isfile(self.hits_path), "命中后应写入持久化文件")
        data = json.loads(open(self.hits_path, encoding="utf-8").read())
        self.assertIn("t", data["hits"])
        self.assertEqual(data["hits"]["t"]["center"], [42, 32])
        self.assertEqual(data["hits"]["t"]["signature"], tm._template_signature(tpl))

        # 模拟重启：内存清空后从文件恢复
        tm.clear_last_hits()
        self.assertEqual(tm.load_last_hits(), 1)
        self.assertEqual(tm._LAST_HITS["t"], (42, 32))
        self.assertEqual(tm._get_last_hit("t", tpl), (42, 32))

    def test_replacing_template_image_clears_hit(self):
        tpl = _pattern(24, seed=12)
        screen = np.zeros((120, 160, 3), dtype=np.uint8)
        screen[20:44, 30:54] = tpl
        self._match(screen, tpl)
        self.assertIn("t", tm._LAST_HITS)

        replaced = tpl.copy()
        replaced[0, 0] = (255, 0, 255)
        removed = tm.prune_invalid_last_hits({"t": replaced})
        self.assertEqual(removed, 1, "模板图片内容变化后应清除上次命中点")
        self.assertNotIn("t", tm._LAST_HITS)
        data = json.loads(open(self.hits_path, encoding="utf-8").read())
        self.assertEqual(data["hits"], {})

    def test_deleted_template_clears_hit(self):
        tpl = _pattern(24, seed=13)
        screen = np.zeros((120, 160, 3), dtype=np.uint8)
        screen[20:44, 30:54] = tpl
        self._match(screen, tpl)
        self.assertEqual(tm.prune_invalid_last_hits({}), 1)
        self.assertNotIn("t", tm._LAST_HITS)

    def test_preferred_hit_wins_even_outside_manual_rect(self):
        tpl = _pattern(24, seed=14)
        box = (0, 0, 150, 300)
        screen1 = np.zeros((320, 320, 3), dtype=np.uint8)
        screen1[40:64, 40:64] = tpl
        first = self._match(screen1, tpl, search_rect=box)
        self.assertEqual(first["t"][0], (52, 52))

        # 目标移出框选区域，但仍在「上次命中点」附近 -> 应优先命中
        screen2 = np.zeros((320, 320, 3), dtype=np.uint8)
        screen2[60:84, 260:284] = tpl
        second = self._match(screen2, tpl, search_rect=box)
        self.assertIsNotNone(second["t"][0], "有上次命中点时应优先于框选区域")
        self.assertEqual(second["t"][0], (272, 72))

    def test_falls_back_to_manual_rect_when_preferred_misses(self):
        tpl = _pattern(24, seed=15)
        box = (0, 0, 200, 200)
        screen1 = np.zeros((240, 240, 3), dtype=np.uint8)
        screen1[20:44, 20:44] = tpl
        self._match(screen1, tpl, search_rect=box)

        # 缩小优先搜索窗口，让目标落在窗口外、但在框选区域内
        screen2 = np.zeros((240, 240, 3), dtype=np.uint8)
        screen2[100:124, 100:124] = tpl
        with mock.patch.object(config, "PREFERRED_SEARCH_RADIUS", 20):
            second = self._match(screen2, tpl, search_rect=box)
        self.assertIsNotNone(second["t"][0], "命中点附近没找到时应退回框选区域")
        self.assertEqual(second["t"][0], (112, 112))

    def test_no_hit_recorded_when_nothing_found(self):
        tpl = _pattern(24, seed=16)
        screen = np.zeros((120, 160, 3), dtype=np.uint8)
        result = self._match(screen, tpl)
        self.assertIsNone(result["t"][0])
        self.assertFalse(os.path.exists(self.hits_path), "未命中不应写持久化文件")


if __name__ == "__main__":
    unittest.main()
