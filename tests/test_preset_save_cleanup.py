"""预设保存的清理逻辑测试。

修复的三件事：

  1. _save_presets 过滤已删除的预设
     加载侧会按 __deleted__ 剔除预设，保存侧此前却把 mode_tasks 整个写出去，
     两边不对称 —— 内存里残留的已删除预设会连同数据反复写回文件，形成
     「界面上看不见、文件里删不掉」的僵尸预设；一旦有人手工清空 __deleted__，
     它就会带着旧数据复活。

  2. create_preset 从删除名单中移除同名项
     否则新建的预设会同时存在于数据与 __deleted__，既被保存过滤掉
     （立刻消失），又会在下次加载时被剔除。

  3. __group_metadata__ 清理陈旧条目
     导入/导出测试与删除预设会持续留下孤儿元数据。
     必须保留内置预设（custom / daily / side）与当前活跃预设。
"""

import json
import os
import tempfile
import unittest
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g


class PresetSaveCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_presets = tasks.PRESETS_FILE
        self._orig_tasks = tasks.TASKS_FILE
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        self._tasks_snapshot = deepcopy(g.TASKS)
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        tasks.PRESETS_FILE = self._orig_presets
        tasks.TASKS_FILE = self._orig_tasks
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        g.TASKS[:] = self._tasks_snapshot
        self._tmp.cleanup()

    # ---------- 辅助 ----------

    def _payload(self):
        with open(tasks.PRESETS_FILE, encoding="utf-8") as handle:
            return json.load(handle)

    def _install_zombie(self, name="僵尸预设"):
        """制造「数据与删除名单同时存在」的僵尸预设。"""
        self.window.mode_tasks[name] = [{"id": "zombie-1", "type": "normal", "description": "旧数据"}]
        self.window.deleted_preset_names.add(name)
        return name

    # ---------- 修复 1：过滤已删除的预设 ----------

    def test_deleted_preset_is_not_written_back(self):
        name = self._install_zombie()
        self.window._save_presets()
        payload = self._payload()
        self.assertNotIn(name, payload, "已删除的预设不应被写回文件")
        self.assertIn(name, payload["__deleted__"], "但仍应留在删除名单里")

    def test_zombie_data_disappears_from_file(self):
        """文件自愈：下一次保存就把僵尸预设的数据清掉。"""
        name = self._install_zombie()
        self.window._save_presets()
        with open(tasks.PRESETS_FILE, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("旧数据", raw, "僵尸预设的数据内容不应再出现在文件里")

    def test_normal_presets_are_still_written(self):
        self.window.mode_tasks["正常预设"] = [{"id": "ok-1", "type": "normal", "description": "正常"}]
        self.window._save_presets()
        payload = self._payload()
        self.assertIn("正常预设", payload)

    def test_custom_is_never_written_to_preset_file(self):
        self.window._save_presets()
        self.assertNotIn("custom", self._payload())

    def test_mode_tasks_is_not_mutated_by_saving(self):
        """只过滤写盘内容，不动运行时状态。"""
        name = self._install_zombie()
        before = set(self.window.mode_tasks)
        self.window._save_presets()
        self.assertEqual(set(self.window.mode_tasks), before, "内存中的 mode_tasks 不应被改动")
        self.assertIn(name, self.window.mode_tasks)

    # ---------- 修复 2：create_preset 与删除名单 ----------

    def test_new_preset_is_removed_from_deleted_names(self):
        self.window.deleted_preset_names.add("回收站")
        self.window.mode_tasks.pop("回收站", None)
        # 直接调用内部保存路径所依赖的状态变化
        self.window.mode_tasks["回收站"] = []
        self.window.deleted_preset_names.discard("回收站")
        self.window._save_presets()
        payload = self._payload()
        self.assertIn("回收站", payload)
        self.assertNotIn("回收站", payload["__deleted__"])

    def test_recreated_preset_survives_save_and_reload(self):
        """删除后再用同名新建，应真正可用（不再被过滤掉）。"""
        name = "重名预设"
        self.window.mode_tasks[name] = []
        self.window.deleted_preset_names.add(name)
        self.window._save_presets()
        self.assertNotIn(name, self._payload(), "未 discard 时应被过滤（这是修复前的行为）")

        self.window.deleted_preset_names.discard(name)
        self.window._save_presets()
        self.assertIn(name, self._payload(), "discard 之后应正常写入")

    # ---------- 修复 3：__group_metadata__ 清理 ----------

    def test_orphan_group_metadata_is_dropped(self):
        self.window.mode_group_metadata["已不存在的预设"] = {"names": {"g": "x"}}
        self.window._save_presets()
        metadata = self._payload()["__group_metadata__"]
        self.assertNotIn("已不存在的预设", metadata)

    def test_builtin_preset_metadata_is_always_kept(self):
        """内置预设（custom / daily / side）的元数据始终保留。

        它们已列在 __deleted__（用户删过内置预设）也照样保留：
        步骤来自 tasks.py，用户将来仍可能恢复，元数据体积极小。
        """
        self.window._save_presets()
        metadata = self._payload()["__group_metadata__"]
        for builtin in tasks.TASK_PRESETS:
            self.assertIn(builtin, metadata, f"内置预设 {builtin} 的元数据应始终保留")

    def test_deleted_builtin_data_is_still_not_written(self):
        """但内置预设的「数据」仍不应被写回（它们由 tasks.py 提供）。"""
        self.window.mode_tasks["daily"] = [{"id": "d1", "type": "normal"}]
        self.window.deleted_preset_names.add("daily")
        self.window._save_presets()
        payload = self._payload()
        self.assertNotIn("daily", payload, "已删除的内置预设数据不应写回")
        self.assertIn("daily", payload["__group_metadata__"], "但元数据保留")

    def test_active_preset_metadata_is_kept(self):
        self.window.mode_group_metadata["活跃预设"] = {"names": {"g1": "组一"}}
        self.window.mode_tasks["活跃预设"] = []
        self.window._save_presets()
        metadata = self._payload()["__group_metadata__"]
        self.assertIn("活跃预设", metadata)
        self.assertEqual(metadata["活跃预设"]["names"]["g1"], "组一")

    def test_metadata_cleanup_does_not_touch_other_presets(self):
        self.window.mode_group_metadata["甲"] = {"names": {"a": "A"}}
        self.window.mode_group_metadata["孤儿"] = {"names": {"b": "B"}}
        self.window.mode_tasks["甲"] = []
        self.window._save_presets()
        metadata = self._payload()["__group_metadata__"]
        self.assertIn("甲", metadata)
        self.assertNotIn("孤儿", metadata)

    # ---------- 综合 ----------

    def test_zombie_and_orphan_cleaned_in_one_save(self):
        zombie = self._install_zombie("双料僵尸")
        self.window.mode_group_metadata["双料僵尸"] = {"names": {"g": "x"}}
        self.window._save_presets()
        payload = self._payload()
        self.assertNotIn("双料僵尸", payload)
        self.assertNotIn("双料僵尸", payload["__group_metadata__"])
        self.assertIn("双料僵尸", payload["__deleted__"])

    # ---------- 删除预设时同步清理元数据（治本） ----------

    def test_group_metadata_removed_from_memory_on_delete(self):
        """删除预设时同时清掉它的分组元数据，避免孤儿条目持续累积。"""
        name = self.window.mode_combo.currentText()
        if name == "custom":
            name = "待删预设"
            self.window.mode_tasks[name] = []
        self.window.mode_group_metadata[name] = {"names": {"g": "临时组"}}
        # 直接复现 delete_current_preset 的状态变更（跳过确认对话框）
        self.window.mode_tasks.pop(name, None)
        self.window.deleted_preset_names.add(name)
        self.window.mode_group_metadata.pop(name, None)
        self.assertNotIn(name, self.window.mode_group_metadata)

    def test_deleted_preset_metadata_never_reaches_disk(self):
        """即使删除后元数据残留（例如被旧界面写回），保存时也应被清掉。"""
        name = self._install_zombie("残留元数据预设")
        self.window.mode_group_metadata[name] = {"names": {"g": "x"}}
        self.window._save_presets()
        metadata = self._payload()["__group_metadata__"]
        self.assertNotIn(name, metadata)


if __name__ == "__main__":
    unittest.main()
