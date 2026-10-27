# -*- coding: utf-8 -*-
"""L3：检查更新的代理可在程序内设置（2026-10-27）。

之前代理只能在 `config.py` 改 `UPDATE_PROXY`，普通用户无法在界面里设置。
现在状态栏有「设置」：填写代理地址保存到数据目录 `saved_settings.json`，
更新检查的代理优先级为：`config.UPDATE_PROXY` → 程序内设置 → 环境变量 → Windows 系统代理 → 直连。
"""

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
import gui_pyside6 as g  # noqa: E402
import settings_store  # noqa: E402
import tasks  # noqa: E402
import update_check as uc  # noqa: E402
import update_ui  # noqa: E402

_APP = QApplication.instance() or QApplication([])

PROXY = "http://127.0.0.1:7892"


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._settings_file = os.path.join(self._tmp.name, "saved_settings.json")
        self._patch = mock.patch.object(settings_store, "SETTINGS_FILE", self._settings_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()


class SettingsStoreTests(StoreTestBase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(settings_store.load_settings(), {})
        self.assertEqual(settings_store.get_update_proxy(), "")

    def test_roundtrip_and_trim(self):
        settings_store.set_update_proxy("  " + PROXY + "  ")
        self.assertEqual(settings_store.get_update_proxy(), PROXY)
        with open(self._settings_file, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["update_proxy"], PROXY)

    def test_invalid_json_is_tolerated(self):
        with open(self._settings_file, "w", encoding="utf-8") as handle:
            handle.write("{ 坏 JSON")
        self.assertEqual(settings_store.load_settings(), {})
        settings_store.set_update_proxy(PROXY)
        self.assertEqual(settings_store.get_update_proxy(), PROXY, "坏文件应能被覆盖修复")

    def test_clearing_proxy(self):
        settings_store.set_update_proxy(PROXY)
        settings_store.set_update_proxy("")
        self.assertEqual(settings_store.get_update_proxy(), "")


class ProxyPrecedenceTests(StoreTestBase):
    def test_user_setting_wins_over_env_and_registry(self):
        settings_store.set_update_proxy(PROXY)
        with mock.patch.object(config, "UPDATE_PROXY", ""):
            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://env:1"}, clear=False):
                with mock.patch.object(uc, "_registry_proxy", lambda: "http://reg:2"):
                    self.assertEqual(uc.resolve_proxy(), PROXY)

    def test_config_explicit_wins_over_user_setting(self):
        settings_store.set_update_proxy(PROXY)
        with mock.patch.object(config, "UPDATE_PROXY", "http://cfg:3"):
            self.assertEqual(uc.resolve_proxy(), "http://cfg:3")

    def test_env_used_when_no_user_setting(self):
        with mock.patch.object(config, "UPDATE_PROXY", ""):
            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://env:1"}, clear=False):
                with mock.patch.object(uc, "_registry_proxy", lambda: "http://reg:2"):
                    self.assertEqual(uc.resolve_proxy(), "http://env:1")

    def test_direct_when_nothing_set(self):
        with mock.patch.object(config, "UPDATE_PROXY", ""):
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(uc, "_registry_proxy", lambda: None):
                    self.assertIsNone(uc.resolve_proxy())


class SettingsUiTests(StoreTestBase):
    @classmethod
    def setUpClass(cls):
        cls._data_tmp = tempfile.TemporaryDirectory()
        cls._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(cls._data_tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(cls._data_tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        cls._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = [{"id": "A", "type": "delay", "description": "等待", "duration": 0, "enabled": True}]
        cls.window = g.PySide6ScriptWindow()
        cls.window.show()
        _APP.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        g.TASKS[:] = cls._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = cls._orig
        cls._data_tmp.cleanup()

    def setUp(self):
        super().setUp()
        settings_store.set_update_proxy("")

    def test_settings_button_is_in_status_bar_before_help(self):
        self.assertTrue(self.window.status_bar_widget.isAncestorOf(self.window.settings_button))
        self.assertLess(self.window.settings_button.x(), self.window.help_button.x(),
                        "「设置」应在「使用说明」左边")

    def test_dialog_prefills_saved_proxy_and_shows_path(self):
        settings_store.set_update_proxy(PROXY)
        dialog = self.window._create_settings_dialog()
        try:
            self.assertEqual(dialog.proxy_edit.text(), PROXY)
            labels = [label.text() for label in dialog.findChildren(g.QLabel)]
            self.assertTrue(any("saved_settings.json" in text for text in labels),
                            "对话框应显示设置文件位置")
        finally:
            dialog.deleteLater()

    def test_save_writes_setting_and_logs(self):
        dialog = self.window._create_settings_dialog()
        dialog.proxy_edit.setText("  " + PROXY + "  ")
        self.window._save_update_proxy(dialog.proxy_edit)
        _APP.processEvents()
        self.assertEqual(settings_store.get_update_proxy(), PROXY)
        self.assertIn("已保存更新代理", self.window.log_box.toPlainText())
        dialog.deleteLater()

    def test_clear_proxy_logs_auto_detect(self):
        settings_store.set_update_proxy(PROXY)
        dialog = self.window._create_settings_dialog()
        dialog.proxy_edit.setText("")
        self.window._save_update_proxy(dialog.proxy_edit)
        _APP.processEvents()
        self.assertEqual(settings_store.get_update_proxy(), "")
        self.assertIn("已清空更新代理", self.window.log_box.toPlainText())
        dialog.deleteLater()


class UpdateFailureHintTests(unittest.TestCase):
    def test_failure_hint_points_to_settings_button(self):
        class _Result:
            error = "连接超时"

        with mock.patch.object(update_ui.QMessageBox, "information") as info:
            update_ui.report_check_failure(None, _Result())
        self.assertTrue(info.called)
        text = info.call_args[0][2]
        self.assertIn("设置", text)
        self.assertIn("代理", text)
        self.assertNotIn("config.py", text, "普通用户提示不应再让他去改 config.py")


class SettingsGuardTests(unittest.TestCase):
    def test_implementation_markers(self):
        store_source = open("settings_store.py", encoding="utf-8").read()
        check_source = open("update_check.py", encoding="utf-8").read()
        gui_source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("SETTINGS_FILE", store_source)
        self.assertIn("def get_update_proxy", store_source)
        self.assertIn("def _user_proxy", check_source)
        self.assertIn("user_proxy = _user_proxy()", check_source)
        self.assertIn("def open_settings_dialog", gui_source)
        self.assertIn("def _save_update_proxy", gui_source)
        self.assertIn("settings_store.set_update_proxy", gui_source)


if __name__ == "__main__":
    unittest.main()
