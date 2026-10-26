# -*- coding: utf-8 -*-
"""Qt 官方中文翻译的加载（F：标准对话框文字本地化）。

所有者 2026-10-26 反馈："执行前体检弹窗的展开与收起详细选项是英文的，要中文。"
根因：那个按钮是 QMessageBox 自动补的，文字来自 Qt 翻译文件（qtbase_zh_CN.qm），
程序没加载翻译器 → 永远英文。方案：启动时加载 Qt 官方翻译，不自己画对话框。
"""

import os
import pathlib
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QMessageBox,
)

import diagnostics  # noqa: E402
import qt_translations  # noqa: E402

_APP = QApplication.instance() or QApplication([])
_ROOT = pathlib.Path(__file__).resolve().parent.parent


class TranslationLoadingTests(unittest.TestCase):
    def tearDown(self):
        # 卸载本用例装的翻译器，避免影响其它测试模块里的标准控件文字断言
        for translator in list(qt_translations._TRANSLATORS):
            _APP.removeTranslator(translator)
        qt_translations._TRANSLATORS.clear()

    def test_translation_dirs_contain_the_pyside6_translations_folder(self):
        dirs = qt_translations.translation_dirs()
        self.assertTrue(dirs)
        existing = [directory for directory in dirs if os.path.isdir(directory)]
        self.assertTrue(existing, f"至少应有一个存在的翻译目录：{dirs}")

    def test_translations_load_in_this_environment(self):
        self.assertGreater(qt_translations.install_qt_translations(_APP), 0,
                           "这台机器的 PySide6 应自带 qtbase_zh_CN.qm")

    def test_install_without_app_is_a_noop(self):
        self.assertEqual(qt_translations.install_qt_translations(None), 0)

    def test_details_button_becomes_chinese(self):
        """核心回归：setDetailedText 补出来的按钮文字必须是中文。"""
        qt_translations.install_qt_translations(_APP)
        box = QMessageBox()
        box.setText("问题摘要")
        box.setDetailedText("问题详情")
        texts = [button.text() for button in box.findChildren(QAbstractButton) if button.text()]
        self.assertTrue(texts)
        self.assertTrue(any(not text.isascii() for text in texts),
                        f"折叠按钮应为中文，实际：{texts}")
        box.close()

    def test_standard_buttons_become_chinese(self):
        qt_translations.install_qt_translations(_APP)
        box = QMessageBox()
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        texts = [button.text() for button in box.findChildren(QAbstractButton) if button.text()]
        self.assertTrue(all(not text.isascii() for text in texts),
                        f"确定/取消应为中文，实际：{texts}")
        box.close()

    def test_missing_translations_fall_back_with_a_warning(self):
        diagnostics.clear()
        with mock.patch.object(qt_translations, "translation_dirs",
                                        lambda: [os.path.join(str(_ROOT), "不存在的目录")]):
            self.assertEqual(qt_translations.install_qt_translations(_APP), 0)
        warnings = "\n".join(diagnostics.snapshot())
        self.assertIn("Qt 中文翻译", warnings, "找不到翻译文件时应留一条可见警告")
        diagnostics.clear()


class StartupWiringTests(unittest.TestCase):
    """静态守卫：入口要真的装翻译，打包规格要真的带上翻译文件。"""

    def test_main_installs_translations(self):
        source = (_ROOT / "gui_pyside6.py").read_text(encoding="utf-8")
        self.assertIn("install_qt_translations(app)", source,
                      "gui_pyside6.main() 必须在建好 QApplication 后加载中文翻译")

    def test_spec_collects_translation_files(self):
        spec = (_ROOT / "VisionFlow_Automator.spec").read_text(encoding="utf-8")
        self.assertIn("_translation_datas", spec)
        self.assertIn("qtbase_zh_CN.qm", spec)

    def test_build_verification_requires_translation_file(self):
        source = (_ROOT / "build_package.py").read_text(encoding="utf-8")
        self.assertIn("qtbase_zh_CN.qm", source,
                      "打包校验应把中文翻译文件列为必须保留项")

if __name__ == "__main__":
    unittest.main()
