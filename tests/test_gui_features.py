"""gui_pyside6 四项移植能力的回归测试。

这四项能力原先只有 Tkinter 版 gui.py 具备，PySide6 版缺失：

  1. 嵌套分组树渲染（PySide6 版把组全部平铺在顶层，忽略 parents 元数据）
  2. 分组折叠状态持久化（PySide6 版只读 expanded，从不写入）
  3. 蓝图缩放比例恢复（PySide6 版只写入 layout_data["zoom"]，打开时从不应用）
  4. 跨预设复制选中项（PySide6 版实际是「整个预设另存为新名称」）

测试用临时目录隔离数据文件，不触碰真实预设。
"""

import copy
import json
import os
import tempfile
import unittest
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g


class GuiFeatureTests(unittest.TestCase):
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
        self.window = g.PySide6ScriptWindow()
        self.mode = self.window.mode_combo.currentText() or "custom"

    def tearDown(self):
        self.window.close()
        tasks.PRESETS_FILE = self._orig_presets
        tasks.TASKS_FILE = self._orig_tasks
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()

    # ---------- 辅助 ----------

    def _install_three_level_groups(self, collapse_child=False):
        """建立 外(gr) -> 中(gc) -> 内(gg) 三层分组，并给每组放步骤。"""
        self.window.mode_group_metadata[self.mode] = {
            "names": {"gr": "外层", "gc": "中层", "gg": "内层"},
            "colors": {"gr": "#eab308", "gc": "#06b6d4", "gg": "#16a34a"},
            "expanded": {"gc": False} if collapse_child else {},
            "order": ["gr", "gc", "gg"],
            "parents": {"gr": None, "gc": "gr", "gg": "gc"},
            "children": {"gr": ["gc"], "gc": ["gg"], "gg": []},
        }
        for index, task in enumerate(g.TASKS):
            if index == 0:
                task["group_id"] = "gg"
            elif index == 1:
                task["group_id"] = "gc"
            else:
                task["group_id"] = "gr"
        self.window.refresh_task_list()

    # ---------- 能力 1：嵌套分组树 ----------

    def test_nested_groups_are_rendered_as_hierarchy(self):
        self._install_three_level_groups()
        top_count = self.window.task_list.topLevelItemCount()
        self.assertEqual(top_count, 1, "三层嵌套只应有一个顶层项（根组）")

        root = self.window.task_list.topLevelItem(0)
        self.assertIn("外层", root.text(0))
        child = root.child(0)
        self.assertIn("中层", child.text(0))
        grandchild = child.child(0)
        self.assertIn("内层", grandchild.text(0))

    def test_group_items_carry_group_role(self):
        self._install_three_level_groups()
        root = self.window.task_list.topLevelItem(0)
        self.assertEqual(root.data(0, 256), "group", "组项应在 UserRole 标记为 group")

    def test_tasks_are_placed_under_their_own_group(self):
        self._install_three_level_groups()
        root = self.window.task_list.topLevelItem(0)
        child = root.child(0)
        grandchild = child.child(0)
        # 步骤 0 在内层组、步骤 1 在中层组、其余在外层组
        self.assertEqual(grandchild.childCount(), 1)
        self.assertEqual(grandchild.child(0).data(0, 256 + 1), 0)
        self.assertEqual(child.childCount() - 1, 1, "中层组除子组外应有 1 个步骤")
        self.assertEqual(root.childCount() - 1, len(g.TASKS) - 2)

    def test_non_nested_groups_stay_flat(self):
        """没有 parents 元数据时，各组仍应是顶层项（不破坏既有行为）。"""
        self.window.mode_group_metadata[self.mode] = {
            "names": {"ga": "甲", "gb": "乙"},
            "colors": {}, "expanded": {}, "order": ["ga", "gb"],
            "parents": {}, "children": {},
        }
        for index, task in enumerate(g.TASKS):
            task["group_id"] = "ga" if index % 2 == 0 else "gb"
        self.window.refresh_task_list()
        self.assertEqual(self.window.task_list.topLevelItemCount(), 2)

    # ---------- 能力 2：折叠状态持久化 ----------

    def test_collapsing_group_writes_metadata(self):
        self._install_three_level_groups()
        child = self.window.task_list.topLevelItem(0).child(0)
        child.setExpanded(False)
        expanded = self.window.mode_group_metadata[self.mode]["expanded"]
        self.assertIs(expanded.get("gc"), False, "折叠后应把 expanded[gc] 记为 False")

    def test_expanding_group_writes_metadata(self):
        self._install_three_level_groups(collapse_child=True)
        child = self.window.task_list.topLevelItem(0).child(0)
        self.assertFalse(child.isExpanded())
        child.setExpanded(True)
        expanded = self.window.mode_group_metadata[self.mode]["expanded"]
        self.assertIs(expanded.get("gc"), True)

    def test_collapse_state_persisted_to_disk(self):
        self._install_three_level_groups()
        self.window.task_list.topLevelItem(0).child(0).setExpanded(False)
        self.window.save_current_tasks()
        with open(tasks.PRESETS_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
        expanded = payload["__group_metadata__"][self.mode]["expanded"]
        self.assertIs(expanded.get("gc"), False, "折叠状态应落盘")

    def test_collapse_state_survives_rebuild(self):
        self._install_three_level_groups()
        self.window.task_list.topLevelItem(0).child(0).setExpanded(False)
        self.window.refresh_task_list()
        child = self.window.task_list.topLevelItem(0).child(0)
        self.assertFalse(child.isExpanded(), "重建后应保持折叠")
        root = self.window.task_list.topLevelItem(0)
        self.assertTrue(root.isExpanded(), "根组未折叠过，应保持展开")

    def test_task_item_expansion_is_ignored(self):
        """步骤项的展开/折叠不应污染组元数据。"""
        self._install_three_level_groups()
        root = self.window.task_list.topLevelItem(0)
        task_item = root.child(root.childCount() - 1)
        self.assertNotEqual(task_item.data(0, 256), "group")
        self.window._on_group_expanded_changed(task_item)
        self.assertEqual(self.window.mode_group_metadata[self.mode]["expanded"], {})

    # ---------- 能力 3：蓝图缩放恢复 ----------

    def test_zoom_is_restored_from_layout_data(self):
        window = g.BlueprintWindow(copy.deepcopy(g.TASKS), {"zoom": 1.8}, lambda *a: None, {}, self.window, {})
        self.assertAlmostEqual(window.view.transform().m11(), 1.8, places=3)
        window.close()

    def test_zoom_defaults_to_one_when_absent(self):
        window = g.BlueprintWindow(copy.deepcopy(g.TASKS), {}, lambda *a: None, {}, self.window, {})
        self.assertAlmostEqual(window.view.transform().m11(), 1.0, places=3)
        window.close()

    def test_invalid_zoom_is_ignored(self):
        for bad in (999, -1, 0, "abc", None):
            with self.subTest(zoom=bad):
                window = g.BlueprintWindow(
                    copy.deepcopy(g.TASKS), {"zoom": bad}, lambda *a: None, {}, self.window, {}
                )
                self.assertAlmostEqual(window.view.transform().m11(), 1.0, places=3)
                window.close()

    def test_zoom_change_is_recorded(self):
        window = g.BlueprintWindow(copy.deepcopy(g.TASKS), {}, lambda *a: None, {}, self.window, {})
        # 模拟滚轮缩放：先改变画布变换，再触发记录（与 _on_zoom_changed 的调用场景一致）
        window.view.scale(2.5, 2.5)
        window._on_zoom_changed(window.view.transform().m11())
        self.assertAlmostEqual(window.layout_data["zoom"], 2.5, places=3)

    def test_out_of_range_zoom_is_not_recorded(self):
        window = g.BlueprintWindow(copy.deepcopy(g.TASKS), {"zoom": 2.0}, lambda *a: None, {}, self.window, {})
        window._on_zoom_changed(999)          # 超出允许范围，应被忽略
        self.assertAlmostEqual(window.layout_data["zoom"], 2.0, places=3)
        window._on_zoom_changed(0.01)         # 过小，同样忽略
        self.assertAlmostEqual(window.layout_data["zoom"], 2.0, places=3)
        window._on_zoom_changed("abc")        # 非数字，忽略且不抛异常
        self.assertAlmostEqual(window.layout_data["zoom"], 2.0, places=3)
        window.close()

    # ---------- 能力 4：跨预设复制选中项 ----------

    def test_copy_single_tasks(self):
        copied = self.window._copy_selected_entries([("task", 0), ("task", 2)], [])
        self.assertEqual(len(copied), 2)

    def test_copied_tasks_get_new_ids(self):
        original_ids = {str(task.get("id")) for task in g.TASKS}
        copied = self.window._copy_selected_entries([("task", 0)], [])
        self.assertNotIn(copied[0]["id"], original_ids, "副本应使用新 id")

    def test_copy_group_includes_descendants(self):
        self._install_three_level_groups()
        copied = self.window._copy_selected_entries([("group", "gr")], [])
        self.assertEqual(len(copied), len(g.TASKS), "选中根组应连带全部子组内的步骤")

    def test_copied_group_is_renamed(self):
        self._install_three_level_groups()
        copied = self.window._copy_selected_entries([("group", "gr")], [])
        group_ids = {task.get("group_id") for task in copied}
        self.assertIn("gr_copy", group_ids, "被复制的组应改名为 <id>_copy")

    def test_copied_group_name_avoids_collision(self):
        """目标预设里已有 gr_copy 时，应继续加后缀。"""
        self._install_three_level_groups()
        target = [{"id": str(uuid.uuid4()), "group_id": "gr_copy"}]
        copied = self.window._copy_selected_entries([("group", "gr")], target)
        group_ids = {task.get("group_id") for task in copied}
        self.assertNotIn("gr_copy", group_ids)
        self.assertTrue(any(str(gid).startswith("gr_copy_") for gid in group_ids))

    def test_internal_flow_next_is_remapped(self):
        """副本内部保留原有先后连接关系。"""
        first_id = g.TASKS[0].get("id")
        g.TASKS[0]["flow_next"] = g.TASKS[1].get("id")
        copied = self.window._copy_selected_entries([("task", 0), ("task", 1)], [])
        self.assertEqual(copied[0]["flow_next"], copied[1]["id"])
        g.TASKS[0].pop("flow_next", None)
        self.assertIsNotNone(first_id)

    def test_flow_next_to_outside_task_is_left_alone(self):
        g.TASKS[0]["flow_next"] = g.TASKS[5].get("id")
        copied = self.window._copy_selected_entries([("task", 0)], [])
        self.assertEqual(copied[0]["flow_next"], g.TASKS[5].get("id"))
        g.TASKS[0].pop("flow_next", None)

    def test_copy_without_selection_returns_empty(self):
        self.assertEqual(self.window._copy_selected_entries([], []), [])

    def test_selected_copy_entries_reflects_selection(self):
        window = self.window
        window.task_list.clearSelection()
        for index in range(window.task_list.topLevelItemCount()):
            item = window.task_list.topLevelItem(index)
            if item.data(0, 256) == "group":
                item.setSelected(True)
        entries = window._selected_copy_entries()
        self.assertTrue(all(kind == "group" for kind, _ in entries))


    # ---------- 模板预览：构建期不显示 ----------

    def test_preview_is_hidden_during_window_construction(self):
        """启动时不应自动显示模板缩略图。

        refresh_task_list() 会自动选中第一个步骤并触发 _update_template_preview，
        若不设构建期保护，启动瞬间编辑面板就会显示第一张模板缩略图。
        """
        self.assertFalse(
            self.window.template_preview.isVisibleTo(self.window),
            "窗口刚构建完，模板预览不应可见",
        )

    def test_initializing_flag_is_cleared_after_construction(self):
        self.assertTrue(hasattr(self.window, "_initializing"))
        self.assertFalse(self.window._initializing, "构建结束后应清除构建期标志")

    def test_preview_displays_after_explicit_user_load(self):
        """构建结束后，加载步骤应正常显示预览。

        注意用 isVisibleTo() 而不是 isVisible()：后者要求控件及其所有祖先
        都实际显示在屏幕上，而测试里窗口并未 show()，会永远返回 False。
        isVisibleTo(parent) 反映的是控件自身的可见意图。
        """
        item = self.window._find_task_item(0)
        self.assertIsNotNone(item, "应能找到第一个步骤的树项")
        self.window.load_selected_task(item, None)
        self.assertTrue(
            self.window.template_preview.isVisibleTo(self.window),
            "用户选择步骤后应显示模板预览",
        )
        self.assertTrue(self.window.template_preview.toolTip(), "应显示模板名与尺寸")

    def test_preview_hidden_when_template_missing(self):
        item = self.window._find_task_item(0)
        self.window.load_selected_task(item, None)
        self.window._update_template_preview("绝不存在的模板名")
        self.assertFalse(self.window.template_preview.isVisibleTo(self.window))


    # ---------- 模板预览必须是「内嵌控件」，不能是独立窗口 ----------

    def test_preview_widget_is_embedded_not_a_window(self):
        """缩略图必须嵌在右侧编辑面板里，而不是浮在外面的独立窗口。

        主窗口的 _build_ui 曾经漏掉 editor_layout.addWidget(template_preview)，
        导致这个 QLabel 没有父控件，成为一个固定 180x110 的顶层窗口，
        显示时以「弹窗」形式飘在主窗口之外。
        """
        preview = self.window.template_preview
        self.assertFalse(
            preview.isWindow(),
            "模板预览不应是独立顶层窗口（说明它没有被加入布局）",
        )
        self.assertIsNotNone(
            preview.parentWidget(),
            "模板预览必须有父控件（说明它已被加入布局）",
        )

    def test_blueprint_preview_widget_is_also_embedded(self):
        bp = g.BlueprintWindow([], {}, lambda *a: None, {}, self.window, {})
        try:
            self.assertFalse(bp.template_preview.isWindow())
            self.assertIsNotNone(bp.template_preview.parentWidget())
        finally:
            bp.close()

    def test_preview_still_shows_after_being_embedded(self):
        """加入布局后，显示功能仍应正常。"""
        item = self.window._find_task_item(0)
        self.window.load_selected_task(item, None)
        self.assertTrue(self.window.template_preview.isVisibleTo(self.window))

    # ---------- 紧凑布局：缩略图在左，按钮在右 ----------

    def _action_buttons(self, window):
        from PySide6.QtWidgets import QPushButton
        return [b.text() for b in window.editor_actions.findChildren(QPushButton)]

    def test_action_buttons_present_in_main_window(self):
        buttons = self._action_buttons(self.window)
        for label in ("绑定图片", "记录点击点", "框选识别区域", "清空识别区域", "应用修改"):
            self.assertIn(label, buttons)

    def test_action_buttons_present_in_blueprint_window(self):
        bp = g.BlueprintWindow([], {}, lambda *a: None, {}, self.window, {})
        try:
            buttons = self._action_buttons(bp)
            for label in ("绑定图片", "记录点击点", "框选识别区域", "清空识别区域", "应用修改"):
                self.assertIn(label, buttons)
        finally:
            bp.close()

    def test_buttons_are_laid_out_in_a_grid(self):
        """按钮应排成网格（两列），而非整行铺开。"""
        from PySide6.QtWidgets import QGridLayout, QPushButton
        layout = self.window.editor_actions.layout()
        self.assertIsInstance(layout, QGridLayout, "操作按钮应使用网格布局以便放右侧")
        positions = {}
        for i in range(layout.count()):
            item = layout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, QPushButton):
                row, col, _rs, _cs = layout.getItemPosition(i)
                positions[widget.text()] = (row, col)
        # 前四个按钮两两一行，应用修改跨两列
        self.assertEqual(positions["绑定图片"], (0, 0))
        self.assertEqual(positions["记录点击点"], (0, 1))
        self.assertEqual(positions["框选识别区域"], (1, 0))
        self.assertEqual(positions["清空识别区域"], (1, 1))

    def test_apply_button_spans_both_columns(self):
        from PySide6.QtWidgets import QGridLayout, QPushButton
        layout = self.window.editor_actions.layout()
        for i in range(layout.count()):
            widget = layout.itemAt(i).widget()
            if isinstance(widget, QPushButton) and widget.text() == "应用修改":
                _row, _col, _rs, cs = layout.getItemPosition(i)
                self.assertEqual(cs, 2, "「应用修改」应跨两列")
                return
        self.fail("未找到「应用修改」按钮")

    def test_preview_and_actions_are_siblings_in_one_row(self):
        """缩略图与按钮容器应同属一个水平行布局（左右并排）。"""
        preview_parent = self.window.template_preview.parentWidget()
        actions_parent = self.window.editor_actions.parentWidget()
        self.assertIs(
            preview_parent, actions_parent,
            "缩略图与按钮容器应挂在同一个父控件下（同一行布局）",
        )

    def test_preview_height_matches_button_block(self):
        """缩略图高度应与其右侧按钮块对齐（避免被拉伸后图片偏小）。"""
        self.assertEqual(self.window.template_preview.maximumHeight(), 110)


class BlueprintGroupHeaderTests(unittest.TestCase):
    """蓝图组头显示的「组名 · N 步骤」。

    这段能力一度被项目接手文档误记为"渲染分支不可达、组头永远不显示 info"——
    实际构造点（refresh() 里建组框处）会传 info=f"{step_count} 步骤"，组头一直在
    显示它。真正没用的是 set_info() 这个 0 调用点的 setter，已删除。
    本测试锁住"能力确实在工作"与"setter 确实不在"两件事。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_presets_file = tasks.PRESETS_FILE
        self._orig_tasks_file = tasks.TASKS_FILE
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()

    def _tasks(self):
        return [
            {"id": "a", "type": "normal", "enabled": True, "description": "一",
             "template": "t", "group_id": "gr"},
            {"id": "b", "type": "normal", "enabled": True, "description": "二",
             "template": "t", "group_id": "gr"},
            {"id": "c", "type": "normal", "enabled": True, "description": "三",
             "template": "t"},
        ]

    def _metadata(self):
        return {
            "names": {"gr": "外层"},
            "colors": {"gr": "#eab308"},
            "expanded": {},
            "order": ["gr"],
            "parents": {"gr": None},
            "children": {"gr": []},
        }

    def _make(self, tasks=None, metadata=None):
        blueprint = g.BlueprintWindow(
            tasks if tasks is not None else self._tasks(),
            {},
            lambda *args, **kwargs: None,
            metadata if metadata is not None else self._metadata(),
            self.window,
            {},
        )
        blueprint.refresh()
        return blueprint

    def _boxes_by_group(self, blueprint):
        return {
            item.group_id: item
            for item in blueprint.scene.items()
            if isinstance(item, g.BlueprintGroupItem)
        }

    # ---------- 能力仍然在工作 ----------

    def test_group_header_carries_the_step_count(self):
        blueprint = self._make()
        try:
            boxes = self._boxes_by_group(blueprint)
            self.assertIn("gr", boxes, "组图元没有被创建")
            self.assertEqual(boxes["gr"]._name, "外层")
            self.assertEqual(boxes["gr"]._info, "2 步骤",
                             "组头应当带上该组的步骤数")
        finally:
            blueprint.close()

    def test_step_count_refreshes_after_adding_a_step(self):
        blueprint = self._make()
        try:
            blueprint.tasks.append({"id": "d", "type": "normal", "enabled": True,
                                    "description": "四", "template": "t", "group_id": "gr"})
            blueprint.refresh()
            self.assertEqual(self._boxes_by_group(blueprint)["gr"]._info, "3 步骤")
        finally:
            blueprint.close()

    def test_constructor_stores_the_info_argument(self):
        box = g.BlueprintGroupItem("g", (0, 0, 10, 10), [], name="组", info="5 步骤")
        self.assertEqual(box._name, "组")
        self.assertEqual(box._info, "5 步骤")

    def test_painting_a_group_header_with_info_succeeds(self):
        """真正走一遍 paint()：确认「组名 · N 步骤」那条分支能画出来。

        只断言 _info 的值会与实现耦合，所以这里用 scene.render 让 paint 真正执行
        （source 矩形取所有图元的包围盒，保证组头落在渲染范围内）。
        """
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter

        blueprint = self._make()
        try:
            source = blueprint.scene.itemsBoundingRect()
            self.assertFalse(source.isEmpty(), "场景里没有任何图元")
            image = QImage(240, 180, QImage.Format.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            try:
                blueprint.scene.render(painter, QRectF(0, 0, 240, 180), source)
            finally:
                painter.end()
        finally:
            blueprint.close()

    def test_info_text_actually_changes_what_is_painted(self):
        """对比 info 有值/无值两次渲染的像素。

        这条才真正验证"渲染分支可达且生效"：如果把 paint 里的
        `text = self._name if not self._info else f"{self._name} · {self._info}"`
        改成永远只画 `self._name`，两次渲染就会完全相同，本测试失败。
        （只断言 `_info` 的值是抓不到这种回归的。）
        """
        with_info = self._render_group("9 步骤")
        without_info = self._render_group("")
        self.assertNotEqual(
            bytes(with_info.constBits()), bytes(without_info.constBits()),
            "info 没有影响绘制结果 —— 组头的「组名 · N 步骤」渲染分支失效了",
        )

    def _render_group(self, info):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QGraphicsScene

        box = g.BlueprintGroupItem("g", (0, 0, 160, 40), [], name="外层", info=info)
        scene = QGraphicsScene()
        scene.addItem(box)
        image = QImage(200, 60, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        try:
            scene.render(painter, QRectF(0, 0, 200, 60), QRectF(0, 0, 160, 40))
        finally:
            painter.end()
        return image

    # ---------- setter 确实已删除 ----------

    def test_set_info_is_gone(self):
        self.assertFalse(
            hasattr(g.BlueprintGroupItem, "set_info"),
            "set_info 是 0 调用点的冗余 setter；改步骤数请走 refresh()（组图元会重建）",
        )


class PreviewSuppressionTests(unittest.TestCase):
    """构建期标志在未设置时不应误伤（蓝图窗口等其它 QWidget 子类）。"""

    def test_missing_flag_defaults_to_showing(self):
        self.assertFalse(getattr(g.CaptureOverlayMixin(), "_initializing", False))


if __name__ == "__main__":
    unittest.main()
