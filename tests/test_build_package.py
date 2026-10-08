# -*- coding: utf-8 -*-
"""`build_package.py` 里可测的部分：部署必须**保住数据文件**，校验必须抓得住问题。

打包脚本真正危险的动作只有一个 —— 往分发目录里写文件。分发目录里放着用户的
4 个 `saved_*.json`，覆盖式部署会把它们删掉（这是脚本存在的首要理由）。所以
这里用临时目录把 deploy / verify / sync_data 全部跑一遍，断言：

  · 部署后数据文件逐字节不变、陈旧的程序文件被清掉、新程序文件就位；
  · 数据文件被动过 / 程序项与产物不一致 / 该有的运行库缺失 → 校验必须报出来；
  · `--sync-data` 是"先备份、再覆盖"，可回退。
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import build_package as bp


def write(path, text="x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def fake_dist(root):
    """造一个最小可用的"构建产物"：exe + _internal（含必须存在的运行库）。"""
    dist = Path(root) / "dist" / bp.PRODUCT
    write(dist / bp.EXE_NAME, "new-exe")
    write(dist / "_internal" / "lib.dll", "new-lib")
    for relative, _why in bp.REQUIRED_ENTRIES:
        target = dist / relative
        if relative.endswith(".dll"):
            write(target, "dll")
        else:
            write(target / "placeholder.txt", "x")
    write(dist / "_internal" / "icons" / "1.png", "png")
    return dist


def fake_target(root):
    """造一个"已经部署过"的分发目录：数据文件 + 上一版的程序文件。"""
    target = Path(root) / (bp.PRODUCT + "_打包版")
    write(target / bp.EXE_NAME, "old-exe")
    write(target / "_internal" / "lib.dll", "old-lib")
    write(target / "_internal" / "stale.dll", "should-be-gone")
    write(target / "_internal" / "icons" / "1.png", "png")
    for relative, _why in bp.REQUIRED_ENTRIES:
        path = target / relative
        if relative.endswith(".dll"):
            write(path, "dll")
        else:
            write(path / "placeholder.txt", "x")
    for index, name in enumerate(bp.DATA_FILE_NAMES):
        write(target / name, f"data-{index}")
    return target


class DeployTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dist = fake_dist(self.root)
        self.target = fake_target(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_program_entries_excludes_data_files(self):
        write(self.dist / bp.DATA_FILE_NAMES[0], "unexpected")
        names = [item.name for item in bp.program_entries(self.dist)]
        self.assertIn(bp.EXE_NAME, names)
        self.assertIn("_internal", names)
        self.assertNotIn(bp.DATA_FILE_NAMES[0], names)

    def test_deploy_replaces_program_and_keeps_data(self):
        before = bp.snapshot_data(self.target)
        self.assertEqual(len(before), len(bp.DATA_FILE_NAMES))
        bp.deploy(self.dist, self.target, log_callback=lambda *a, **k: None)
        # 数据文件逐字节未变
        self.assertEqual(bp.snapshot_data(self.target), before)
        # 程序项被替换
        self.assertEqual((self.target / bp.EXE_NAME).read_text(encoding="utf-8"), "new-exe")
        self.assertEqual((self.target / "_internal" / "lib.dll").read_text(encoding="utf-8"), "new-lib")
        # 上一版残留的程序文件被清掉（否则会留下混搭的运行库）
        self.assertFalse((self.target / "_internal" / "stale.dll").exists())

    def test_deploy_into_fresh_directory_creates_it_without_data(self):
        fresh = self.root / "新分发目录"
        result = bp.deploy(self.dist, fresh, log_callback=lambda *a, **k: None)
        self.assertTrue(result["fresh"])
        self.assertTrue((fresh / bp.EXE_NAME).exists())
        self.assertEqual(bp.data_files(fresh), [], "首次部署不应当凭空造出数据文件")

    def test_verify_passes_after_a_clean_deploy(self):
        before = bp.snapshot_data(self.target)
        bp.deploy(self.dist, self.target, log_callback=lambda *a, **k: None)
        self.assertEqual(bp.verify(self.dist, self.target, before), [])


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dist = fake_dist(self.root)
        self.target = fake_target(self.root)
        self.before = bp.snapshot_data(self.target)
        bp.deploy(self.dist, self.target, log_callback=lambda *a, **k: None)

    def tearDown(self):
        self._tmp.cleanup()

    def _with_problems(self):
        return bp.verify(self.dist, self.target, self.before)

    def test_detects_changed_data_file(self):
        write(self.target / bp.DATA_FILE_NAMES[0], "被人改过")
        problems = self._with_problems()
        self.assertTrue(any("数据文件被改动" in p for p in problems), problems)

    def test_detects_deleted_data_file(self):
        os.remove(self.target / bp.DATA_FILE_NAMES[1])
        problems = self._with_problems()
        self.assertTrue(any("数据文件丢失" in p for p in problems), problems)

    def test_detects_program_file_out_of_sync(self):
        write(self.target / bp.EXE_NAME, "被换成了别的东西")
        problems = self._with_problems()
        self.assertTrue(any("不一致" in p for p in problems), problems)

    def test_detects_missing_required_runtime(self):
        shutil.rmtree(self.target / "_internal" / "_tk_data")
        problems = self._with_problems()
        self.assertTrue(any("_tk_data" in p for p in problems), problems)

    def test_detects_empty_icon_library(self):
        for item in (self.target / "_internal" / "icons").glob("*.png"):
            os.remove(item)
        problems = self._with_problems()
        self.assertTrue(any("模板库是空的" in p for p in problems), problems)


class SyncDataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target = fake_target(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sync_copies_source_data_and_backs_up_the_old_one(self):
        source = self.root / "工程"
        for index, name in enumerate(bp.DATA_FILE_NAMES):
            write(source / name, f"source-{index}")
        result = bp.sync_data(self.target, source, log_callback=lambda *a, **k: None)
        self.assertEqual(len(result["copied"]), len(bp.DATA_FILE_NAMES))
        self.assertEqual((self.target / bp.DATA_FILE_NAMES[0]).read_text(encoding="utf-8"), "source-0")
        # 覆盖前那份必须还在备份目录里，能回退
        backups = list(Path(result["backup"]).glob("*.json"))
        self.assertEqual(len(backups), len(bp.DATA_FILE_NAMES))
        self.assertEqual((Path(result["backup"]) / bp.DATA_FILE_NAMES[0]).read_text(encoding="utf-8"), "data-0")

    def test_preflight_rejects_project_dir_as_target(self):
        ok, problems = bp.preflight(bp.ROOT)
        self.assertFalse(ok)
        self.assertTrue(any("不能是工程目录" in p for p in problems), problems)


class CommandLineTests(unittest.TestCase):
    """走一遍 main() 的接线：`--from-dist` 让"只更新分发目录"这条路径也能测。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dist = fake_dist(self.root)
        self.target = fake_target(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_from_dist_deploys_and_verifies_without_building(self):
        before = bp.snapshot_data(self.target)
        code = bp.main(["--from-dist", str(self.dist), "--target", str(self.target)])
        self.assertEqual(code, bp.EXIT_OK)
        self.assertEqual(bp.snapshot_data(self.target), before, "数据文件不该被动过")
        self.assertEqual((self.target / bp.EXE_NAME).read_text(encoding="utf-8"), "new-exe")

    def test_from_dist_rejects_a_directory_that_is_not_a_bundle(self):
        empty = self.root / "不是产物"
        empty.mkdir()
        code = bp.main(["--from-dist", str(empty), "--target", str(self.target)])
        self.assertEqual(code, bp.EXIT_PREFLIGHT)

    def test_zip_output_contains_program_and_data(self):
        bp.main(["--from-dist", str(self.dist), "--target", str(self.target)])
        archive = bp.make_zip(self.target)
        self.assertTrue(archive.exists())
        import zipfile
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
        self.assertTrue(any(name.endswith(bp.EXE_NAME) for name in names))
        self.assertTrue(any(name.endswith(bp.DATA_FILE_NAMES[0]) for name in names))
        archive.unlink()


if __name__ == "__main__":
    unittest.main()
