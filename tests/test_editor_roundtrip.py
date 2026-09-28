"""编辑器「加载 → 改值 → 应用」往返测试，外加装饰器静态守卫。

背景
----
commit 36da50b 在删除已退休的 `open_detour_editor` 时，误把紧随其后的
`PySide6ScriptWindow._float_value` 的 `@staticmethod` 一并删掉了。于是那行
残留的 `@Slot()` 变成了 `_float_value` 的装饰器，它的第一个参数从
`self` 变成了 `value`。后果是主窗口「应用修改」按钮一点就崩溃：

    TypeError: PySide6ScriptWindow._float_value() takes 2 positional
               arguments but 3 were given

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
        wanted = {"_float_value", "_int_value", "_float", "_int"}
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
            self.assertEqual(window._float_value("1.5", 0.0), 1.5)
            self.assertEqual(window._float_value("不是数字", 9.0), 9.0)
            self.assertEqual(window._int_value("7"), 7)
            self.assertIsNone(window._int_value("x"))
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

    def test_apply_writes_paired_offset_and_click(self):
        task = self._load_first_task()
        task["offset_x"], task["offset_y"] = 11.0, -22.0
        task["offset"] = (11.0, -22.0)
        task["click_x"], task["click_y"] = 333, 444
        task["click_position"] = (333, 444)
        self.window.load_selected_task(self.window._find_task_item(0), None)

        # 显示格式走统一格式化，这里按数值比对而不是比对字面量
        self.assertEqual(g._to_float(self.window.offset_x_edit.text(), None), 11.0)
        self.assertEqual(g._to_float(self.window.offset_y_edit.text(), None), -22.0)
        self.assertEqual(g._to_int(self.window.click_x_edit.text()), 333)
        self.assertEqual(g._to_int(self.window.click_y_edit.text()), 444)

        self.window.offset_x_edit.setText("7")
        self.window.offset_y_edit.setText("8")
        self.window.click_x_edit.setText("99")
        self.window.click_y_edit.setText("100")
        self.window.apply_selected_task()

        self.assertEqual(task["offset"], (7.0, 8.0))
        self.assertEqual(task["offset_x"], 7.0)
        self.assertEqual(task["offset_y"], 8.0)
        self.assertEqual(task["click_position"], (99, 100))

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

    def test_apply_round_trips_match_rect(self):
        task = self._load_first_task()
        # 主窗口的区域来源是可见的 4 个输入框（此前测试写的是一个从未进布局的
        # 隐形 match_rect_edit —— 那条断言在无意中给隐形控件上了锁）
        for editor, value in ((self.window.region_left_edit, "10"),
                              (self.window.region_top_edit, "20"),
                              (self.window.region_right_edit, "30"),
                              (self.window.region_bottom_edit, "40")):
            editor.setText(value)
        self.window.apply_selected_task()

        self.assertEqual(task["match_rect"], (10, 20, 30, 40))
        self.assertEqual(task["search_rect"], (10, 20, 30, 40))
        self.assertEqual(task["match_rects"], [(10, 20, 30, 40)])

    def test_apply_clears_region_when_boxes_are_empty(self):
        task = self._load_first_task()
        task["match_rect"] = (10, 20, 30, 40)
        task["search_rect"] = (10, 20, 30, 40)
        task["match_rects"] = [(10, 20, 30, 40)]
        self.window.load_selected_task(self.window._find_task_item(0), None)

        for editor in (self.window.region_left_edit, self.window.region_top_edit,
                       self.window.region_right_edit, self.window.region_bottom_edit):
            editor.clear()
        self.window.apply_selected_task()

        self.assertNotIn("match_rect", task)
        self.assertNotIn("search_rect", task)
        self.assertNotIn("match_rects", task)

    def test_apply_keeps_the_region_when_boxes_are_incomplete(self):
        """4 框填一半时不改动该区域。

        此前这里会读一个**不可见的** match_rect_edit 把旧值悄悄写回（用户的输入被
        静默忽略）；短暂的一版实现又改成"删除该区域"。现在的规则是：填一半属于
        打错字，什么都不改，并在日志里说明。
        """
        task = self._load_first_task()
        task["match_rect"] = (10, 20, 30, 40)
        task["search_rect"] = (10, 20, 30, 40)
        task["match_rects"] = [(10, 20, 30, 40)]
        self.window.load_selected_task(self.window._find_task_item(0), None)

        self.window.region_right_edit.clear()
        self.window.region_bottom_edit.clear()
        self.window.apply_selected_task()

        self.assertEqual(task["match_rect"], (10, 20, 30, 40), "填一半不该丢掉区域")
        self.assertEqual(task["match_rects"], [(10, 20, 30, 40)])
        self.assertIn("未修改该区域", self.window.log_box.toPlainText())

    def test_apply_keeps_the_region_when_the_rect_is_inverted(self):
        """右下小于左上（退化矩形）同样不改动该区域，只提示。"""
        task = self._load_first_task()
        task["match_rect"] = (10, 20, 30, 40)
        task["search_rect"] = (10, 20, 30, 40)
        task["match_rects"] = [(10, 20, 30, 40)]
        self.window.load_selected_task(self.window._find_task_item(0), None)

        for editor, value in ((self.window.region_left_edit, "300"),
                              (self.window.region_top_edit, "20"),
                              (self.window.region_right_edit, "100"),
                              (self.window.region_bottom_edit, "40")):
            editor.setText(value)
        self.window.apply_selected_task()

        self.assertEqual(task["match_rect"], (10, 20, 30, 40), "退化矩形不该丢掉区域")
        self.assertIn("未修改该区域", self.window.log_box.toPlainText())

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

    def test_blueprint_apply_editor_writes_offset(self):
        own_task = {
            "type": "normal",
            "template": "new_step",
            "description": "蓝图往返",
            "offset_x": 0.0,
            "offset_y": 0.0,
            "offset": (0.0, 0.0),
        }
        own_tasks = [own_task]
        # save_callback 传空实现，确保 save_layout() 不会写真实布局文件
        blueprint = g.BlueprintWindow(own_tasks, {}, lambda *a: None, {}, None, {})
        try:
            blueprint.current_index = 0
            blueprint._load_editor_for_task(own_task)
            blueprint.offset_x_edit.setText("1")
            blueprint.offset_y_edit.setText("2")
            blueprint._apply_editor()  # 曾经在这里崩溃
        finally:
            blueprint.close()

        self.assertEqual(own_task["offset"], (1.0, 2.0))
        self.assertEqual(own_task["offset_x"], 1.0)
        self.assertEqual(own_task["offset_y"], 2.0)
        self.assertEqual(own_task["description"], "蓝图往返")

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
