"""模板匹配的边界情况测试。

重点是「模板比截图还大」这一情形：
多尺度分支早就有尺寸保护，单尺度分支此前没有，会直接抛 cv2.error，
使整轮识别崩溃。此测试锁定该行为不再回归。
"""

import unittest

import numpy as np

from core.template_match import match_all_templates


def make_screen(width, height, fill=0):
    return np.full((height, width, 3), fill, dtype=np.uint8)


class TemplateSizeGuardTests(unittest.TestCase):
    def test_oversized_template_does_not_raise_in_single_scale(self):
        screen = make_screen(100, 100)
        big = make_screen(300, 300)
        result = match_all_templates(screen, {"big": big}, threshold=0.8, use_multi_scale=False)
        self.assertEqual(result["big"][0], None, "超大模板应返回未命中")
        self.assertLess(result["big"][1], 0.0)

    def test_oversized_template_does_not_raise_in_multi_scale(self):
        screen = make_screen(100, 100)
        big = make_screen(300, 300)
        result = match_all_templates(screen, {"big": big}, threshold=0.8, use_multi_scale=True)
        self.assertEqual(result["big"][0], None)

    def test_oversized_template_does_not_block_other_templates(self):
        """一个超大模板不应影响同一批次中其它模板的匹配。"""
        screen = make_screen(120, 120)
        screen[40:60, 40:60] = 255
        big = make_screen(300, 300)
        small = np.full((20, 20, 3), 255, dtype=np.uint8)

        result = match_all_templates(
            screen, {"big": big, "small": small}, threshold=0.9, use_multi_scale=False
        )
        self.assertEqual(result["big"][0], None, "超大模板应未命中")
        self.assertIsNotNone(result["small"][0], "正常模板仍应命中")

    def test_normal_match_still_works_single_scale(self):
        screen = make_screen(200, 200)
        screen[50:70, 50:70] = 255
        small = np.full((20, 20, 3), 255, dtype=np.uint8)

        result = match_all_templates(screen, {"small": small}, threshold=0.9, use_multi_scale=False)
        center, confidence = result["small"]
        self.assertIsNotNone(center)
        self.assertGreaterEqual(confidence, 0.9)

    def test_template_exactly_the_screen_size(self):
        """边界：模板与截图同尺寸时不应崩溃。"""
        screen = make_screen(40, 40)
        template = make_screen(40, 40)
        result = match_all_templates(screen, {"same": template}, threshold=0.5, use_multi_scale=False)
        self.assertIn("same", result)


if __name__ == "__main__":
    unittest.main()
