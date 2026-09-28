# -*- coding: utf-8 -*-
"""路径收口的测试。

背景
----
路径此前分散在三处、都用 `os.path.dirname(__file__)` 推导（`config.ICON_DIR`、
`tasks` 的四个 JSON、`gui_pyside6` 里两个 Qt 样式 SVG）。打包成 exe 后
`__file__` 指向 PyInstaller 的**临时解压目录**，四个 JSON 会被写进去、退出即丢。

现在全部收进 `paths.py`：只读资源走 `resource_root()`（打包后是 `sys._MEIPASS`），
可写数据走 `data_dir()`（打包后是 exe 所在目录，可用 `VF_DATA_DIR` 覆盖）。

这个文件既验证行为，也用一条静态守卫**防止收口被重新打散**。
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config
import paths
import tasks


class PathFunctionsTests(unittest.TestCase):
    def test_source_mode_uses_the_project_root_for_both_roots(self):
        """源码运行时：资源目录与数据目录都是项目根。"""
        with mock.patch.object(paths, "_frozen", return_value=False):
            self.assertEqual(Path(paths.app_root()), PROJECT_ROOT)
            self.assertEqual(Path(paths.resource_root()), PROJECT_ROOT)

    def test_frozen_mode_puts_data_next_to_the_exe_and_assets_in_the_bundle(self):
        """打包运行：数据在 exe 旁边（可写），资源在解压目录（只读）。"""
        fake_exe = str(PROJECT_ROOT / "dist" / "VisionFlow.exe")
        fake_bundle = str(PROJECT_ROOT / "dist" / "_internal")
        with mock.patch.object(paths, "_frozen", return_value=True), \
                mock.patch.object(sys, "executable", fake_exe), \
                mock.patch.object(sys, "_MEIPASS", fake_bundle, create=True):
            self.assertEqual(paths.app_root(), str(PROJECT_ROOT / "dist"))
            self.assertEqual(paths.resource_root(), fake_bundle)
            self.assertEqual(paths.data_dir(), str(PROJECT_ROOT / "dist"))

    def test_data_dir_env_override_wins(self):
        with mock.patch.dict(os.environ, {"VF_DATA_DIR": str(PROJECT_ROOT / "probe_dir")}):
            self.assertEqual(paths.data_dir(), str(PROJECT_ROOT / "probe_dir"))

    def test_empty_env_override_is_ignored(self):
        with mock.patch.dict(os.environ, {"VF_DATA_DIR": ""}):
            self.assertEqual(paths.data_dir(), paths.app_root())

    def test_describe_mentions_both_roots(self):
        text = paths.describe()
        self.assertIn("资源目录=", text)
        self.assertIn("数据目录=", text)
        self.assertIn("源码运行", text)


class PathConstantsTests(unittest.TestCase):
    def test_constants_derive_from_the_functions(self):
        self.assertEqual(paths.ICON_DIR, os.path.join(paths.resource_root(), "icons"))
        for name, file_name in (("TASKS_FILE", "saved_tasks.json"),
                                ("PRESETS_FILE", "saved_presets.json"),
                                ("BLUEPRINT_LAYOUT_FILE", "saved_blueprint_layouts.json"),
                                ("BLUEPRINT_GRAPH_FILE", "saved_blueprint_graphs.json")):
            with self.subTest(name=name):
                self.assertEqual(getattr(paths, name), os.path.join(paths.data_dir(), file_name))
                self.assertIn(file_name, paths.DATA_FILE_NAMES)

    def test_config_and_tasks_share_the_single_source(self):
        """config / tasks 暴露的名字必须是同一个来源，不能各算一份。"""
        self.assertEqual(config.ICON_DIR, paths.ICON_DIR)
        self.assertEqual(tasks.TASKS_FILE, paths.TASKS_FILE)
        self.assertEqual(tasks.PRESETS_FILE, paths.PRESETS_FILE)
        self.assertEqual(tasks.BLUEPRINT_LAYOUT_FILE, paths.BLUEPRINT_LAYOUT_FILE)
        self.assertEqual(tasks.BLUEPRINT_GRAPH_FILE, paths.BLUEPRINT_GRAPH_FILE)

    def test_icon_dir_points_at_the_real_template_library(self):
        self.assertTrue(os.path.isdir(paths.ICON_DIR), "模板目录必须存在")
        self.assertTrue(list(Path(paths.ICON_DIR).glob("*.png")), "模板目录里应有 PNG")


class PathSourceGuardTests(unittest.TestCase):
    """静态守卫：生产代码里只有 paths.py 可以自己算路径。"""

    ALLOWED = {"paths.py"}

    def test_no_other_module_computes_paths_from_dunder_file(self):
        offenders = []
        for path in sorted(PROJECT_ROOT.glob("*.py")) + sorted((PROJECT_ROOT / "core").glob("*.py")):
            if path.name in self.ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if "__file__" in line and not line.strip().startswith("#"):
                    offenders.append(f"{path.name}:{number} {line.strip()[:70]}")
        self.assertEqual(
            offenders, [],
            "路径必须只在 paths.py 里算（其余模块从那里导入），否则打包后 __file__ "
            "会指向临时解压目录：\n  " + "\n  ".join(offenders),
        )

    def test_env_override_takes_effect_at_import_time(self):
        """`VF_DATA_DIR` 必须在**导入时**就生效（便携部署靠它把数据放到任意目录）。"""
        env = dict(os.environ)
        target = PROJECT_ROOT / "probe_data_dir"
        env["VF_DATA_DIR"] = str(target)
        result = subprocess.run(
            [sys.executable, "-c", "import paths, tasks; print(tasks.TASKS_FILE); print(paths.ICON_DIR)"],
            capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(Path(lines[0]), target / "saved_tasks.json")
        self.assertEqual(Path(lines[1]), PROJECT_ROOT / "icons",
                         "资源目录不受 VF_DATA_DIR 影响")


if __name__ == "__main__":
    unittest.main()
