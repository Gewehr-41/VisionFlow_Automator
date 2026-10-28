# -*- coding: utf-8 -*-
"""识别坐标 → 屏幕坐标：窗口模式加窗口偏移，前景/全屏模式保持全局坐标。"""

import unittest
from unittest import mock

import config
from core import screen


class GlobalPositionTests(unittest.TestCase):
    def test_foreground_mode_returns_global_coordinates(self):
        """自动最小化后前台窗口会变，不能把新前台窗口的偏移加到坐标上。"""
        with mock.patch.object(config, "USE_WINDOW_MODE", False), \
                mock.patch.object(screen, "get_window_rect",
                                  return_value={"left": 300, "top": 200, "width": 800, "height": 600}):
            self.assertEqual(screen.to_global_position(120, 60), (120, 60))

    def test_window_mode_adds_window_offset(self):
        with mock.patch.object(config, "USE_WINDOW_MODE", True), \
                mock.patch.object(config, "TARGET_WINDOW_TITLE", "Game"), \
                mock.patch.object(screen, "get_window_rect",
                                  return_value={"left": 300, "top": 200, "width": 800, "height": 600}):
            self.assertEqual(screen.to_global_position(120, 60), (420, 260))

    def test_missing_rect_falls_back_to_input(self):
        with mock.patch.object(config, "USE_WINDOW_MODE", True), \
                mock.patch.object(screen, "get_window_rect", return_value=None):
            self.assertEqual(screen.to_global_position(120, 60), (120, 60))


if __name__ == "__main__":
    unittest.main()
