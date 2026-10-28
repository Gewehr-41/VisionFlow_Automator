# -*- coding: utf-8 -*-
"""增量更新助手：逐文件替换、删除、回滚、用户数据不变。"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import update_manifest
import update_ui


class DeltaHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.install = self.root / "安装 目录"
        self.staged = self.root / "staged"
        self._make_install()
        self._make_staged()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, root, relative, text):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _make_install(self):
        self._write(self.install, "VisionFlow_Automator.exe", "old-exe")
        self._write(self.install, "_internal/a.dll", "old-a")
        self._write(self.install, "_internal/same.dll", "same")
        self._write(self.install, "_internal/gone.dll", "gone")
        self._write(self.install, "saved_tasks.json", "user-data")
        self._write(self.install, update_manifest.MANIFEST_NAME, '{"version":"0.5.2","files":[]}')

    def _make_staged(self):
        self._write(self.staged, "payload/VisionFlow_Automator.exe", "new-exe")
        self._write(self.staged, "payload/_internal/a.dll", "new-a")
        self._write(self.staged, "payload/_internal/added.dll", "added")
        manifest = {
            "format": 1,
            "kind": "delta",
            "version": "0.5.3",
            "base_version": "0.5.2",
            "deleted": ["_internal/gone.dll"],
            "changed": ["VisionFlow_Automator.exe", "_internal/a.dll", "_internal/added.dll"],
            "files": [
                {"path": "VisionFlow_Automator.exe", "size": 7,
                 "sha256": update_manifest.sha256_file(self.staged / "payload/VisionFlow_Automator.exe")},
                {"path": "_internal/a.dll", "size": 5,
                 "sha256": update_manifest.sha256_file(self.staged / "payload/_internal/a.dll")},
                {"path": "_internal/added.dll", "size": 5,
                 "sha256": update_manifest.sha256_file(self.staged / "payload/_internal/added.dll")},
            ],
        }
        self._write(self.staged, update_manifest.MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False))

    def _run_helper(self, package):
        script = Path(update_ui.build_delta_update_helper(
            self.install, package, 999999, log_dir=self.root))
        lines = []
        for line in script.read_text(encoding="utf-8-sig").splitlines():
            if "Start-Process -FilePath (Join-Path $DST" in line:
                lines.append("Write-UpLog 'skip launch'")
            else:
                lines.append(line)
        script.write_bytes(b"\xef\xbb\xbf" + "\r\n".join(lines).encode("utf-8"))
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            timeout=90, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout.decode("utf-8", "replace"))

    def _package(self, changed=None):
        manifest = update_manifest.read_manifest(self.staged / update_manifest.MANIFEST_NAME)
        return {
            "kind": "delta",
            "root": self.staged,
            "manifest": manifest,
            "changed": list(changed if changed is not None else manifest["changed"]),
            "deleted": list(manifest["deleted"]),
        }

    def test_applies_changed_added_and_deleted_files(self):
        self._run_helper(self._package())
        self.assertEqual((self.install / "VisionFlow_Automator.exe").read_text(encoding="utf-8"), "new-exe")
        self.assertEqual((self.install / "_internal/a.dll").read_text(encoding="utf-8"), "new-a")
        self.assertEqual((self.install / "_internal/added.dll").read_text(encoding="utf-8"), "added")
        self.assertFalse((self.install / "_internal/gone.dll").exists())
        self.assertEqual((self.install / "_internal/same.dll").read_text(encoding="utf-8"), "same")
        self.assertEqual((self.install / "saved_tasks.json").read_text(encoding="utf-8"), "user-data",
                         "增量更新不能改用户数据")
        installed = update_manifest.load_local_manifest(self.install)
        self.assertEqual(installed["version"], "0.5.3")

    def test_rolls_back_when_payload_missing(self):
        package = self._package(changed=["VisionFlow_Automator.exe", "_internal/a.dll",
                                         "missing.dll"])
        # 用 helper 直接失败：不经过 prepare 的预校验，验证助手自身的回滚
        script = Path(update_ui.build_delta_update_helper(
            self.install, package, 999999, log_dir=self.root))
        lines = []
        for line in script.read_text(encoding="utf-8-sig").splitlines():
            if "Start-Process -FilePath (Join-Path $DST" in line:
                lines.append("Write-UpLog 'skip launch'")
            else:
                lines.append(line)
        script.write_bytes(b"\xef\xbb\xbf" + "\r\n".join(lines).encode("utf-8"))
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            timeout=90, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual((self.install / "VisionFlow_Automator.exe").read_text(encoding="utf-8"), "old-exe")
        self.assertEqual((self.install / "_internal/a.dll").read_text(encoding="utf-8"), "old-a")
        self.assertTrue((self.install / "_internal/gone.dll").exists())
        self.assertFalse((self.install / "_internal/added.dll").exists(), "新增文件回滚时应删除")
        self.assertEqual((self.install / "saved_tasks.json").read_text(encoding="utf-8"), "user-data")


if __name__ == "__main__":
    unittest.main()
