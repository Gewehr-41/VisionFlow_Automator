# -*- coding: utf-8 -*-
"""一键自动更新（2026-10-28）：下载校验 → 退出后替换 → 自动重启。

测试只覆盖可安全验证的部分：资产判定、zip 解压校验（含路径穿越）、
替换助手脚本内容；不实际执行替换/重启，避免测试环境里真的改动文件。
"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import update_ui  # noqa: E402


class _Result:
    def __init__(self, asset_url=""):
        self.asset_url = asset_url


def _make_zip(path, with_exe=True, evil=False):
    with zipfile.ZipFile(path, "w") as package:
        if evil:
            package.writestr("../evil.exe", "x")
            return path
        if with_exe:
            package.writestr("VisionFlow_Automator/VisionFlow_Automator.exe", "exe")
        package.writestr("VisionFlow_Automator/_internal/lib.dll", "dll")
    return path


class CanAutoUpdateTests(unittest.TestCase):
    def test_requires_frozen_and_asset(self):
        with mock.patch.object(update_ui.sys, "frozen", True, create=True):
            self.assertTrue(update_ui.can_auto_update(_Result("https://x/vf.zip")))
            self.assertFalse(update_ui.can_auto_update(_Result("")))

    def test_source_run_never_auto_updates(self):
        with mock.patch.object(update_ui.sys, "frozen", False, create=True):
            self.assertFalse(update_ui.can_auto_update(_Result("https://x/vf.zip")))


class ExtractUpdateZipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_and_locates_root(self):
        archive = _make_zip(self.root / "u.zip")
        root = update_ui.extract_update_zip(archive, self.root / "out")
        self.assertEqual(root.name, "VisionFlow_Automator")
        self.assertTrue((root / "VisionFlow_Automator.exe").is_file())

    def test_rejects_zip_without_exe(self):
        archive = _make_zip(self.root / "u.zip", with_exe=False)
        with self.assertRaises(RuntimeError):
            update_ui.extract_update_zip(archive, self.root / "out")

    def test_rejects_zip_slip(self):
        archive = _make_zip(self.root / "u.zip", evil=True)
        with self.assertRaises(RuntimeError):
            update_ui.extract_update_zip(archive, self.root / "out")

    def test_rejects_broken_zip(self):
        bad = self.root / "bad.zip"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(RuntimeError):
            update_ui.extract_update_zip(bad, self.root / "out")


class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.install = self.root / "安装 目录"
        self.staged = self.root / "staged 目录"
        self.install.mkdir()
        self.staged.mkdir()
        (self.install / update_ui.UPDATE_EXE).write_text("old", encoding="utf-8")
        (self.staged / update_ui.UPDATE_EXE).write_text("new", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_helper_contains_wait_backup_copy_restart(self):
        bat = Path(update_ui.build_update_helper(self.install, self.staged, 4321, log_dir=self.root))
        text = bat.read_text(encoding="utf-8")
        self.assertIn("PID eq 4321", text)
        self.assertIn(update_ui.UPDATE_BACKUP_PREFIX, text)
        self.assertIn("robocopy", text)
        self.assertIn('start "" "%DST%\\' + update_ui.UPDATE_EXE + '"', text)
        self.assertIn("goto restore", text)
        self.assertNotIn("saved_tasks.json", text, "更新助手不应碰用户数据")

    @unittest.skipUnless(os.name == "nt", "替换助手是 Windows 批处理")
    def test_helper_replaces_files_and_backs_up_old_program(self):
        """实际跑一遍助手（把 start 重启替换成 echo），验证备份 + 替换 + 用户数据不动。"""
        import subprocess

        (self.install / "_internal").mkdir()
        (self.install / "_internal" / "old.dll").write_text("old-lib", encoding="utf-8")
        (self.install / "saved_tasks.json").write_text("user-data", encoding="utf-8")
        (self.staged / "_internal").mkdir()
        (self.staged / "_internal" / "new.dll").write_text("new-lib", encoding="utf-8")
        bat = Path(update_ui.build_update_helper(self.install, self.staged, 999999, log_dir=self.root))
        lines = []
        for line in bat.read_text(encoding="utf-8").splitlines():
            lines.append("echo [skip launch]" if line.strip().startswith("start ") else line)
        bat.write_bytes("\r\n".join(lines).encode("utf-8"))
        completed = subprocess.run(["cmd.exe", "/c", str(bat)], timeout=60,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(completed.returncode, 0, completed.stdout.decode("utf-8", "replace"))
        self.assertEqual((self.install / update_ui.UPDATE_EXE).read_text(encoding="utf-8"), "new")
        self.assertTrue((self.install / "_internal" / "new.dll").exists())
        self.assertEqual((self.install / "saved_tasks.json").read_text(encoding="utf-8"), "user-data",
                         "用户数据不能被更新助手改动")
        backups = list(self.install.glob(update_ui.UPDATE_BACKUP_PREFIX + "*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / update_ui.UPDATE_EXE).read_text(encoding="utf-8"), "old")

    def test_helper_paths_are_absolute(self):
        bat = Path(update_ui.build_update_helper(self.install, self.staged, 1, log_dir=self.root))
        text = bat.read_text(encoding="utf-8")
        self.assertIn(str(self.install.resolve()), text)
        self.assertIn(str(self.staged.resolve()), text)


class AutoUpdateGuardTests(unittest.TestCase):
    def test_gui_and_ui_markers(self):
        gui = open("gui_pyside6.py", encoding="utf-8").read()
        ui = open("update_ui.py", encoding="utf-8").read()
        self.assertIn("def _offer_update", gui)
        self.assertIn("def _start_auto_update", gui)
        self.assertIn("def _install_update", gui)
        self.assertIn("can_auto_update", gui)
        self.assertIn("ask_update_choice", ui)
        self.assertIn("def can_auto_update", ui)
        self.assertIn("def build_update_helper", ui)
        self.assertIn("def extract_update_zip", ui)


if __name__ == "__main__":
    unittest.main()
