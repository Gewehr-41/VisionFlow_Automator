# -*- coding: utf-8 -*-
"""打包版自定义图标（2026-10-27）。

用户把自己的 `app.ico` 放到工程根目录（或 `icons/app.ico`）后重新打包：
- `.spec` 用它作为 exe 图标（Explorer/快捷方式看到的图标）；
- 同时把 `app.ico` 放进 `_internal`，启动时 `_apply_window_icon()` 设置窗口/任务栏图标。
没有 `app.ico` 时保持默认行为。
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import paths  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _write_ico(path):
    image = Image.new("RGB", (256, 256), (40, 120, 200))
    image.save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    return path


class ApplyWindowIconTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self._patch = mock.patch.object(paths, "resource_root", lambda: self.root)
        self._patch.start()
        self._saved_icon = _APP.windowIcon()

    def tearDown(self):
        self._APP_restore()
        self._patch.stop()
        self._tmp.cleanup()

    def _APP_restore(self):
        _APP.setWindowIcon(self._saved_icon)

    def test_applies_icon_when_app_ico_exists(self):
        _write_ico(os.path.join(self.root, "app.ico"))
        g._apply_window_icon(_APP)
        self.assertFalse(_APP.windowIcon().isNull(), "有 app.ico 时应设置窗口图标")

    def test_keeps_default_without_app_ico(self):
        g._apply_window_icon(_APP)
        self.assertTrue(_APP.windowIcon().isNull() or _APP.windowIcon().isNull() == self._saved_icon.isNull())

    def test_invalid_ico_is_ignored_without_crash(self):
        with open(os.path.join(self.root, "app.ico"), "wb") as handle:
            handle.write(b"not an icon")
        g._apply_window_icon(_APP)      # 不应抛异常
        self.assertTrue(QIcon(os.path.join(self.root, "app.ico")).isNull())


class AppIconGuardTests(unittest.TestCase):
    def test_spec_and_build_support_custom_icon(self):
        spec = open("VisionFlow_Automator.spec", encoding="utf-8").read()
        gui = open("gui_pyside6.py", encoding="utf-8").read()
        build = open("build_package.py", encoding="utf-8").read()
        self.assertIn("def _app_icon()", spec)
        self.assertIn("icon=_app_icon()", spec, "EXE 需要用 app.ico")
        self.assertIn("_runtime_icon_datas", spec)
        self.assertIn("app.ico", spec)
        self.assertIn("def _apply_window_icon(app)", gui)
        self.assertIn("_apply_window_icon(app)", gui)
        self.assertIn("app.ico", build)


if __name__ == "__main__":
    unittest.main()
