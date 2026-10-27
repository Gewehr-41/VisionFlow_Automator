# -*- coding: utf-8 -*-
"""蓝图：组内子组折叠后，父组/根组的框要跟着收紧（所有者 2026-10-27 反馈）。

问题：子组折叠后只显示一张 230×120 的群组节点卡片，但父组的包围盒仍按**隐藏节点**的
最小/最大坐标计算，于是父组（根组）保持原来散开的范围，空出一大片。

修复：`_group_box_rect()` 递归按"折叠后的实际呈现"计算——折叠组只占卡片大小，
展开组 = 自己直属节点范围 ∪ 各子组包围盒；`_render_groups` 与 `_sync_group_bounds` 共用。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_pyside6 as g  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _task(task_id, group_id=None):
    task = {"id": task_id, "type": "normal", "description": task_id, "enabled": True, "template": "1"}
    if group_id:
        task["group_id"] = group_id
        task["group_name"] = group_id
    return task


def make_window(nested=False):
    if nested:
        metadata = {
            "names": {"g1": "根组", "g2": "子组", "g3": "孙组"},
            "colors": {"g1": "#eab308", "g2": "#38bdf8", "g3": "#f472b6"},
            "parents": {"g1": None, "g2": "g1", "g3": "g2"},
            "children": {"g1": ["g2"], "g2": ["g3"], "g3": []},
            "order": ["g1"],
        }
        tasks = [_task("A", "g1"), _task("B", "g1"), _task("C", "g2"), _task("D", "g2"),
                 _task("E", "g3"), _task("F", "g3")]
        positions = {"0": [60, 120], "1": [340, 120], "2": [60, 400], "3": [340, 400],
                     "4": [60, 700], "5": [900, 1200]}
    else:
        metadata = {
            "names": {"g1": "父组", "g2": "子组"},
            "colors": {"g1": "#eab308", "g2": "#38bdf8"},
            "parents": {"g1": None, "g2": "g1"},
            "children": {"g1": ["g2"], "g2": []},
            "order": ["g1"],
        }
        tasks = [_task("A", "g1"), _task("B", "g1"), _task("C", "g2"), _task("D", "g2")]
        positions = {"0": [60, 120], "1": [340, 120], "2": [60, 600], "3": [900, 1000]}
    window = g.BlueprintWindow(tasks, {"positions": positions}, lambda *a, **k: None,
                               metadata, None, {})
    window.resize(1200, 800)
    window.show()
    QApplication.processEvents()
    return window


def rect_of(window, group_id):
    rect = window._group_items[group_id].rect()
    return rect.left(), rect.top(), rect.right(), rect.bottom()


class CollapsedChildShrinksParentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = make_window()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.window.deleteLater()
        QApplication.processEvents()

    def _refresh(self, collapsed):
        self.window.collapsed_groups = set(collapsed)
        self.window.refresh()
        _APP.processEvents()

    def test_parent_box_shrinks_to_collapsed_child_card(self):
        self._refresh({"g2"})
        parent = self.window._group_items["g1"].rect()
        card = self.window._group_items["g2"].rect()
        self.assertAlmostEqual(card.width(), 230, delta=1)
        self.assertAlmostEqual(card.height(), 120, delta=1)
        # 隐藏节点最下沿是 1000+120+18=1138；折叠后父组底边应该只到卡片底部附近
        self.assertLess(parent.bottom(), 800,
                        f"父组框底边 {parent.bottom():.0f} 仍被隐藏子组节点撑开（空出一大片）")
        self.assertAlmostEqual(parent.bottom(), card.bottom(), delta=1,
                               msg="父组底边应贴合折叠子组卡片底边")

    def test_parent_box_left_right_ignore_hidden_child_spread(self):
        self._refresh({"g2"})
        parent = self.window._group_items["g1"].rect()
        self.assertLess(parent.right(), 800,
                        f"父组框右边 {parent.right():.0f} 仍被隐藏子组节点(900)撑开")

    def test_expanded_child_keeps_full_extent(self):
        self._refresh(set())
        parent = self.window._group_items["g1"].rect()
        self.assertGreater(parent.bottom(), 1100, "子组展开时父组应包含全部子组节点")
        self.assertGreater(parent.right(), 1000)

    def test_collapsed_outer_group_is_card_size(self):
        self._refresh({"g1", "g2"})
        card = self.window._group_items["g1"].rect()
        self.assertAlmostEqual(card.width(), 230, delta=1)
        self.assertAlmostEqual(card.height(), 120, delta=1)

    def test_sync_bounds_after_move_uses_collapsed_layout(self):
        self._refresh({"g2"})
        before = self.window._group_items["g1"].rect()
        self.window._sync_group_bounds()
        after = self.window._group_items["g1"].rect()
        self.assertAlmostEqual(before.bottom(), after.bottom(), delta=1,
                               msg="拖动/同步后的父组框也应按折叠卡片计算，不能回弹成一大片")
        self.assertLess(after.bottom(), 800)


class NestedCollapseExtentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = make_window(nested=True)

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.window.deleteLater()
        QApplication.processEvents()

    def test_grandchild_collapse_shrinks_child_then_root(self):
        self.window.collapsed_groups = {"g3"}
        self.window.refresh()
        _APP.processEvents()
        child = self.window._group_items["g2"].rect()
        root = self.window._group_items["g1"].rect()
        self.assertLess(child.bottom(), 800, "子组框不应被孙组隐藏节点(1200)撑开")
        self.assertLess(root.bottom(), 800, "根组框也不应被孙组隐藏节点撑开")

    def test_collapsing_child_too_shrinks_root_further(self):
        self.window.collapsed_groups = {"g3"}
        self.window.refresh()
        _APP.processEvents()
        root_before = self.window._group_items["g1"].rect().bottom()
        self.window.collapsed_groups = {"g2", "g3"}
        self.window.refresh()
        _APP.processEvents()
        root_after = self.window._group_items["g1"].rect().bottom()
        self.assertLessEqual(root_after, root_before + 1)

    def test_focus_view_also_uses_collapsed_extent(self):
        self.window.collapsed_groups = {"g3"}
        self.window.focus_group = "g2"
        self.window.refresh()
        _APP.processEvents()
        child = self.window._group_items["g2"].rect()
        self.assertLess(child.bottom(), 800, "聚焦视图里子组框也应按折叠孙组计算")


class GroupExtentGuardTests(unittest.TestCase):
    def test_render_and_sync_share_group_box_rect(self):
        source = open("blueprint_groups.py", encoding="utf-8").read()
        self.assertIn("def _group_box_rect(self, group_id):", source)
        self.assertIn("return self._group_box_rect(group_id)", source)
        self.assertIn("bounds = self._group_box_rect(group_id)", source)

    def test_collapsed_card_uses_fixed_size_in_box_rect(self):
        source = open("blueprint_groups.py", encoding="utf-8").read()
        self.assertIn("BlueprintGroupItem.COLLAPSED_WIDTH", source)
        self.assertIn("BlueprintGroupItem.COLLAPSED_HEIGHT", source)


if __name__ == "__main__":
    unittest.main()
