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
import blueprint_canvas
import capture_overlay
import task_widgets

# 界面代码已拆成 4 个模块（gui_pyside6 只留两个窗口类与入口），
# 静态守卫必须扫全部，否则代码一搬家守卫就静默失效。
_GUI_SOURCES = [
    pathlib.Path(module.__file__).resolve()
    for module in (g, capture_overlay, blueprint_canvas, task_widgets)
]


def _iter_class_methods():
    """产出 (类名, 方法名, 函数节点, 装饰器名集合)。"""
    for source in _GUI_SOURCES:
        tree = ast.parse(source.read_text(encoding="utf-8"))
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

    def test_staticmethod_does_not_take_self(self):
        """反向守卫：@staticmethod/@classmethod 不能配错首参。

        这条是删除重复方法时踩坑后补的：按行范围删掉 `@staticmethod def _special_field_specs(task)`
        时，装饰器那一行留在了原地，落到了下一个方法 `_clear_editor(self)` 头上
        → 它被当成静态方法调用，`self` 变成了必填参数，
        20+ 个测试报 `_clear_editor() missing 1 required positional argument: 'self'`。
        原来那条守卫只查「普通方法首参不是 self」，查不出这种反向错误。
        """
        offenders = []
        for class_name, func_name, node, decorators in _iter_class_methods():
            args = node.args.posonlyargs + node.args.args
            first = args[0].arg if args else None
            if "staticmethod" in decorators and first == "self":
                offenders.append(f"{class_name}.{func_name} 标了 @staticmethod 却用 self 作首参")
            if "classmethod" in decorators and first != "cls":
                offenders.append(f"{class_name}.{func_name} 标了 @classmethod 但首参是 {first!r}")
        self.assertEqual(
            offenders, [],
            "装饰器与方法首参不匹配（通常是删除/移动方法时把装饰器留在了原处）：\n  "
            + "\n  ".join(offenders),
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


class PresetManagerSharingTests(unittest.TestCase):
    """预设管理（8 个方法）必须由 preset_manager 提供，不得再抄回窗口类。"""

    METHODS = ["_save_presets", "_refresh_mode_combo", "create_preset", "rename_current_preset",
               "copy_current_preset", "delete_current_preset", "export_current_preset",
               "import_preset"]

    def test_preset_methods_come_from_the_manager_mixin(self):
        import preset_manager

        for name in self.METHODS:
            with self.subTest(method=name):
                self.assertNotIn(name, g.PySide6ScriptWindow.__dict__,
                                 f"{name} 不该在窗口类里再定义一份")
                self.assertIs(getattr(g.PySide6ScriptWindow, name),
                              getattr(preset_manager.PresetManagerMixin, name),
                              f"{name} 应由 PresetManagerMixin 提供")


class TaskListSharingTests(unittest.TestCase):
    """任务列表（13 个方法）必须由 task_list_manager 提供，且 MRO 顺序不能错。"""

    METHODS = ["_capture_task_list", "_copy_selected_entries", "_find_task_item",
               "_run_task_list_commit", "_schedule_task_list_commit", "_selected_copy_entries",
               "copy_selected_item", "delete_selected_item", "move_task_to_end",
               "refresh_task_list", "select_task_index", "toggle_item", "toggle_selected_tasks"]

    def test_task_list_methods_come_from_the_mixin(self):
        import task_list_manager

        for name in self.METHODS:
            with self.subTest(method=name):
                self.assertNotIn(name, g.PySide6ScriptWindow.__dict__,
                                 f"{name} 不该在窗口类里再定义一份")
                self.assertIs(getattr(g.PySide6ScriptWindow, name),
                              getattr(task_list_manager.TaskListMixin, name),
                              f"{name} 应由 TaskListMixin 提供")

    def test_task_list_mixin_precedes_the_overlay_mixin(self):
        """`_capture_task_list` 是采集 mixin 要求的访问器钩子。

        它必须由 TaskListMixin 提供（我们在这里实现了它）。若窗口类的基类顺序被改成
        `CaptureOverlayMixin` 在前，采集管线会拿到那个空实现（返回 None），
        属于**静默失效**——所以这里把两件事都断言下来。
        """
        mro = [klass.__name__ for klass in g.PySide6ScriptWindow.__mro__]
        self.assertIn("TaskListMixin", mro)
        self.assertLess(mro.index("TaskListMixin"), mro.index("CaptureOverlayMixin"),
                        "TaskListMixin 必须在 CaptureOverlayMixin 之前")
        self.assertIsNot(g.PySide6ScriptWindow._capture_task_list,
                         g.CaptureOverlayMixin.__dict__["_capture_task_list"],
                         "_capture_task_list 不能被采集 mixin 的空实现盖住")


class ExecutionControlSharingTests(unittest.TestCase):
    """执行控制（14 个方法）必须由 execution_control 提供。"""

    METHODS = ["_capture_log", "_log_pending_warnings", "_on_global_stop_hotkey",
               "_restore_config_after_run", "_start_stop_hotkey", "_start_worker",
               "_stop_stop_hotkey", "_request_worker_quit_if_running", "append_log",
               "on_worker_finished", "start_script", "stop_script", "step_script", "toggle_pause"]

    def test_execution_methods_come_from_the_mixin(self):
        import execution_control

        for name in self.METHODS:
            with self.subTest(method=name):
                self.assertNotIn(name, g.PySide6ScriptWindow.__dict__,
                                 f"{name} 不该在窗口类里再定义一份")
                self.assertIs(getattr(g.PySide6ScriptWindow, name),
                              getattr(execution_control.ExecutionControlMixin, name),
                              f"{name} 应由 ExecutionControlMixin 提供")

    def test_worker_class_is_imported_by_the_execution_module(self):
        """worker/线程类由 execution_control 导入 —— 测试拦 worker 必须替换那里的名字。

        否则 `_start_worker` 会拿到真类，**真的启动脚本**（截屏 + 点击），
        而只看 config 断言的测试不会失败。这条守卫把这个事实钉住。
        """
        import execution_control

        self.assertIs(execution_control.TaskWorker,
                      __import__("task_widgets").TaskWorker)
        self.assertIs(g.TaskWorker, execution_control.TaskWorker)


class BlueprintInteractionSharingTests(unittest.TestCase):
    """蓝图窗口的画布交互（24 个方法）必须由 blueprint_interaction 提供。"""

    METHODS = ["_add_bend", "_apply_wire_geometry", "_create_bend_handle", "_delete_bend",
               "_edge_for_wire", "_node_endpoint", "_node_hidden", "_node_item_from",
               "_on_bend_moved", "_on_node_moved", "_on_selection_changed", "_on_wire_clicked",
               "_on_zoom_changed", "_persist_bends", "_refresh_editor_from_selection",
               "_restore_saved_zoom", "_schedule_scene_rect_update", "_select_node",
               "_sync_bend_handles", "_update_scene_rect_deferred", "_visible_scene_rect",
               "_wire_color", "save_layout", "set_grid_snap"]

    def test_canvas_methods_come_from_the_mixin(self):
        import blueprint_interaction

        for name in self.METHODS:
            with self.subTest(method=name):
                self.assertNotIn(name, g.BlueprintWindow.__dict__,
                                 f"{name} 不该在蓝图窗口类里再定义一份")
                self.assertIs(getattr(g.BlueprintWindow, name),
                              getattr(blueprint_interaction.BlueprintInteractionMixin, name),
                              f"{name} 应由 BlueprintInteractionMixin 提供")


class BlueprintGroupSharingTests(unittest.TestCase):
    """蓝图窗口的分组（23 个方法）必须由 blueprint_groups 提供。"""

    METHODS = ["_add_group_menu", "_add_group_menu_item", "_collapsed_group_for", "_delete_group",
               "_edit_group", "_group_after_editor_applied", "_group_all_node_indices",
               "_group_color_button_text_color", "_group_color_dialog_title",
               "_group_default_color", "_group_descendants", "_group_hide_editor_groups",
               "_group_metadata_for_current_mode", "_group_node_indices", "_load_collapsed_groups",
               "_on_empty_group_moved", "_render_groups", "_sync_group_bounds", "add_group",
               "remove_from_group", "set_selection_group", "show_group_context_menu",
               "toggle_group"]

    def test_group_methods_come_from_the_mixin(self):
        import blueprint_groups

        for name in self.METHODS:
            with self.subTest(method=name):
                self.assertNotIn(name, g.BlueprintWindow.__dict__,
                                 f"{name} 不该在蓝图窗口类里再定义一份")
                self.assertIs(getattr(g.BlueprintWindow, name),
                              getattr(blueprint_groups.BlueprintGroupMixin, name),
                              f"{name} 应由 BlueprintGroupMixin 提供")

    def test_group_mixin_precedes_the_overlay_mixin(self):
        """`_group_*` 那几个钩子由采集 mixin 调用，必须在它之前解析到实现。"""
        mro = [klass.__name__ for klass in g.BlueprintWindow.__mro__]
        self.assertLess(mro.index("BlueprintGroupMixin"), mro.index("CaptureOverlayMixin"))


class DataIsolationSeamTests(unittest.TestCase):
    """元守卫：测试的数据隔离必须**同时**替换路径常量，不能只替换函数名。

    背景：界面代码正在被逐步搬出 `gui_pyside6.py`。测试里 `g.save_presets = tasks.save_presets`
    这类补丁只在"写盘代码与这个模块级名字同模块"时才有效；一旦代码搬到别的模块，
    它就会绕过补丁去写**真实文件**（本项目真的踩过这个坑：4 个数据文件被误写）。
    而 `tasks.PRESETS_FILE` 这类**路径**补丁是与模块无关的，任何模块调用
    `tasks.save_presets` 都会写到临时目录。
    所以规则是：凡替换了写盘函数，就必须同时替换对应的路径常量。
    """

    FUNC_TO_PATH = {
        "save_presets": "PRESETS_FILE",
        "save_tasks": "TASKS_FILE",
        "save_blueprint_layouts": "BLUEPRINT_LAYOUT_FILE",
        "save_blueprint_graphs": "BLUEPRINT_GRAPH_FILE",
    }

    def test_writers_patched_in_tests_also_patch_their_paths(self):
        import re
        from pathlib import Path

        offenders = []
        for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            patched_funcs = {func for func in self.FUNC_TO_PATH
                             if re.search(rf"g\.{func}\s*=", text)}
            if not patched_funcs:
                continue
            missing = []
            for func in sorted(patched_funcs):
                path_name = self.FUNC_TO_PATH[func]
                # 必须是「重定向到临时目录」的赋值；tearDown 里的还原赋值不算数
                # （只匹配到赋值符号会被 self._orig_xxx 这种还原语句蒙混过去）
                if not re.search(rf"tasks\.{path_name}\s*=\s*(os\.path\.join|tempfile\.|self\._tmp)",
                                 text):
                    missing.append(path_name)
            if missing:
                offenders.append(f"{path.name} 替换了函数但没把路径重定向到临时目录: {missing}")
        self.assertEqual(
            offenders, [],
            "只替换写盘函数、不替换路径常量的隔离在代码搬走后会失效并写坏真实数据：\n  "
            + "\n  ".join(offenders),
        )


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

    def test_both_windows_share_the_special_form_logic(self):
        """「类型专用字段」的渲染/回写必须共享；两窗口只提供规格表与两处语义钩子。"""
        for name in ("_rebuild_special_form", "_apply_special_fields", "_clear_special_form"):
            with self.subTest(method=name):
                self.assertIs(getattr(g.BlueprintWindow, name), getattr(g.CaptureOverlayMixin, name),
                              f"蓝图窗口应直接复用 mixin 的 {name}")
        # 主窗口只覆盖「多一步显隐」，逻辑仍走 super()
        self.assertIsNot(g.PySide6ScriptWindow._rebuild_special_form,
                         g.CaptureOverlayMixin._rebuild_special_form)
        self.assertIs(g.PySide6ScriptWindow._apply_special_fields,
                      g.CaptureOverlayMixin._apply_special_fields)
        # 规格表与两处语义钩子：两个窗口各自提供
        self.assertIsNot(g.PySide6ScriptWindow._special_field_specs,
                         g.CaptureOverlayMixin._special_field_specs,
                         "主窗口的标签文字与蓝图窗口不同，应有自己的规格表")
        self.assertIsNot(g.PySide6ScriptWindow._special_empty_input_pops,
                         g.CaptureOverlayMixin._special_empty_input_pops)
        self.assertIsNot(g.PySide6ScriptWindow._special_float_fallback,
                         g.CaptureOverlayMixin._special_float_fallback)

    def test_special_form_specs_render_a_row_per_field(self):
        """规格表里的每一项都必须真的渲染出一行（共享渲染器不能少建行）。"""
        blueprint = g.BlueprintWindow([{"id": "x", "type": "condition"}], {},
                                      lambda *a, **k: None, {}, None, {})
        try:
            task = {"type": "condition", "condition_templates": ["a"], "condition_operator": "any",
                    "condition_true_jump_to": 1, "condition_false_jump_to": 2,
                    "condition_invert": True, "threshold": 0.5}
            blueprint.current_index = 0
            blueprint._load_editor_for_task(task)
            specs = blueprint._special_field_specs(task)
            self.assertEqual(len(blueprint.special_edits), len(specs))
            self.assertEqual(blueprint.special_form.rowCount(), len(specs))
            for key, _label, _kind in specs:
                with self.subTest(key=key):
                    self.assertIn(key, blueprint.special_edits)
        finally:
            blueprint.close()

    def test_both_windows_share_the_editor_core(self):
        """公共实现必须共享；两个窗口的差异只能通过显式钩子表达。"""
        for name in ("_load_common_editor_fields", "_apply_common_editor_fields",
                     "_update_editor_visibility"):
            for owner in (g.PySide6ScriptWindow, g.BlueprintWindow):
                with self.subTest(method=name, owner=owner.__name__):
                    self.assertIs(getattr(owner, name), getattr(g.CaptureOverlayMixin, name),
                                  f"{owner.__name__}.{name} 应直接复用 mixin 实现，不要另抄一份")

        # 钩子：差异必须写成覆盖，而不是在窗口里复制整段逻辑
        # （`_editor_apply_optional` 曾在主窗口被覆盖成空实现，导致「可选步骤」勾了没
        #   效果；2026-09-28 已对齐，所以现在只剩超时初值这一处显式差异）
        overridden_by_main = ("_editor_timeout_text",)
        for name in overridden_by_main:
            with self.subTest(hook=name):
                self.assertIsNot(getattr(g.PySide6ScriptWindow, name),
                                 getattr(g.CaptureOverlayMixin, name),
                                 f"{name} 是主窗口的显式差异，应覆盖而不是消失")
                self.assertIs(getattr(g.BlueprintWindow, name),
                              getattr(g.CaptureOverlayMixin, name),
                              f"{name} 蓝图窗口应沿用 mixin 的默认实现")
        for name in ("_editor_apply_optional",):
            with self.subTest(hook=name):
                self.assertIs(getattr(g.PySide6ScriptWindow, name),
                              getattr(g.CaptureOverlayMixin, name),
                              f"{name} 两个窗口都应沿用 mixin 实现（不要再覆盖成空操作）")
                self.assertIs(getattr(g.BlueprintWindow, name),
                              getattr(g.CaptureOverlayMixin, name))
        self.assertIsNot(g.BlueprintWindow._editor_float_fallback,
                         g.CaptureOverlayMixin._editor_float_fallback,
                         "_editor_float_fallback 是蓝图窗口的显式差异，应覆盖")

    def test_both_windows_load_the_same_panel_state(self):
        """同一个步骤在两个窗口里加载后，公共字段的控件状态必须一致。

        这条不变量是 Bug C（主窗口漏加载 wait_for / next_template）的通用形式：
        此前没有任何测试比较过两个窗口的加载结果，所以漂移了很久都没人发现。
        """
        task = {
            "id": "consistency",
            "type": "normal",
            "template": "tpl_a",
            "description": "一致性检查",
            "threshold": 0.8,
            "timeout": 7.5,
            "after_wait": 0.4,
            "click": False,
            "click_requires_match": False,
            "optional": True,
            "required": False,
            "click_x": 111,
            "click_y": 222,
            "next_template": "tpl_b",
            "wait_for": "next_appear",
        }
        g.TASKS[:] = [dict(task)]
        self.window.refresh_task_list()
        self.window.select_task_index(0)

        blueprint = g.BlueprintWindow([dict(task)], {}, lambda *a, **k: None, {}, None, {})
        try:
            blueprint.current_index = 0
            blueprint._load_editor_for_task(blueprint.tasks[0])

            for attr in ("description_edit", "template_edit", "threshold_edit", "timeout_edit",
                         "after_wait_edit", "click_x_edit", "click_y_edit", "next_template_edit"):
                with self.subTest(field=attr):
                    self.assertEqual(getattr(self.window, attr).text(),
                                     getattr(blueprint, attr).text(),
                                     f"{attr} 在两个窗口里的显示不一致")
            for attr in ("click_checkbox", "match_required_checkbox", "optional_checkbox",
                         "enabled_checkbox"):
                with self.subTest(field=attr):
                    self.assertEqual(getattr(self.window, attr).isChecked(),
                                     getattr(blueprint, attr).isChecked(),
                                     f"{attr} 在两个窗口里的勾选状态不一致")
            self.assertEqual(self.window.wait_for_combo.currentIndex(),
                             blueprint.wait_for_combo.currentIndex(),
                             "「等待方式」下拉框在两个窗口里的选中项不一致")
        finally:
            blueprint.close()

    def test_both_windows_write_the_same_common_fields(self):
        """同一份面板输入，两个窗口回写出的公共字段必须一致（含 optional/required）。"""
        task = {"id": "consistency", "type": "normal", "template": "tpl_a"}
        g.TASKS[:] = [dict(task)]
        self.window.refresh_task_list()
        self.window.select_task_index(0)

        blueprint = g.BlueprintWindow([dict(task)], {}, lambda *a, **k: None, {}, None, {})
        try:
            blueprint.current_index = 0
            blueprint._load_editor_for_task(blueprint.tasks[0])
            for owner in (self.window, blueprint):
                owner.description_edit.setText("回写一致性")
                owner.template_edit.setText("tpl_z")
                owner.threshold_edit.setText("0.66")
                owner.timeout_edit.setText("6.5")
                owner.after_wait_edit.setText("0.33")
                owner.click_x_edit.setText("12")
                owner.click_y_edit.setText("34")
                owner.next_template_edit.setText("tpl_next")
                owner.click_checkbox.setChecked(True)
                owner.match_required_checkbox.setChecked(False)
                owner.wait_for_combo.setCurrentIndex(2)

            self.window.apply_selected_task()
            blueprint._apply_editor()
            mine, theirs = g.TASKS[0], blueprint.tasks[0]
        finally:
            blueprint.close()

        for key in sorted(set(mine) | set(theirs)):
            with self.subTest(key=key):
                self.assertEqual(mine.get(key), theirs.get(key),
                                 f"{key} 在两个窗口里的回写结果不一致")

    def test_apply_writes_every_common_field(self):
        """回写必须真的落进任务——逐字段的**绝对**断言。

        上一条测试只比较"两个窗口是否一致"，两边一起改坏时它抓不到
        （变异验证时确实漏掉过 `click_requires_match`）。这条补上绝对检查。
        """
        blueprint = g.BlueprintWindow([{"id": "abs", "type": "normal", "template": "tpl_a"}],
                                      {}, lambda *a, **k: None, {}, None, {})
        try:
            blueprint.current_index = 0
            blueprint._load_editor_for_task(blueprint.tasks[0])
            for label, owner, container in (("主窗口", self.window, g.TASKS),
                                            ("蓝图窗口", blueprint, blueprint.tasks)):
                with self.subTest(owner=label):
                    if label == "主窗口":
                        g.TASKS[:] = [{"id": "abs", "type": "normal", "template": "tpl_a"}]
                        self.window.refresh_task_list()
                        self.window.select_task_index(0)
                    owner._load_editor_for_task(container[0]) if label == "蓝图窗口" \
                        else self.window.load_selected_task(self.window._find_task_item(0), None)

                    owner.description_edit.setText("回写一致性")
                    owner.template_edit.setText("tpl_z")
                    owner.threshold_edit.setText("0.66")
                    owner.timeout_edit.setText("6.5")
                    owner.after_wait_edit.setText("0.33")
                    owner.click_x_edit.setText("12")
                    owner.click_y_edit.setText("34")
                    owner.next_template_edit.setText("tpl_next")
                    owner.click_checkbox.setChecked(True)
                    owner.match_required_checkbox.setChecked(False)
                    owner.optional_checkbox.setChecked(True)
                    owner.wait_for_combo.setCurrentIndex(2)

                    if label == "主窗口":
                        owner.apply_selected_task()
                    else:
                        owner._apply_editor()
                    task = container[0]

                    self.assertEqual(task["description"], "回写一致性")
                    self.assertEqual(task["template"], "tpl_z")
                    self.assertEqual(task["threshold"], 0.66)
                    self.assertEqual(task["timeout"], 6.5)
                    self.assertEqual(task["after_wait"], 0.33)
                    self.assertIs(task["click"], True)
                    self.assertIs(task["click_requires_match"], False)
                    self.assertIs(task["optional"], True, "「可选步骤（跳过）」必须真的回写")
                    self.assertIs(task["required"], False, "required 必须跟着 optional 翻")
                    self.assertEqual(task["click_position"], (12, 34))
                    self.assertEqual((task["click_x"], task["click_y"]), (12, 34))
                    self.assertEqual(task["next_template"], "tpl_next")
                    self.assertEqual(task["next_templates"], ["tpl_next"])
                    self.assertEqual(task["wait_for"], "change_then_appear")
        finally:
            blueprint.close()

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
        # 蓝图数据文件的**路径**也要一起替换（不只是替换函数）：
        # 任何搬到别的模块里的写盘代码会走 `tasks.save_blueprint_*`，那时只有路径补丁
        # 还能拦住它 —— 只补函数名的隔离在代码搬家后会失效并写坏真实文件。
        self._orig_layout_file = tasks.BLUEPRINT_LAYOUT_FILE
        self._orig_graph_file = tasks.BLUEPRINT_GRAPH_FILE
        self._orig_save_presets = g.save_presets
        self._orig_save_tasks = g.save_tasks
        self._orig_save_layouts = g.save_blueprint_layouts
        self._orig_save_graphs = g.save_blueprint_graphs
        self._orig_blueprint_cls = g.BlueprintWindow
        self._tasks_snapshot = copy.deepcopy(g.TASKS)
        tasks.PRESETS_FILE = os.path.join(self._tmp.name, "saved_presets.json")
        tasks.TASKS_FILE = os.path.join(self._tmp.name, "saved_tasks.json")
        tasks.BLUEPRINT_LAYOUT_FILE = os.path.join(self._tmp.name, "saved_blueprint_layouts.json")
        tasks.BLUEPRINT_GRAPH_FILE = os.path.join(self._tmp.name, "saved_blueprint_graphs.json")
        g.save_presets = tasks.save_presets
        g.save_tasks = tasks.save_tasks
        g.save_blueprint_layouts = tasks.save_blueprint_layouts
        g.save_blueprint_graphs = tasks.save_blueprint_graphs
        self.window = g.PySide6ScriptWindow()

    def tearDown(self):
        self.window.close()
        g.TASKS[:] = self._tasks_snapshot
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        tasks.BLUEPRINT_LAYOUT_FILE = self._orig_layout_file
        tasks.BLUEPRINT_GRAPH_FILE = self._orig_graph_file
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
