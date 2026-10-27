# -*- coding: utf-8 -*-
"""「类型 / ID」行：ID 统一不分段（2026-10-27）。

历史原因：新步骤 id 是 `str(uuid.uuid4())`（带横线），旧数据/导入的 id 可能是不带横线的短串，
界面上看起来"有的分段有的没分段"。统一为**显示时去掉横线**，存储里的原始 id 不动
（剪贴板/跳转/片段等引用仍使用原值）。
"""

import copy
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402
import tasks  # noqa: E402

_APP = QApplication.instance() or QApplication([])

HYPHEN_ID = "6c7a98a4-226a-4194-800a-9101431963c3"
PLAIN_ID = "fd4c8d82"


class TaskIdDisplayTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets, g.save_tasks = tasks.save_presets, tasks.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        g.TASKS[:] = [
            {"id": HYPHEN_ID, "type": "normal", "description": "带横线", "template": "1", "enabled": True},
            {"id": PLAIN_ID, "type": "normal", "description": "无横线", "template": "1", "enabled": True},
        ]
        self.window = g.PySide6ScriptWindow()
        self.window.show()
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        (tasks.PRESETS_FILE, tasks.TASKS_FILE, g.save_presets, g.save_tasks) = self._orig
        self._tmp.cleanup()

    def _select(self, index):
        self.window.task_list.clearSelection()
        item = self.window._find_task_item(index)
        item.setSelected(True)
        self.window.task_list.setCurrentItem(item)
        _APP.processEvents()

    def test_hyphenated_id_is_displayed_without_segments(self):
        self._select(0)
        text = self.window.selected_label.text()
        self.assertIn(HYPHEN_ID.replace("-", ""), text)
        self.assertNotIn("-", text, "显示用 ID 不应再有分段横线")

    def test_plain_id_is_unchanged(self):
        self._select(1)
        text = self.window.selected_label.text()
        self.assertIn(PLAIN_ID, text)
        self.assertNotIn("-", text)

    def test_stored_id_is_not_rewritten(self):
        self._select(0)
        self._select(1)
        self.assertEqual(g.TASKS[0]["id"], HYPHEN_ID, "存储的原始 ID 不能被显示逻辑改写")
        self.assertEqual(g.TASKS[1]["id"], PLAIN_ID)


class TaskIdDisplayGuardTests(unittest.TestCase):
    def test_display_uses_stripped_id(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn('display_id = str(task.get("id", "")).replace("-", "")', source)
        self.assertIn("ID: {display_id}", source)


if __name__ == "__main__":
    unittest.main()
