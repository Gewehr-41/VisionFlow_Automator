# -*- coding: utf-8 -*-
"""文件级更新清单与增量包。"""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import update_check
import update_manifest


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, root, relative, text):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_manifest_excludes_user_data(self):
        program = self.root / "program"
        self._write(program, "VisionFlow_Automator.exe", "exe")
        self._write(program, "_internal/PYZ-00.pyz", "pyz")
        self._write(program, "README.md", "readme")
        self._write(program, "saved_tasks.json", "user")
        self._write(program, "saved_settings.json", "user")
        self._write(program, "_internal/icons/captured_abc.png", "user")
        self._write(program, "_backup_update_1/old.exe", "backup")
        manifest = update_manifest.build_manifest(program, "0.5.2")
        paths = {item["path"] for item in manifest["files"]}
        self.assertEqual(paths, {"VisionFlow_Automator.exe", "_internal/PYZ-00.pyz", "README.md"})
        self.assertEqual(manifest["version"], "0.5.2")
        self.assertEqual(manifest["kind"], "full")

    def test_compare_and_delta_zip(self):
        old = self.root / "old"
        new = self.root / "new"
        self._write(old, "VisionFlow_Automator.exe", "old-exe")
        self._write(old, "_internal/same.dll", "same")
        self._write(old, "_internal/gone.dll", "gone")
        self._write(new, "VisionFlow_Automator.exe", "new-exe")
        self._write(new, "_internal/same.dll", "same")
        self._write(new, "_internal/new.dll", "new")
        previous = update_manifest.build_manifest(old, "0.5.1")
        current = update_manifest.build_manifest(new, "0.5.2")
        changed, deleted = update_manifest.compare_manifests(previous, current)
        self.assertEqual(changed, ["VisionFlow_Automator.exe", "_internal/new.dll"])
        self.assertEqual(deleted, ["_internal/gone.dll"])

        delta_path = self.root / "delta_from_v0.5.1_to_v0.5.2.zip"
        archive, changed2, deleted2, size = update_manifest.make_delta_zip(
            new, current, previous, delta_path)
        self.assertTrue(Path(archive).is_file())
        self.assertGreater(size, 0)
        with zipfile.ZipFile(archive) as package:
            names = set(package.namelist())
        self.assertIn("update_manifest.json", names)
        self.assertIn("payload/VisionFlow_Automator.exe", names)
        self.assertIn("payload/_internal/new.dll", names)
        self.assertNotIn("payload/_internal/same.dll", names)
        delta_manifest = update_manifest.read_manifest(archive)
        self.assertEqual(delta_manifest["kind"], "delta")
        self.assertEqual(delta_manifest["base_version"], "0.5.1")
        self.assertEqual(delta_manifest["deleted"], ["_internal/gone.dll"])

    def test_verify_payload(self):
        program = self.root / "program"
        self._write(program, "_internal/a.dll", "A")
        manifest = update_manifest.build_manifest(program, "0.5.2")
        staged = self.root / "staged"
        self._write(staged, "payload/_internal/a.dll", "A")
        delta = dict(manifest, kind="delta", base_version="0.5.1",
                     changed=["_internal/a.dll"], deleted=[])
        self.assertEqual(update_manifest.verify_payload(staged, delta), [])
        self._write(staged, "payload/_internal/a.dll", "B")
        self.assertTrue(update_manifest.verify_payload(staged, delta))


class PrepareUpdatePackageTests(unittest.TestCase):
    def _build_delta(self, root, base_version="0.5.2"):
        old = root / "old"
        new = root / "new"
        for directory, exe, extra in ((old, "old-exe", "old"), (new, "new-exe", "new")):
            directory.mkdir()
            (directory / "VisionFlow_Automator.exe").write_text(exe, encoding="utf-8")
            (directory / "_internal").mkdir()
            (directory / "_internal" / "a.dll").write_text(extra, encoding="utf-8")
        previous = update_manifest.build_manifest(old, base_version)
        current = update_manifest.build_manifest(new, "0.5.3")
        archive, _changed, _deleted, _size = update_manifest.make_delta_zip(
            new, current, previous, root / "delta.zip")
        install = root / "install"
        install.mkdir()
        (install / update_manifest.MANIFEST_NAME).write_text(
            '{"format":1,"kind":"full","version":"%s","base_version":"","deleted":[],"files":[]}'
            % base_version, encoding="utf-8")
        return Path(archive), install

    def test_prepares_delta_and_verifies_payload(self):
        import update_ui
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, install = self._build_delta(root)
            package = update_ui.prepare_update_package(archive, root / "extract", install)
            self.assertEqual(package["kind"], "delta")
            self.assertIn("VisionFlow_Automator.exe", package["changed"])
            self.assertTrue((root / "extract" / "payload" / "VisionFlow_Automator.exe").is_file())

    def test_rejects_delta_when_base_version_mismatch(self):
        import update_ui
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, install = self._build_delta(root, base_version="0.5.1")
            (install / update_manifest.MANIFEST_NAME).write_text(
                '{"format":1,"kind":"full","version":"0.5.0","base_version":"","deleted":[],"files":[]}',
                encoding="utf-8")
            with self.assertRaises(RuntimeError):
                update_ui.prepare_update_package(archive, root / "extract", install)


class OnlyDeltaAssetTests(unittest.TestCase):
    def test_delta_is_not_used_as_full_fallback(self):
        import update_ui
        with tempfile.TemporaryDirectory() as tmp:
            class Result:
                asset_url = "u-delta"
                full_url = ""
                delta_url = "u-delta"
                delta_name = "delta_from_v0.5.2_to_v0.5.3.zip"
                delta_size = 8
                delta_base_version = "0.5.2"
                full_name = ""
                full_size = 0

            self.assertIsNone(update_ui.choose_update_asset(Result(), tmp))


class AssetSelectionTests(unittest.TestCase):
    def test_picks_delta_for_matching_base(self):
        assets = [
            {"name": "VisionFlow_Automator_full.zip", "browser_download_url": "u-full", "size": 100},
            {"name": "delta_from_v0.5.2_to_v0.5.3.zip", "browser_download_url": "u-delta", "size": 8},
        ]
        selected = update_check.pick_update_assets(assets, current_version="0.5.2")
        self.assertEqual(selected["full"]["url"], "u-full")
        self.assertEqual(selected["delta"]["url"], "u-delta")
        # 旧版本不认识 delta，只会选名字含 VisionFlow 的完整包
        legacy = update_check.pick_update_asset(assets)
        self.assertEqual(legacy["url"], "u-full")

    def test_no_delta_when_base_does_not_match(self):
        assets = [
            {"name": "VisionFlow_Automator_full.zip", "browser_download_url": "u-full"},
            {"name": "delta_from_v0.5.2_to_v0.5.3.zip", "browser_download_url": "u-delta"},
        ]
        selected = update_check.pick_update_assets(assets, current_version="0.5.1")
        self.assertEqual(selected["full"]["url"], "u-full")
        self.assertIsNone(selected["delta"])


class ChooseAssetTests(unittest.TestCase):
    def test_local_manifest_enables_delta(self):
        import update_ui
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, update_manifest.MANIFEST_NAME).write_text(
                '{"format":1,"kind":"full","version":"0.5.2","base_version":"","deleted":[],"files":[]}',
                encoding="utf-8")

            class Result:
                asset_url = "u-default"
                full_url = "u-full"
                delta_url = "u-delta"
                delta_name = "delta_from_v0.5.2_to_v0.5.3.zip"
                delta_size = 8
                delta_base_version = "0.5.2"
                full_name = "VisionFlow_Automator_full.zip"
                full_size = 100

            self.assertEqual(update_ui.choose_update_asset(Result(), tmp)["kind"], "delta")

    def test_no_local_manifest_falls_back_to_full(self):
        import update_ui
        with tempfile.TemporaryDirectory() as tmp:
            class Result:
                asset_url = "u-default"
                full_url = "u-full"
                delta_url = "u-delta"
                delta_name = "delta_from_v0.5.2_to_v0.5.3.zip"
                delta_size = 8
                delta_base_version = "0.5.2"
                full_name = "VisionFlow_Automator_full.zip"
                full_size = 100

            self.assertEqual(update_ui.choose_update_asset(Result(), tmp)["kind"], "full")


if __name__ == "__main__":
    unittest.main()
