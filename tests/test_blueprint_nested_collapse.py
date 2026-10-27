# -*- coding: utf-8 -*-
"""蓝图：折叠组与嵌套子组的连线/聚焦逻辑（所有者 2026-10-27 反馈）。

问题：
  1. 折叠外层组后，组内子组虽然不画，但**子组相关连线**还在——端点退回到隐藏节点坐标；
  2. 进入折叠组内部（子图视图）看不到组内的组。

修复（本文件直接测两个纯函数 + `_node_endpoint` 的端点决策，不创建 Qt 场景，
避免全量测试时 QGraphicsScene 与打包子进程用例叠加触发 fast-fail）：
  · `find_rendered_collapsed_box`：隐藏节点的连线只路由到**实际渲染出来**的折叠组框；
  · `group_inside_focus`：聚焦在折叠组内部时，其子组框照常渲染。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF  # noqa: E402

from blueprint_groups import group_inside_focus  # noqa: E402
from blueprint_interaction import BlueprintInteractionMixin, find_rendered_collapsed_box  # noqa: E402


class FakeBox:
    """假的折叠组框（只提供 sceneBoundingRect）。"""

    def __init__(self, left=0.0, top=0.0, width=230.0, height=120.0):
        self._rect = QRectF(left, top, width, height)

    def sceneBoundingRect(self):
        return self._rect


class FakeNode:
    def __init__(self, x=60.0, y=120.0, width=230.0, height=120.0, visible=False):
        self._x, self._y = x, y
        self._rect = QRectF(0, 0, width, height)
        self._visible = visible

    def isVisible(self):
        return self._visible

    def x(self):
        return self._x

    def y(self):
        return self._y

    def rect(self):
        return self._rect

    def port_for_name(self, _name):
        return None

    def port_y(self, _offset):
        return self._rect.height() / 2


def _task(task_id, group_id):
    return {"id": task_id, "type": "normal", "description": task_id, "enabled": True,
            "template": "1", "group_id": group_id}


PARENTS = {"g1": None, "g2": "g1", "g3": None}


class RenderedCollapsedBoxTests(unittest.TestCase):
    """隐藏节点 → 端点只认已渲染的折叠组框。"""

    def setUp(self):
        self.tasks = [_task("A", "g1"), _task("B", "g2"), _task("C", "g3")]

    def test_nested_collapsed_without_child_box_falls_back_to_outer_box(self):
        # 外层与子组都折叠；子组框因为祖先折叠没有渲染 → 应落到外层框
        outer = FakeBox()
        boxes = {"g1": outer}
        box = find_rendered_collapsed_box(self.tasks, 1, {"g1", "g2"}, boxes, PARENTS)
        self.assertIs(box, outer, "不能返回不存在的子组框（那样会退回到隐藏节点坐标）")

    def test_child_box_preferred_when_rendered(self):
        outer, child = FakeBox(), FakeBox()
        boxes = {"g1": outer, "g2": child}
        box = find_rendered_collapsed_box(self.tasks, 1, {"g1", "g2"}, boxes, PARENTS)
        self.assertIs(box, child, "子组框确实渲染出来时应优先用它")

    def test_no_collapsed_group_returns_none(self):
        self.assertIsNone(find_rendered_collapsed_box(self.tasks, 0, set(), {"g1": FakeBox()}, PARENTS))

    def test_root_group_returns_none(self):
        tasks = [_task("D", None)]
        self.assertIsNone(find_rendered_collapsed_box(tasks, 0, {"group_default"},
                                                      {"group_default": FakeBox()}, PARENTS),
                          "group_default 是根层，不折叠成框")

    def test_index_out_of_range_is_safe(self):
        self.assertIsNone(find_rendered_collapsed_box(self.tasks, 99, {"g1"}, {"g1": FakeBox()}, PARENTS))


class FocusAwareEndpointTests(unittest.TestCase):
    """聚焦（进入子图）后，组内节点可见，端点不能再被当成"折叠进卡片"。"""

    def setUp(self):
        self.tasks = [_task("A", "g1"), _task("B", "g2"), _task("C", "g3")]

    def test_visible_nodes_inside_collapsed_focus_use_own_endpoint(self):
        box = FakeBox()
        result = find_rendered_collapsed_box(
            self.tasks, 0, {"g1"}, {"g1": box}, PARENTS, focus_group="g1")
        self.assertIsNone(result, "进入折叠组内部后，组内节点可见，不应再路由到本组卡片"
                                 "（否则内部连线会被整条跳过）")

    def test_descendant_of_focus_also_not_collapsed_to_card(self):
        result = find_rendered_collapsed_box(
            self.tasks, 1, {"g1"}, {"g1": FakeBox()}, PARENTS, focus_group="g1")
        self.assertIsNone(result, "聚焦父组时，子组链路上的节点也不该被折叠进父卡片")

    def test_collapsed_child_inside_focus_still_uses_child_box(self):
        outer, child = FakeBox(), FakeBox()
        result = find_rendered_collapsed_box(
            self.tasks, 1, {"g1", "g2"}, {"g1": outer, "g2": child}, PARENTS, focus_group="g1")
        self.assertIs(child, result, "聚焦内部时，真正的折叠子组仍应显示成子组卡片")

    def test_focus_outside_does_not_affect_collapsed_box(self):
        box = FakeBox()
        result = find_rendered_collapsed_box(
            self.tasks, 0, {"g1"}, {"g1": box}, PARENTS, focus_group="g3")
        self.assertIs(box, result, "聚焦别的组时，本组仍按折叠卡片路由")

    def test_focus_host_endpoint_uses_visible_port(self):
        class Host(BlueprintInteractionMixin):
            def __init__(self):
                self.tasks = [_task("A", "g1"), _task("B", "g2")]
                self.collapsed_groups = {"g1", "g2"}
                self.focus_group = "g1"
                self._node_items = {0: FakeNode(x=40.0, y=90.0, visible=True)}
                self._group_items = {"g1": FakeBox(), "g2": FakeBox()}

            def _group_parents(self):
                return PARENTS

        host = Host()
        point = host._node_endpoint(0, "input")
        self.assertAlmostEqual(point.x(), 40.0, msg="聚焦内可见节点应使用自己的端口")
        self.assertAlmostEqual(point.y(), 90.0 + 60.0)


class HiddenNodeEndpointTests(unittest.TestCase):
    """`_node_endpoint` 在节点隐藏时的端点选择（回归：不能落在隐藏节点坐标）。"""

    def _host(self, boxes, collapsed, hidden_index=1):
        class Host(BlueprintInteractionMixin):
            def __init__(self):
                self.tasks = [_task("A", "g1"), _task("B", "g2"), _task("C", "g3")]
                self.collapsed_groups = set(collapsed)
                self._group_items = dict(boxes)
                self._node_items = {hidden_index: FakeNode(x=80.0, y=320.0, visible=False)}

            def _group_parents(self):
                return PARENTS

        return Host()

    def test_hidden_node_endpoint_uses_rendered_outer_box(self):
        outer = FakeBox(left=180.0, top=300.0)
        host = self._host({"g1": outer}, {"g1", "g2"})
        point = host._node_endpoint(1, "output", "output")
        rect = outer.sceneBoundingRect()
        self.assertAlmostEqual(point.x(), rect.right(), delta=0.5,
                               msg="隐藏节点连线应接到已渲染的外层群组节点，而不是隐藏节点坐标")
        self.assertAlmostEqual(point.y(), rect.center().y(), delta=0.5)

    def test_nested_collapsed_uses_nearest_rendered_box(self):
        outer, child = FakeBox(left=0, top=0), FakeBox(left=400, top=0)
        host = self._host({"g1": outer, "g2": child}, {"g1", "g2"})
        point = host._node_endpoint(1, "input")
        self.assertAlmostEqual(point.x(), child.sceneBoundingRect().left(), delta=0.5)

    def test_visible_node_uses_its_own_port(self):
        class Host(BlueprintInteractionMixin):
            def __init__(self):
                self.tasks = [_task("A", "g1")]
                self.collapsed_groups = set()
                self._group_items = {}
                self._node_items = {0: FakeNode(x=100.0, y=50.0, visible=True)}

            def _group_parents(self):
                return PARENTS

        host = Host()
        point = host._node_endpoint(0, "input")
        self.assertAlmostEqual(point.x(), 100.0)
        self.assertAlmostEqual(point.y(), 50.0 + 60.0)


class FocusInsideCollapsedGroupTests(unittest.TestCase):
    """聚焦在折叠组内部时，其子组要参与渲染。"""

    def test_focus_group_itself_is_inside(self):
        self.assertTrue(group_inside_focus("g1", "g1", PARENTS))

    def test_descendant_is_inside(self):
        self.assertTrue(group_inside_focus("g2", "g1", PARENTS),
                        "聚焦外层时子组必须算作子图内部（否则子组框不渲染）")

    def test_sibling_group_is_outside(self):
        self.assertFalse(group_inside_focus("g3", "g1", PARENTS))

    def test_no_focus_means_outside(self):
        self.assertFalse(group_inside_focus("g2", None, PARENTS))

    def test_cycle_does_not_hang(self):
        parents = {"g1": "g2", "g2": "g1"}
        self.assertFalse(group_inside_focus("g1", "g9", parents))


class RenderUsesPureHelpersTests(unittest.TestCase):
    """静态守卫：渲染/端点代码必须走这两个纯函数（防止以后被改回旧逻辑）。"""

    def test_render_groups_uses_inside_focus(self):
        source = open("blueprint_groups.py", encoding="utf-8").read()
        self.assertIn("group_inside_focus(group_id, focus, parents_map)", source)
        self.assertIn("has_collapsed_ancestor(group_id) and not inside_focus(group_id)", source)

    def test_node_endpoint_uses_rendered_box(self):
        source = open("blueprint_interaction.py", encoding="utf-8").read()
        self.assertIn("self._collapsed_box(index)", source)
        self.assertIn("find_rendered_collapsed_box(", source)

    def test_collapsed_box_passes_focus_so_focused_subgraph_keeps_wires(self):
        source = open("blueprint_interaction.py", encoding="utf-8").read()
        self.assertIn('getattr(self, "focus_group", None)', source,
                      "端点解析必须把 focus_group 传进去，否则进入折叠组后内部连线全断")

    def test_connections_skip_same_rendered_box(self):
        source = open("gui_pyside6.py", encoding="utf-8").read()
        self.assertIn("source_box = self._collapsed_box(source_index)", source)
        self.assertIn("if source_box is not None and source_box is target_box:", source)


if __name__ == "__main__":
    unittest.main()
