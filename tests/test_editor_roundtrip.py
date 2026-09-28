"""编辑器「加载 → 改值 → 应用」往返测试，外加装饰器静态守卫。

背景
----
commit 36da50b 在删除已退休的 `open_detour_editor` 时，误把紧随其后的
`PySide6ScriptWindow._float_value` 的 `@staticmethod` 一并删掉了。于是那行
残留的 `@Slot()` 变成了 `_float_value` 的装饰器，它的第一个参数从
`self` 变成了 `value`。后果是主窗口「应用修改」按钮一点就崩溃：

    TypeError: PySide6ScriptWindow._float_value() takes 2 positional
               arguments but 3 were given

（该别名后来与蓝图窗口的 `_float` / `_int` 合并，统一由 `CaptureOverlayMixin`
提供，名字为 `_float` / `_int`；下面两条测试跟着改名。）

而当时 131 个测试全绿——因为没有任何一个测试真正点过「应用修改」。
本文件补上两件事：

  1. StaticMethodGuardTests
     AST 静态守卫：任何「首参不是 self/cls，却既没有 @staticmethod 也没有
     @classmethod」的方法都会被抓出来。这是这类 bug 的通杀检查，不需要
     真的点到那个按钮。
  2. EditorRoundTripTests
     真正调用 `apply_selected_task()` / `_apply_editor()`，把
     加载 → 改值 → 应用 的完整链路跑一遍。

测试用临时目录隔离数据文件，并在结束后还原 `g.TASKS`，不触碰真实预设。
"""

import ast
import copy
import os
import pathlib
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g

_GUI_SOURCE = pathlib.Path(g.__file__).resolve()


def _iter_class_methods():
    """产出 (类名, 方法名, 函数节点, 装饰器名集合)。"""
    tree = ast.parse(_GUI_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = set()
            for deco in item.decorator_list:
                if isinstance(deco, ast.Name):
                    decorators.add(deco.id)
                elif isinstance(deco, ast.Attribute):
                    decorators.add(deco.attr)
            yield node.name, item.name, item, decorators


class StaticMethodGuardTests(unittest.TestCase):
    """静态守卫：不允许出现「丢掉 @staticmethod 的辅助方法」。"""

    def test_every_method_takes_self_or_cls(self):
        offenders = []
        for class_name, func_name, node, decorators in _iter_class_methods():
            if decorators & {"staticmethod", "classmethod"}:
                continue
            args = node.args.posonlyargs + node.args.args
            if not args:
                offenders.append(f"{class_name}.{func_name} 一个参数都没有")
            elif args[0].arg not in ("self", "cls"):
                offenders.append(
                    f"{class_name}.{func_name} 首参是 {args[0].arg!r}，"
                    f"却缺少 @staticmethod/@classmethod"
                )
        self.assertEqual(
            offenders, [],
            "以下方法会以 `self.xxx()` 形式被调用，但首参不是 self/cls；"
            "这类方法必须加 @staticmethod：\n  " + "\n  ".join(offenders),
        )

    def test_value_helpers_are_staticmethods(self):
        # 两窗口共用 mixin 里的 _float / _int（主窗口原来的 _float_value / _int_value
        # 是同实现的重复别名，已删除）
        wanted = {"_float", "_int"}
        found = {}
        for class_name, func_name, _node, decorators in _iter_class_methods():
            if func_name in wanted:
                found[(class_name, func_name)] = decorators
        self.assertTrue(found, "没有找到任何数值解析辅助方法，测试自身已失效")
        for (class_name, func_name), decorators in found.items():
            self.assertIn(
                "staticmethod", decorators,
                f"{class_name}.{func_name} 必须是 @staticmethod",
            )
            self.assertNotIn(
                "Slot", decorators,
                f"{class_name}.{func_name} 是内部辅助方法，不应被标成 Qt 槽",
            )

    def test_value_helpers_callable_through_instance(self):
        """这条正是当初崩溃的调用形式：实例属性访问 + 两个实参。"""
        window = g.PySide6ScriptWindow()
        try:
            self.assertEqual(window._float("1.5", 0.0), 1.5)
            self.assertEqual(window._float("不是数字", 9.0), 9.0)
            self.assertEqual(window._int("7"), 7)
            self.assertIsNone(window._int("x"))
        finally:
            window.close()

    def test_blueprint_value_helpers_callable_through_instance(self):
        blueprint = g.BlueprintWindow([{"type": "normal"}], {}, lambda *a: None, {}, None, {})
        try:
            self.assertEqual(blueprint._float("2.5", 0.0), 2.5)
            self.assertEqual(blueprint._int("3"), 3)
        finally:
            blueprint.close()


class EditorRoundTripTests(unittest.TestCase):
    """加载 → 改值 → 应用 的完整往返（会真正调用应用按钮的处理函数）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_presets_file = tasks.PRESETS_FILE
        self._orig_tasks_file = tasks.TASKS_FILE
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()

    # ---------- 主窗口 ----------

    def _load_first_task(self):
        self.assertTrue(g.TASKS, "预设为空，无法测试编辑器往返")
        task = g.TASKS[0]
        task["type"] = "normal"
        item = self.window._find_task_item(0)
        self.assertIsNotNone(item, "找不到第 1 步对应的树项")
        self.window.load_selected_task(item, None)
        return task

    def test_apply_writes_paired_click(self):
        task = self._load_first_task()
        task["click_x"], task["click_y"] = 333, 444
        task["click_position"] = (333, 444)
        self.window.load_selected_task(self.window._find_task_item(0), None)

        # 显示格式走统一格式化，这里按数值比对而不是比对字面量
        self.assertEqual(g._to_int(self.window.click_x_edit.text()), 333)
        self.assertEqual(g._to_int(self.window.click_y_edit.text()), 444)

        self.window.click_x_edit.setText("99")
        self.window.click_y_edit.setText("100")
        self.window.apply_selected_task()

        self.assertEqual(task["click_position"], (99, 100))

    def test_offset_is_no_longer_edited_by_the_panel(self):
        """「偏移」一行已按所有者要求从界面删除（2026-09-28）。

        控件不存在，加载与回写也一并删掉：任务里的 offset 值**原样保留**，
        「应用修改」不再碰它（引擎仍按缺省 (0, 0) 使用）。
        """
        task = self._load_first_task()
        task["offset_x"], task["offset_y"] = 11.0, -22.0
        task["offset"] = (11.0, -22.0)
        self.window.load_selected_task(self.window._find_task_item(0), None)

        self.assertFalse(hasattr(self.window, "offset_x_edit"), "偏移输入框应已删除")
        self.assertFalse(hasattr(self.window, "offset_y_edit"), "偏移输入框应已删除")

        self.window.description_edit.setText("改个描述")
        self.window.apply_selected_task()

        self.assertEqual(task["offset"], (11.0, -22.0), "「应用修改」不该改动 offset")
        self.assertEqual(task["offset_x"], 11.0)
        self.assertEqual(task["offset_y"], -22.0)
        self.assertEqual(task["description"], "改个描述")

    def test_apply_clears_click_when_both_boxes_empty(self):
        task = self._load_first_task()
        task["click_x"], task["click_y"] = 333, 444
        task["click_position"] = (333, 444)
        self.window.load_selected_task(self.window._find_task_item(0), None)

        self.window.click_x_edit.setText("")
        self.window.click_y_edit.setText("")
        self.window.apply_selected_task()

        self.assertNotIn("click_position", task)
        self.assertNotIn("click_x", task)
        self.assertNotIn("click_y", task)

    def test_load_shows_wait_for_and_next_template(self):
        """「下一模板」与「等待方式」必须从任务加载到控件。

        回归：`apply_selected_task` 会把这两个控件的内容写回任务，而
        `load_selected_task` 此前从不加载它们 —— 选中步骤后**不改任何控件**直接点
        「应用修改」，`wait_for` 就会被改成默认的 `time`、`next_template` 会被删掉。
        真实数据里 8 个步骤的 `wait_for≠time`、13 个步骤带真实 `next_template`。
        """
        task = self._load_first_task()
        task["wait_for"] = "next_appear"
        task["next_template"] = "next_probe"
        self.window.load_selected_task(self.window._find_task_item(0), None)

        self.assertEqual(self.window.next_template_edit.text(), "next_probe",
                         "「下一模板」输入框应显示已存值")
        self.assertEqual(self.window.wait_for_combo.currentIndex(), 1,
                         "「等待方式」下拉框应选中「等待目标模板出现」")
        self.assertIn("等待目标模板出现", self.window.wait_for_combo.currentText())

    def test_load_maps_every_wait_mode_to_its_combo_item(self):
        task = self._load_first_task()
        for wait_for, expected_index in (("time", 0), ("next_appear", 1), ("change_then_appear", 2)):
            with self.subTest(wait_for=wait_for):
                task["wait_for"] = wait_for
                self.window.load_selected_task(self.window._find_task_item(0), None)
                self.assertEqual(self.window.wait_for_combo.currentIndex(), expected_index)

    def test_apply_without_edits_keeps_wait_for_and_next_template(self):
        """加载后不动任何控件直接「应用修改」，这两个字段必须原样保留。"""
        task = self._load_first_task()
        task["wait_for"] = "next_appear"
        task["next_template"] = "next_probe"
        task["next_templates"] = ["next_probe"]
        self.window.load_selected_task(self.window._find_task_item(0), None)

        self.window.apply_selected_task()

        self.assertEqual(task["wait_for"], "next_appear", "未编辑的等待方式被改写了")
        self.assertEqual(task["next_template"], "next_probe", "未编辑的下一模板被删掉了")

    def test_apply_does_not_touch_the_recognition_regions(self):
        """识别区域不再由「应用修改」写回。

        区域**只能靠框选产生**（`finish_region_capture` 直接写任务并立即落盘），
        界面也不再提供手工输入坐标的控件。所以「应用修改」必须让区域字段原封不动
        —— 此前它写死 `match_rects = [match_rect]`，会把多条区域压成 1 条。
        """
        task = self._load_first_task()
        task["match_rects"] = [(10, 20, 30, 40), (50, 60, 70, 80)]
        task["match_rect"] = (10, 20, 30, 40)
        task["search_rect"] = (10, 20, 30, 40)
        self.window.load_selected_task(self.window._find_task_item(0), None)

        self.window.description_edit.setText("改个描述")
        self.window.apply_selected_task()

        self.assertEqual(task["match_rects"], [(10, 20, 30, 40), (50, 60, 70, 80)],
                         "「应用修改」不该改动识别区域")
        self.assertEqual(task["search_rect"], (10, 20, 30, 40))
        self.assertEqual(task["description"], "改个描述")


    def test_main_window_has_no_hidden_match_rect_widget(self):
        """主窗口不该再有那个从未进布局的隐形 match_rect_edit。"""
        self.assertFalse(hasattr(self.window, "match_rect_edit"))

    def test_apply_writes_description_and_threshold(self):
        task = self._load_first_task()
        self.window.description_edit.setText("往返测试步骤")
        self.window.threshold_edit.setText("0.87")
        self.window.apply_selected_task()

        self.assertEqual(task["description"], "往返测试步骤")
        self.assertEqual(task["threshold"], 0.87)

    # ---------- 蓝图窗口 ----------

    def test_blueprint_apply_editor_leaves_offset_alone(self):
        """蓝图窗口的「偏移」一行也删了（2026-09-28），回写逻辑随之移除。

        这条同时是原来那个"曾经在这里崩溃"的回归测试的替代：`_apply_editor`
        仍必须能完整跑完并只写它真正拥有的字段。
        """
        own_task = {
            "type": "normal",
            "template": "new_step",
            "description": "蓝图往返",
            "offset_x": 5.0,
            "offset_y": 6.0,
            "offset": (5.0, 6.0),
        }
        own_tasks = [own_task]
        # save_callback 传空实现，确保 save_layout() 不会写真实布局文件
        blueprint = g.BlueprintWindow(own_tasks, {}, lambda *a: None, {}, None, {})
        try:
            blueprint.current_index = 0
            blueprint._load_editor_for_task(own_task)
            self.assertFalse(hasattr(blueprint, "offset_x_edit"), "蓝图窗口的偏移输入框应已删除")
            self.assertFalse(hasattr(blueprint, "offset_y_edit"), "蓝图窗口的偏移输入框应已删除")
            blueprint.description_edit.setText("蓝图往返已改")
            blueprint._apply_editor()  # 曾经在这里崩溃
        finally:
            blueprint.close()

        self.assertEqual(own_task["offset"], (5.0, 6.0), "_apply_editor 不该改动 offset")
        self.assertEqual(own_task["offset_x"], 5.0)
        self.assertEqual(own_task["offset_y"], 6.0)
        self.assertEqual(own_task["description"], "蓝图往返已改")

    def test_blueprint_apply_editor_round_trips_special_fields(self):
        own_task = {"type": "key_press", "key": "W"}
        default_task = {"type": "normal"}
        blueprint = g.BlueprintWindow([own_task, default_task], {}, lambda *a: None, {}, None, {})
        try:
            blueprint.current_index = 0
            blueprint._load_editor_for_task(own_task)
            blueprint._apply_editor()
        finally:
            blueprint.close()

        self.assertEqual(own_task.get("type"), "key_press")


class BlueprintGraphWiringTests(unittest.TestCase):
    """验证 open_blueprint() 真的把「补全后的任务列表」交给蓝图窗口。

    这里用桩替换 `g.BlueprintWindow`，不构造真实窗口：真窗口的 closeEvent 会
    调用 save_layout() -> save_blueprint_state()，从而写真实的
    saved_blueprint_layouts.json / saved_blueprint_graphs.json。用桩可以彻底
    避开这条写盘路径，测试只关心接线本身。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_presets_file = tasks.PRESETS_FILE
        self._orig_tasks_file = tasks.TASKS_FILE
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        self._orig_save_layouts = g.save_blueprint_layouts
        self._orig_save_graphs = g.save_blueprint_graphs
        self._orig_blueprint_cls = g.BlueprintWindow
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        g.save_blueprint_layouts = lambda *a, **k: None
        g.save_blueprint_graphs = lambda *a, **k: None
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        g.save_blueprint_layouts = self._orig_save_layouts
        g.save_blueprint_graphs = self._orig_save_graphs
        g.BlueprintWindow = self._orig_blueprint_cls
        self._tmp.cleanup()

    def _install_stub(self):
        captured = {}

        class StubBlueprintWindow:
            def __init__(self, tasks_arg, layout_data, save_callback,
                         group_metadata=None, parent=None, execution_states=None):
                captured["tasks"] = tasks_arg
                captured["layout"] = layout_data
                captured["group"] = group_metadata

            def show(self):
                pass

        g.BlueprintWindow = StubBlueprintWindow
        return captured

    def test_open_blueprint_fills_missing_flow_next(self):
        self.assertTrue(len(g.TASKS) >= 2, "预设步骤不足，无法测试接线")
        first, second = g.TASKS[0], g.TASKS[1]
        first["type"] = "normal"
        first["flow_next"] = str(second.get("id"))
        payload = g.NodeGraph(g.TASKS).to_payload()
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.blueprint_graphs[mode] = payload
        first.pop("flow_next", None)

        captured = self._install_stub()
        self.window.open_blueprint()
        self.window.blueprint_window = None  # 桩对象没有 close()，避免 tearDown 误用

        self.assertIn("tasks", captured, "open_blueprint 应构造蓝图窗口")
        self.assertEqual(captured["tasks"][0].get("flow_next"), str(second.get("id")))

    def test_open_blueprint_does_not_overwrite_existing_flow_next(self):
        self.assertTrue(len(g.TASKS) >= 3, "预设步骤不足，无法测试接线")
        first, second, third = g.TASKS[0], g.TASKS[1], g.TASKS[2]
        first["type"] = "normal"
        first["flow_next"] = str(second.get("id"))
        payload = g.NodeGraph(g.TASKS).to_payload()
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.blueprint_graphs[mode] = payload
        first["flow_next"] = str(third.get("id"))  # 用户后来改成了第三步

        captured = self._install_stub()
        self.window.open_blueprint()
        self.window.blueprint_window = None

        self.assertEqual(
            captured["tasks"][0].get("flow_next"), str(third.get("id")),
            "已有值不能被旧图快照覆盖",
        )

    def test_open_blueprint_without_graph_data_is_unchanged(self):
        mode = self.window.mode_combo.currentText() or "custom"
        self.window.blueprint_graphs.pop(mode, None)
        before = copy.deepcopy(g.TASKS)

        captured = self._install_stub()
        self.window.open_blueprint()
        self.window.blueprint_window = None

        self.assertEqual(captured["tasks"], before)


if __name__ == "__main__":
    unittest.main()
