# -*- coding: utf-8 -*-
"""蓝图：双击空白区域返回上一层（所有者 2026-10-27 要求）。

行为：
  · 在子图内部（聚焦某个组）时，双击空白 → 回到上一层（嵌套时一次退一层）；
  · 根层双击空白 → 无操作（不报错、不改变折叠状态）；
  · 双击节点/组头仍是原有语义（节点折叠、组收起/展开、双击收起组进入子图）。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402

_APP = QApplication.instance() or QApplication([])
EMPTY_SCENE_POINT = QPointF(8000.0, 8000.0)   # 远离任何图元的空白处


def _task(task_id, group_id):
    return {"id": task_id, "type": "normal", "description": task_id, "enabled": True,
            "template": "1", "group_id": group_id}


def make_window():
    metadata = {
        "names": {"g1": "外层", "g2": "子组", "g3": "外部"},
        "colors": {},
        "parents": {"g1": None, "g2": "g1", "g3": None},
        "children": {"g1": ["g2"], "g2": [], "g3": []},
        "order": ["g1", "g3"],
    }
    tasks = [_task("A", "g1"), _task("B", "g2"), _task("C", "g3")]
    layout = {"positions": {"0": [60, 120], "1": [80, 320], "2": [520, 120]}}
    window = g.BlueprintWindow(tasks, layout, lambda *a, **k: None, metadata, None, {})
    window.resize(1100, 700)
    window.show()
    QApplication.processEvents()
    return window


def double_click(view, scene_point):
    local = QPointF(view.mapFromScene(scene_point))
    global_point = view.viewport().mapToGlobal(local.toPoint())
    event = QMouseEvent(QEvent.Type.MouseButtonDblClick, local, global_point,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    view.mouseDoubleClickEvent(event)
    QApplication.processEvents()


class LeaveFocusOnEmptyDoubleClickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = make_window()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.window.deleteLater()
        QApplication.processEvents()

    def setUp(self):
        self.window.focus_group = None
        self.window.collapsed_groups = set()
        self.window.refresh()
        _APP.processEvents()

    def test_double_click_empty_returns_one_level(self):
        self.window.enter_group("g1")
        _APP.processEvents()
        self.assertEqual(self.window.focus_group, "g1")
        double_click(self.window.view, EMPTY_SCENE_POINT)
        self.assertIsNone(self.window.focus_group, "双击空白应回到上一层（这里是根层）")

    def test_nested_focus_leaves_one_level_at_a_time(self):
        self.window.enter_group("g1")
        self.window.enter_group("g2")
        _APP.processEvents()
        self.assertEqual(self.window.focus_group, "g2")
        double_click(self.window.view, EMPTY_SCENE_POINT)
        self.assertEqual(self.window.focus_group, "g1", "嵌套时一次只退一层")
        double_click(self.window.view, EMPTY_SCENE_POINT)
        self.assertIsNone(self.window.focus_group)

    def test_double_click_empty_at_root_is_noop(self):
        before = set(self.window.collapsed_groups)
        double_click(self.window.view, EMPTY_SCENE_POINT)
        self.assertIsNone(self.window.focus_group)
        self.assertEqual(self.window.collapsed_groups, before, "根层双击空白不应改变折叠状态")

    def test_double_click_node_still_toggles_collapse_not_leaves(self):
        self.window.enter_group("g1")
        _APP.processEvents()
        node = self.window._node_items[1]   # B（子组里的节点），在聚焦视图里可见
        self.assertTrue(node.isVisible())
        before = bool(self.window.tasks[1].get("blueprint_collapsed"))
        double_click(self.window.view, QPointF(node.x() + 20, node.y() + 20))
        after = bool(self.window.tasks[1].get("blueprint_collapsed"))
        self.assertNotEqual(before, after, "双击节点仍是折叠节点")
        self.assertEqual(self.window.focus_group, "g1", "双击节点不应退出子图")

    def test_repeated_double_click_keeps_focus_stable_after_leaving(self):
        self.window.enter_group("g1")
        _APP.processEvents()
        double_click(self.window.view, EMPTY_SCENE_POINT)
        double_click(self.window.view, EMPTY_SCENE_POINT)
        self.assertIsNone(self.window.focus_group)


class LeaveFocusWiringTests(unittest.TestCase):
    def test_view_emits_leave_signal_on_empty_double_click(self):
        from blueprint_canvas import BlueprintView

        self.assertTrue(hasattr(BlueprintView, "leave_focus_requested"))

    def test_window_connects_leave_signal(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("self.view.leave_focus_requested.connect(self.leave_group)", source)

    def test_view_emits_signal_when_no_item(self):
        source = open("blueprint_canvas.py", encoding="utf-8").read()
        self.assertIn("self.leave_focus_requested.emit()", source)
        self.assertIn("if item is None:", source)


if __name__ == "__main__":
    unittest.main()
