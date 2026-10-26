# -*- coding: utf-8 -*-
"""F12 文字识别（OCR）：文字匹配、后端、引擎流程、体检规则、编辑面板。

OCR 是**识别方式**而不是新步骤类型：勾选后同一套循环（超时/重试/迂回/兜底/调试面板/
蓝图连线）照旧生效，这里逐层固定行为。
"""

import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
import gui_pyside6 as g  # noqa: E402
import main  # noqa: E402
import ocr  # noqa: E402
import preflight  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class TextMatchTests(unittest.TestCase):
    def test_contains(self):
        self.assertTrue(main._ocr_text_matches("开始战斗", "开始", "contains"))
        self.assertFalse(main._ocr_text_matches("开始战斗", "结束", "contains"))

    def test_multiple_candidates(self):
        self.assertTrue(main._ocr_text_matches("确认", "开始|确认", "contains"))
        self.assertTrue(main._ocr_text_matches("确认", "开始|确认", "exact"))

    def test_exact(self):
        self.assertTrue(main._ocr_text_matches("确认", "确认", "exact"))
        self.assertFalse(main._ocr_text_matches("确认按钮", "确认", "exact"))
        self.assertTrue(main._ocr_text_matches("  确认  ", "确认", "exact"), "首尾空白应忽略")

    def test_regex(self):
        self.assertTrue(main._ocr_text_matches("等级 Lv.42", r"Lv\.\d+", "regex"))
        self.assertFalse(main._ocr_text_matches("等级 42", r"Lv\.\d+", "regex"))
        self.assertFalse(main._ocr_text_matches("abc", "([", "regex"), "坏正则不能抛异常")

    def test_case_sensitivity(self):
        self.assertTrue(main._ocr_text_matches("START", "start", "contains", False))
        self.assertFalse(main._ocr_text_matches("START", "start", "contains", True))

    def test_empty_inputs(self):
        self.assertFalse(main._ocr_text_matches("", "确认"))
        self.assertFalse(main._ocr_text_matches("确认", ""))


class BackendTests(unittest.TestCase):
    def test_backend_is_available_in_this_environment(self):
        self.assertTrue(ocr.available(), "本机应已安装 rapidocr-onnxruntime")
        self.assertEqual(ocr.backend_name(), "rapidocr")

    def test_missing_backend_reports_install_command(self):
        with mock.patch.object(ocr, "available", lambda: False):
            self.assertIn("pip install", ocr.install_hint())
            with self.assertRaises(RuntimeError) as context:
                ocr.recognize(object())
            self.assertIn("OCR", str(context.exception))

    def test_recognize_real_image(self):
        """真实 OCR：渲染中文文字让后端识别，确认整条链路可用。"""
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont

        font_path = r"C:\Windows\Fonts\msyh.ttc"
        if not os.path.isfile(font_path):
            self.skipTest("缺少中文字体，跳过真实 OCR 用例")
        font = ImageFont.truetype(font_path, 30)
        image = Image.new("RGB", (360, 80), "white")
        ImageDraw.Draw(image).text((16, 20), "开始战斗", font=font, fill="black")
        bgr = np.array(image)[:, :, ::-1].copy()

        blocks = ocr.recognize(bgr)
        self.assertTrue(blocks, "应识别出至少一块文字")
        self.assertTrue(any("开始战斗" in block.text for block in blocks),
                        f"识别结果不符：{[b.text for b in blocks]}")
        self.assertGreater(blocks[0].score, config.OCR_MIN_CONFIDENCE)

    def test_recognize_respects_region(self):
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont

        font_path = r"C:\Windows\Fonts\msyh.ttc"
        if not os.path.isfile(font_path):
            self.skipTest("缺少中文字体")
        font = ImageFont.truetype(font_path, 30)
        image = Image.new("RGB", (520, 90), "white")
        draw = ImageDraw.Draw(image)
        draw.text((16, 22), "开始战斗", font=font, fill="black")
        draw.text((280, 22), "确认", font=font, fill="black")
        bgr = np.array(image)[:, :, ::-1].copy()

        blocks = ocr.recognize(bgr, (240, 0, 520, 90))
        self.assertTrue(any("确认" in block.text for block in blocks))
        self.assertFalse(any("开始战斗" in block.text for block in blocks),
                         "区域外的文字不该被识别到")


class RecognizeStepTests(unittest.TestCase):
    def _blocks(self, *items):
        return [ocr.TextBlock(text, score, center) for text, score, center in items]

    def test_picks_highest_confidence_match(self):
        blocks = self._blocks(("开始战斗", 0.70, (10, 10)), ("开始", 0.95, (20, 20)), ("取消", 0.99, (30, 30)))
        with mock.patch.object(main.ocr, "recognize", lambda image, rect=None: blocks):
            name, center, conf = main._recognize_text_step({"ocr_text": "开始"}, object())
        self.assertEqual(center, (20, 20))
        self.assertAlmostEqual(conf, 0.95)

    def test_min_confidence_filters_blocks(self):
        blocks = self._blocks(("开始战斗", 0.40, (10, 10)))
        task = {"ocr_text": "开始", "ocr_min_confidence": 0.8}
        with mock.patch.object(main.ocr, "recognize", lambda image, rect=None: blocks):
            self.assertEqual(main._recognize_text_step(task, object()), (None, None, 0.0))

    def test_region_is_passed_through(self):
        seen = []

        def fake_recognize(image, rect=None):
            seen.append(rect)
            return []

        task = {"ocr_text": "开始", "match_rects": [[10, 10, 50, 50], [60, 60, 90, 90]]}
        with mock.patch.object(main.ocr, "recognize", fake_recognize):
            main._recognize_text_step(task, object())
        self.assertEqual(seen, [(10, 10, 50, 50), (60, 60, 90, 90)],
                         "有多个识别区域时应逐条尝试")

    def test_regex_and_case_flags_reach_matching(self):
        blocks = self._blocks(("START BATTLE", 0.9, (5, 5)))
        task = {"ocr_text": "start", "ocr_match": "contains", "ocr_case_sensitive": False}
        with mock.patch.object(main.ocr, "recognize", lambda image, rect=None: blocks):
            self.assertIsNotNone(main._recognize_text_step(task, object())[1])
        task["ocr_case_sensitive"] = True
        with mock.patch.object(main.ocr, "recognize", lambda image, rect=None: blocks):
            self.assertIsNone(main._recognize_text_step(task, object())[1])


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clicked = []
        self._patches = [
            mock.patch.object(main, "capture_screen", lambda *a, **k: object()),
            mock.patch.object(main, "click_global_position",
                              lambda x, y, offset=(0, 0): self.clicked.append((x, y))),
            mock.patch.object(main, "wait_for_step_result", lambda *a, **k: True),
            mock.patch.object(main, "interruptible_sleep", lambda seconds, flag: False),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()

    def test_ocr_step_clicks_the_recognized_text(self):
        blocks = [ocr.TextBlock("开始战斗", 0.98, (120, 60))]
        task = {"type": "normal", "ocr_enabled": True, "ocr_text": "开始战斗",
                "click": True, "timeout": 0}
        with mock.patch.object(main.ocr, "recognize", lambda image, rect=None: blocks):
            result = main.execute_task(task, log_callback=lambda message: None)
        self.assertTrue(result)
        self.assertEqual(len(self.clicked), 1)
        # 点击坐标会经过 to_global_position 换算（窗口模式会加窗口偏移），所以和引擎口径对齐来断言
        expected_x, expected_y = main.to_global_position(120, 60)
        self.assertEqual(self.clicked[0][0], expected_x)
        self.assertEqual(self.clicked[0][1], expected_y, "应点击识别到的文字中心")

    def test_ocr_step_without_text_raises(self):
        task = {"type": "normal", "ocr_enabled": True, "ocr_text": ""}
        with self.assertRaises(ValueError):
            main.execute_task(task, log_callback=lambda message: None)

    def test_ocr_step_without_backend_raises_with_hint(self):
        task = {"type": "normal", "ocr_enabled": True, "ocr_text": "开始"}
        with mock.patch.object(main.ocr, "available", lambda: False):
            with self.assertRaises(RuntimeError) as context:
                main.execute_task(task, log_callback=lambda message: None)
        self.assertIn("pip install", str(context.exception))

    def test_ocr_step_times_out_like_template_steps(self):
        task = {"type": "normal", "ocr_enabled": True, "ocr_text": "不存在", "timeout": 0.05}
        with mock.patch.object(main.ocr, "recognize", lambda image, rect=None: []):
            with self.assertRaises(RuntimeError):
                main.execute_task(task, log_callback=lambda message: None)


class PreflightOcrTests(unittest.TestCase):
    def _check(self, task):
        return preflight.check_tasks([task], icon_dir=tempfile.gettempdir(), screen_size=(1920, 1080))

    def _codes(self, issues):
        return [issue.code for issue in issues]

    def test_missing_text_is_fatal(self):
        issues = self._check({"id": "a", "type": "normal", "ocr_enabled": True, "ocr_text": ""})
        self.assertIn("ocr_text_missing", self._codes(issues))
        self.assertEqual([i.level for i in issues if i.code == "ocr_text_missing"], [preflight.FATAL])

    def test_ocr_step_does_not_require_template(self):
        issues = self._check({"id": "a", "type": "normal", "ocr_enabled": True, "ocr_text": "开始"})
        self.assertNotIn("template_unset", self._codes(issues))

    def test_missing_backend_is_fatal(self):
        with mock.patch.object(preflight.ocr, "available", lambda: False):
            issues = self._check({"id": "a", "type": "normal", "ocr_enabled": True, "ocr_text": "开始"})
        self.assertIn("ocr_backend_missing", self._codes(issues))

    def test_invalid_mode_and_confidence_are_warnings(self):
        issues = self._check({"id": "a", "type": "normal", "ocr_enabled": True, "ocr_text": "开始",
                              "ocr_match": "wrong", "ocr_min_confidence": 3})
        self.assertIn("ocr_match_invalid", self._codes(issues))
        self.assertIn("ocr_confidence_invalid", self._codes(issues))

    def test_plain_steps_unaffected(self):
        issues = self._check({"id": "a", "type": "delay", "duration": 1})
        self.assertEqual(issues, [])


class EditorPanelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = []
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def test_options_are_in_a_titled_group_box(self):
        """所有者 2026-10-26 反馈：勾选后出现的 OCR 设置没有分界线，要和「识别区域」一样的小框。"""
        from PySide6.QtWidgets import QGroupBox

        self.assertIsInstance(self.window.ocr_options, QGroupBox,
                              "OCR 选项应放在带标题的 QGroupBox 里（才有边框分界）")
        self.assertEqual(self.window.ocr_options.title(), "文字识别（OCR）")

    def test_options_visibility_follows_checkbox(self):
        # 先切到识别型步骤（否则 OCR 选项所在的识别组整体隐藏）
        self.window._update_editor_visibility({"type": "normal", "template": "a"})
        self.window._load_common_editor_fields({"type": "normal", "template": "a"})
        self.assertFalse(self.window.ocr_options.isVisibleTo(self.window))
        self.window.ocr_checkbox.setChecked(True)
        self.assertTrue(self.window.ocr_options.isVisibleTo(self.window))

    def test_roundtrip(self):
        task = {"type": "normal", "template": "a", "ocr_enabled": True, "ocr_text": "开始|确认",
                "ocr_match": "exact", "ocr_case_sensitive": True, "ocr_min_confidence": 0.75}
        self.window._load_common_editor_fields(task)
        self.assertTrue(self.window.ocr_checkbox.isChecked())
        self.assertEqual(self.window.ocr_text_edit.text(), "开始|确认")
        self.assertEqual(self.window.ocr_match_combo.currentData(), "exact")
        self.assertTrue(self.window.ocr_case_checkbox.isChecked())
        self.assertEqual(self.window.ocr_confidence_edit.text(), "0.75")

        target = {"type": "normal", "template": "a"}
        self.window._apply_common_editor_fields(target)
        self.assertTrue(target["ocr_enabled"])
        self.assertEqual(target["ocr_text"], "开始|确认")
        self.assertEqual(target["ocr_match"], "exact")
        self.assertTrue(target["ocr_case_sensitive"])
        self.assertAlmostEqual(target["ocr_min_confidence"], 0.75)

    def test_confidence_is_clamped(self):
        self.window.ocr_confidence_edit.setText("9")
        target = {"type": "normal"}
        self.window._apply_common_editor_fields(target)
        self.assertEqual(target["ocr_min_confidence"], 1.0)

    def test_clear_unchecks_ocr(self):
        self.window._load_common_editor_fields({"type": "normal", "ocr_enabled": True, "ocr_text": "开始"})
        self.window.clear_editor()
        self.assertFalse(self.window.ocr_checkbox.isChecked())
        self.assertEqual(self.window.ocr_text_edit.text(), "")
        self.assertFalse(self.window.ocr_options.isVisibleTo(self.window))

    def test_task_list_shows_text_recognition_marker(self):
        g.TASKS[:] = [{"id": "a", "type": "normal", "enabled": True, "template": "",
                       "description": "点开始", "ocr_enabled": True, "ocr_text": "开始"}]
        self.window.refresh_task_list()
        labels = []
        for index in range(self.window.task_list.topLevelItemCount()):
            item = self.window.task_list.topLevelItem(index)
            labels.append(item.text(0))
            for child_index in range(item.childCount()):
                labels.append(item.child(child_index).text(0))
        self.assertTrue(any("文字识别" in label for label in labels), labels)


if __name__ == "__main__":
    unittest.main()
