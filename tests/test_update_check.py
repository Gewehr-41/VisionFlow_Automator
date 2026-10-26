# -*- coding: utf-8 -*-
"""F11 自动更新检查：版本比较、网络探测（网页跳转 / API / tags 三级）、界面提示。

关键背景（实测）：GitHub API 对未认证请求按 IP 限流，走共享代理时常直接 403；
而 `https://github.com/<repo>/releases/latest` 的网页跳转不消耗额度，所以优先用它，
失败才退到 API，再退到 tags。本文件逐条固定这个顺序，并用替身 opener 覆盖各条路径。
"""

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
import gui_pyside6 as g  # noqa: E402
import tasks  # noqa: E402
import update_check as uc  # noqa: E402

_APP = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# 版本号解析与比较
# ---------------------------------------------------------------------------

class VersionTests(unittest.TestCase):
    def test_parse_variants(self):
        self.assertEqual(uc.parse_version("v0.4.1"), (0, 4, 1))
        self.assertEqual(uc.parse_version("0.3.1"), (0, 3, 1))
        self.assertEqual(uc.parse_version("V1.2.3-rc1"), (1, 2, 3, 1))
        self.assertEqual(uc.parse_version(None), ())
        self.assertEqual(uc.parse_version("无版本号"), ())

    def test_is_newer(self):
        self.assertTrue(uc.is_newer("v0.6.0", "0.5.0"))
        self.assertFalse(uc.is_newer("v0.4.1", "0.5.0"))
        self.assertFalse(uc.is_newer("v0.5.0", "0.5.0"))
        self.assertTrue(uc.is_newer("0.5.1", "0.5"))       # 缺位补齐
        self.assertFalse(uc.is_newer("garbage", "0.5.0"))
        self.assertFalse(uc.is_newer("v0.6.0", "garbage"))

    def test_config_defaults(self):
        self.assertRegex(config.APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertEqual(config.UPDATE_REPO, "Gewehr-41/VisionFlow_Automator")
        self.assertTrue(config.CHECK_UPDATE_ON_START)
        self.assertGreater(config.UPDATE_CHECK_TIMEOUT, 0)


# ---------------------------------------------------------------------------
# 网络探测：三级顺序
# ---------------------------------------------------------------------------

def _opener_redirect(tag):
    calls = []

    def _opener(url, timeout, proxy):
        calls.append(url)
        if "api.github.com" in url:
            raise AssertionError("有网页跳转时不该再打 API")
        return f"https://github.com/o/r/releases/tag/{tag}", b""
    return _opener, calls


def _opener_json(payload_by_url):
    def _opener(url, timeout, proxy):
        for marker, payload in payload_by_url.items():
            if marker in url:
                if isinstance(payload, Exception):
                    raise payload
                return url, payload
        return url, b""
    return _opener


class FetchTests(unittest.TestCase):
    def test_web_redirect_is_preferred(self):
        opener, calls = _opener_redirect("v0.6.0")
        tag, url, name, error = uc.fetch_latest_release(repo="o/r", timeout=1, proxy="", opener=opener)
        self.assertIsNone(error)
        self.assertEqual(tag, "v0.6.0")
        self.assertIn("/releases/tag/v0.6.0", url)
        self.assertEqual(len(calls), 1, "网页跳转成功后不应再打 API")

    def test_web_page_without_redirect_falls_back_to_api(self):
        api_payload = b'{"tag_name": "v0.7.0", "html_url": "https://x/releases/tag/v0.7.0", "name": "v0.7.0"}'

        def opener(url, timeout, proxy):
            if "api.github.com" not in url:
                # 网页 /releases/latest 没有跳转（例如被登录墙挡住）
                return "https://github.com/o/r/releases", b""
            if url.endswith("/releases/latest"):
                return url, api_payload
            return url, b"[]"

        tag, url, name, error = uc.fetch_latest_release(repo="o/r", timeout=1, proxy="", opener=opener)
        self.assertIsNone(error)
        self.assertEqual(tag, "v0.7.0")
        self.assertEqual(url, "https://x/releases/tag/v0.7.0")

    def test_api_403_falls_back_to_tags(self):
        import urllib.error

        def opener(url, timeout, proxy):
            if "api.github.com" not in url:
                return "https://github.com/o/r/releases", b""
            if url.endswith("/releases/latest"):
                raise urllib.error.HTTPError(url, 403, "rate limited", None, None)
            return url, b'[{"name":"v0.4.1"},{"name":"v0.9.0"},{"name":"v0.5.0"}]'

        tag, url, name, error = uc.fetch_latest_release(repo="o/r", timeout=1, proxy="", opener=opener)
        self.assertIsNone(error)
        self.assertEqual(tag, "v0.9.0", "tags 兜底应取版本号最大的那个")

    def test_all_paths_failing_reports_error_without_raising(self):
        def _opener(url, timeout, proxy):
            raise OSError("网络不可达")

        tag, url, name, error = uc.fetch_latest_release(repo="o/r", timeout=1, proxy="", opener=_opener)
        self.assertEqual(tag, "")
        self.assertTrue(error)
        self.assertIn("网络不可达", error)

    def test_malformed_json_is_reported(self):
        opener = _opener_json({
            "/releases/latest": b"",
            "api.github.com/repos/o/r/releases/latest": b"{not json",
            "api.github.com/repos/o/r/tags": b"{also not json",
        })
        tag, url, name, error = uc.fetch_latest_release(repo="o/r", timeout=1, proxy="", opener=opener)
        self.assertEqual(tag, "")
        self.assertTrue(error)

    def test_response_shapes_are_normalised(self):
        self.assertEqual(uc._split_response(("u", b"x")), ("u", b"x"))
        self.assertEqual(uc._split_response(b"abc"), ("", b"abc"))
        self.assertEqual(uc._split_response("abc"), ("", b"abc"))

        class _Resp:
            def read(self):
                return b"payload"
        self.assertEqual(uc._split_response(_Resp()), ("", b"payload"))


class ProxyTests(unittest.TestCase):
    def test_explicit_config_wins(self):
        with mock.patch.object(config, "UPDATE_PROXY", "http://127.0.0.1:1234"):
            self.assertEqual(uc.resolve_proxy(), "http://127.0.0.1:1234")

    def test_env_var_is_used_when_config_empty(self):
        with mock.patch.object(config, "UPDATE_PROXY", ""):
            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=False):
                self.assertEqual(uc.resolve_proxy(), "http://127.0.0.1:7890")

    def test_registry_proxy_is_last_resort(self):
        with mock.patch.object(config, "UPDATE_PROXY", ""):
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(uc, "_registry_proxy", lambda: "http://127.0.0.1:7892"):
                    self.assertEqual(uc.resolve_proxy(), "http://127.0.0.1:7892")

    def test_no_proxy_returns_none(self):
        with mock.patch.object(config, "UPDATE_PROXY", ""):
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(uc, "_registry_proxy", lambda: None):
                    self.assertIsNone(uc.resolve_proxy())


# ---------------------------------------------------------------------------
# check_for_update 汇总
# ---------------------------------------------------------------------------

class CheckResultTests(unittest.TestCase):
    def test_newer_remote(self):
        opener, _ = _opener_redirect("v0.6.0")
        info = uc.check_for_update(current="0.5.0", repo="o/r", timeout=1, proxy="", opener=opener)
        self.assertTrue(info.has_update)
        self.assertEqual(info.remote_version, "v0.6.0")
        self.assertEqual(info.current_version, "0.5.0")

    def test_same_or_older_remote(self):
        for tag in ("v0.4.1", "v0.5.0"):
            opener, _ = _opener_redirect(tag)
            info = uc.check_for_update(current="0.5.0", repo="o/r", timeout=1, proxy="", opener=opener)
            self.assertFalse(info.has_update, tag)

    def test_error_is_returned_not_raised(self):
        def _opener(url, timeout, proxy):
            raise OSError("boom")

        info = uc.check_for_update(current="0.5.0", repo="o/r", timeout=1, proxy="", opener=_opener)
        self.assertFalse(info.has_update)
        self.assertTrue(info.error)


# ---------------------------------------------------------------------------
# 界面：按钮、结果处理、后台线程
# ---------------------------------------------------------------------------

class UpdateGuiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = list(g.TASKS)
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def _logs(self):
        return self.window.log_box.toPlainText()

    def test_button_exists(self):
        texts = [button.text() for button in self.window.task_buttons]
        self.assertIn("检查更新", texts)

    def test_newer_version_notifies_and_logs(self):
        info = uc.UpdateInfo(True, "v0.6.0", config.APP_VERSION, "https://example.com/dl", "v0.6.0", None)
        with mock.patch.object(g, "notify_update", return_value=True) as notifier:
            self.window._on_update_result(info, manual=True)
        self.assertTrue(notifier.called)
        self.assertIn("发现新版本 v0.6.0", self._logs())

    def test_up_to_date_is_logged_without_dialog(self):
        info = uc.UpdateInfo(False, "v0.4.1", config.APP_VERSION, "", "", None)
        with mock.patch.object(g, "notify_update", side_effect=AssertionError("不该弹窗")):
            self.window._on_update_result(info, manual=True)
        self.assertIn("已是最新版本", self._logs())

    def test_auto_check_failure_only_logs(self):
        info = uc.UpdateInfo(False, "", config.APP_VERSION, "", "", "HTTP 403")
        with mock.patch.object(g, "report_check_failure", side_effect=AssertionError("自动检查不该弹窗")):
            self.window._on_update_result(info, manual=False)
        self.assertIn("更新检查未完成", self._logs())

    def test_manual_check_failure_shows_dialog(self):
        info = uc.UpdateInfo(False, "", config.APP_VERSION, "", "", "HTTP 403")
        with mock.patch.object(g, "report_check_failure", return_value=None) as reporter:
            self.window._on_update_result(info, manual=True)
        self.assertTrue(reporter.called)

    def test_runner_delivers_result_in_background(self):
        from update_ui import UpdateCheckRunner

        info = uc.UpdateInfo(True, "v0.9.0", config.APP_VERSION, "u", "v0.9.0", None)
        received = []
        runner = UpdateCheckRunner(self.window)
        runner.finished.connect(received.append)
        with mock.patch.object(uc, "check_for_update", lambda current=None: info):
            runner.start()
        deadline = time.time() + 3.0
        while not received and time.time() < deadline:
            _APP.processEvents()
            time.sleep(0.01)
        self.assertTrue(received, "后台线程应把结果经信号送回 GUI 线程")
        self.assertEqual(received[0].remote_version, "v0.9.0")

    def test_start_does_not_block(self):
        started = time.time()
        with mock.patch.object(uc, "check_for_update", lambda current=None: None):
            self.window.start_update_check(manual=True)
        self.assertLess(time.time() - started, 1.0, "启动检查不应阻塞界面")


class StartupWiringTests(unittest.TestCase):
    def test_main_starts_check_guarded_by_config(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("CHECK_UPDATE_ON_START", source)
        self.assertIn("window.start_update_check(manual=False)", source)

    def test_version_is_set_on_application(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("setApplicationVersion(config.APP_VERSION)", source)


if __name__ == "__main__":
    unittest.main()
