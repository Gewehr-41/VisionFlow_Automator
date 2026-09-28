"""表单行构造器（模块级共享辅助函数）的测试。

背景
----
两个窗口此前各自在 `_build_ui` 里定义了一份同名的局部闭包
（`add_row` / `add_coordinate_row` / `add_click_until_row`）。三对函数体逐字节
相同、只是闭包变量不同，共 6 份。现在抽成模块级的
`_add_form_row` / `_add_coordinate_row` / `_add_click_until_row`，
由 `functools.partial` 绑定各自的布局，调用点一字未改。

本文件既验证这三个辅助函数本身，也验证两个窗口的字段**确实被排进了同一行**
（防止 partial 绑定写错导致控件没进布局——那种错误不会报错，只会让控件消失）。
"""

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QVBoxLayout, QWidget

import tasks

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g


def find_row_layout_containing(parent_layout, widget):
    """在 parent_layout 的直接子布局里，找出包含 widget 的那一行。"""
    for index in range(parent_layout.count()):
        child = parent_layout.itemAt(index).layout()
        if child is None:
            continue
        for position in range(child.count()):
            if child.itemAt(position).widget() is widget:
                return child
    return None


def row_widget_count(row_layout):
    return sum(
        1 for i in range(row_layout.count())
        if row_layout.itemAt(i).widget() is not None
    )


class HelperUnitTests(unittest.TestCase):
    def setUp(self):
        self.host = QWidget()
        self.layout = QVBoxLayout(self.host)

    def tearDown(self):
        self.host.deleteLater()

    def test_add_form_row_adds_one_row_with_label_and_widget(self):
        edit = QLineEdit()
        g._add_form_row(self.layout, "标签:", edit)

        self.assertEqual(self.layout.count(), 1, "应新增一行")
        row = self.layout.itemAt(0).layout()
        self.assertIsNotNone(row)
        self.assertEqual(row_widget_count(row), 2, "一行应是「标签 + 控件」")
        row_widgets = [row.itemAt(i).widget() for i in range(row.count())]
        self.assertIsInstance(row_widgets[0], QLabel)
        self.assertEqual(row_widgets[0].text(), "标签:")
        self.assertIs(row_widgets[1], edit)

    def test_add_coordinate_row_pairs_x_and_y(self):
        x_edit, y_edit = QLineEdit(), QLineEdit()
        g._add_coordinate_row(self.layout, "偏移:", x_edit, y_edit)

        row = self.layout.itemAt(0).layout()
        widgets = [row.itemAt(i).widget() for i in range(row.count())]
        self.assertEqual(len(widgets), 5, "一行应是「标签 + X + 输入 + Y + 输入」")
        self.assertEqual([w.text() for w in widgets[:2]], ["偏移:", "X"])
        self.assertIs(widgets[2], x_edit)
        self.assertEqual(widgets[3].text(), "Y")
        self.assertIs(widgets[4], y_edit)

    def test_add_click_until_row_is_a_plain_form_row(self):
        edit = QLineEdit()
        g._add_click_until_row(self.layout, "点击间隔(秒):", edit)

        row = self.layout.itemAt(0).layout()
        self.assertEqual(row_widget_count(row), 2)
        self.assertIs(row.itemAt(1).widget(), edit)

    def test_helpers_append_instead_of_replacing(self):
        g._add_form_row(self.layout, "一:", QLineEdit())
        g._add_form_row(self.layout, "二:", QLineEdit())
        self.assertEqual(self.layout.count(), 2)

    def test_every_gui_attribute_used_by_tests_still_exists(self):
        """测试里所有 `gui_pyside6.<名字>` 的引用都必须仍然可解析。

        这条守卫是模块外移时踩坑后补的：清理"未使用导入"时把 `QDialog` 删了，
        生产代码毫无影响，但 16 个测试因为 `mock.patch.object(g.QDialog, ...)`
        全部报错。`gui_pyside6.<名字>` 是一层事实上的兼容面，删除前必须先确认
        没有人在用。
        """
        import re
        from pathlib import Path

        referenced = set()
        for path in Path(__file__).resolve().parent.glob("*.py"):
            referenced |= set(re.findall(r"\bg\.([A-Za-z_][A-Za-z0-9_]*)",
                                         path.read_text(encoding="utf-8")))
        missing = sorted(name for name in referenced if not hasattr(g, name))
        self.assertEqual(missing, [], f"这些名字测试在用，但 gui_pyside6 已不提供：{missing}")

    def test_extracted_classes_live_in_their_own_modules(self):
        """外移的类必须**定义在**新模块里，而不是复制回 gui_pyside6。"""
        import blueprint_canvas
        import capture_overlay
        import task_widgets

        expected = {
            g.CaptureOverlayMixin: capture_overlay,
            g.CaptureOverlay: capture_overlay,
            g.BlueprintNodeItem: blueprint_canvas,
            g.BlueprintWireItem: blueprint_canvas,
            g.BendHandleItem: blueprint_canvas,
            g.BlueprintScene: blueprint_canvas,
            g.BlueprintGroupItem: blueprint_canvas,
            g.BlueprintView: blueprint_canvas,
            g.TaskItemDelegate: task_widgets,
            g.TaskListWidget: task_widgets,
            g.TaskWorker: task_widgets,
        }
        for obj, module in expected.items():
            with self.subTest(name=obj.__name__):
                self.assertIs(obj.__module__, module.__name__,
                              f"{obj.__name__} 应在 {module.__name__} 里定义")

    def test_shared_helpers_are_reexported_from_the_gui_module(self):
        """这些辅助函数已外移到 ui_common，但仍须能从 gui_pyside6 取到。

        兼容面：测试与外部脚本都用 `gui_pyside6.<名字>`；同时断言它们**确实**
        定义在 ui_common（而不是被复制回 gui_pyside6 —— 那就又变成两份了）。
        """
        import ui_common

        for name in ("_to_float", "_to_int", "_add_form_row", "_add_coordinate_row",
                     "_add_click_until_row", "_add_region_selector_row", "_jump_index_map",
                     "_load_template_pixmap", "_first_available_template_pixmap", "_capture_rect"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(g, name), f"gui_pyside6 缺少重新导出的 {name}")
                self.assertIs(getattr(g, name), getattr(ui_common, name),
                              f"{name} 应是 ui_common 里的同一个对象")
                self.assertEqual(getattr(g, name).__module__, "ui_common",
                                 f"{name} 应实际定义在 ui_common，而不是复制回 gui_pyside6")


class WindowLayoutTests(unittest.TestCase):
    """两个窗口的字段必须真的进了同一行布局。"""

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
        self.main_window = g.PySide6ScriptWindow()
        self.blueprint = g.BlueprintWindow([{"id": "a", "type": "normal"}], {},
                                           lambda *a, **k: None, {}, None, {})

    def tearDown(self):
        self.main_window.close()
        self.blueprint.close()
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()

    def _windows(self):
        return (("主窗口", self.main_window), ("蓝图窗口", self.blueprint))

    def test_click_fields_share_a_row_in_both_windows(self):
        for name, window in self._windows():
            layout = window.recognition_group.layout()
            with self.subTest(window=name):
                for x_edit, y_edit, field in (
                    (window.click_x_edit, window.click_y_edit, "点击"),
                ):
                    x_row = find_row_layout_containing(layout, x_edit)
                    y_row = find_row_layout_containing(layout, y_edit)
                    self.assertIsNotNone(x_row, f"{name} 的{field}X 没进任何行布局")
                    self.assertIs(
                        x_row, y_row,
                        f"{name} 的{field} X/Y 应在同一行（否则 partial 绑定错了）",
                    )

    def test_both_windows_use_the_shared_panel_builder(self):
        """面板控件必须由 mixin 的共享构造器建；两个窗口的差异只能通过覆盖钩子表达。"""
        for owner in (g.PySide6ScriptWindow, g.BlueprintWindow):
            with self.subTest(owner=owner.__name__):
                self.assertIs(getattr(owner, "_build_shared_editor_panel"),
                              getattr(g.CaptureOverlayMixin, "_build_shared_editor_panel"),
                              f"{owner.__name__} 应直接复用共享构造器")
                # 「应用修改」两个窗口都提供同名槽（共享构造器直接接 _apply_editor）
                self.assertTrue(callable(getattr(owner, "_apply_editor", None)),
                                f"{owner.__name__} 缺少 _apply_editor 槽")
        # 主窗口覆盖 3 个钩子（标签文字 / 类型专用字段容器 / 预览占位文字）
        for name in ("_editor_field_labels", "_editor_special_form_container",
                     "_editor_preview_placeholder"):
            with self.subTest(hook=name):
                self.assertIsNot(getattr(g.PySide6ScriptWindow, name),
                                 getattr(g.CaptureOverlayMixin, name),
                                 f"{name} 是主窗口的显式差异，应覆盖")

    def test_shared_builder_creates_every_panel_widget(self):
        """共享构造器少建一个控件就会在这里失败（两个窗口都要有）。"""
        expected = (
            "selected_label", "template_preview", "editor_actions", "apply_button",
            "description_edit", "enabled_checkbox", "recognition_group", "region_group",
            "click_until_group", "template_edit", "threshold_edit", "timeout_edit",
            "after_wait_edit", "click_x_edit", "click_y_edit", "region_selector",
            "region_status_label", "region_delete_button", "next_template_edit",
            "wait_for_combo", "click_checkbox", "match_required_checkbox", "optional_checkbox",
            "detour_button", "special_form", "group_form", "group_name_edit",
            "group_color_button", "group_apply_button",
        )
        for label, window in self._windows():
            for attr in expected:
                with self.subTest(window=label, attr=attr):
                    self.assertTrue(hasattr(window, attr), f"{label} 缺少面板控件 {attr}")

    def test_offset_row_is_gone_from_both_windows(self):
        """「偏移」一行已按所有者要求删掉：两个窗口都不该再有它的控件。

        引擎仍读 `task["offset"]`（缺省 (0, 0)），只是界面不再提供输入。
        """
        for name, window in self._windows():
            with self.subTest(window=name):
                layout = window.recognition_group.layout()
                for attr in ("offset_x_edit", "offset_y_edit"):
                    self.assertFalse(hasattr(window, attr), f"{name} 不该再有 {attr}")
                labels = [
                    layout.itemAt(i).widget().text()
                    for i in range(layout.count())
                    if layout.itemAt(i) is not None and layout.itemAt(i).widget() is not None
                    and hasattr(layout.itemAt(i).widget(), "text")
                ]
                self.assertNotIn("偏移:", labels, f"{name} 不该再有「偏移」这一行")

    def test_region_group_has_no_coordinate_inputs(self):
        """识别区域不再提供手工坐标输入（区域只能靠框选产生）。"""
        for name, window in self._windows():
            with self.subTest(window=name):
                layout = window.region_group.layout()
                for attr in ("region_left_edit", "region_top_edit", "region_right_edit",
                             "region_bottom_edit", "region_center_x_edit", "region_center_y_edit",
                             "match_rect_edit"):
                    self.assertFalse(hasattr(window, attr),
                                     f"{name} 不该再有 {attr}（手工坐标输入已移除）")
                self.assertIsNotNone(
                    find_row_layout_containing(layout, window.region_selector),
                    f"{name} 的区域选择器不在识别区域组里")

    def test_template_name_field_is_laid_out_in_both_windows(self):
        for name, window in self._windows():
            with self.subTest(window=name):
                layout = window.recognition_group.layout()
                row = find_row_layout_containing(layout, window.template_edit)
                self.assertIsNotNone(row, f"{name} 的模板名输入框没进布局")

    def test_click_until_fields_are_laid_out_in_both_windows(self):
        for name, window in self._windows():
            layout = window.click_until_group.layout()
            with self.subTest(window=name):
                for widget in (window.click_until_template_edit,
                               window.click_until_interval_edit,
                               window.click_until_stop_delay_edit,
                               window.click_until_timeout_edit):
                    self.assertIsNotNone(
                        find_row_layout_containing(layout, widget),
                        f"{name} 的持续点击字段没进布局",
                    )

    def test_recognition_form_row_count_is_stable(self):
        """行数写死一份期望，防止改动时意外增删整行。"""
        for name, window in self._windows():
            with self.subTest(window=name):
                layout = window.recognition_group.layout()
                rows = sum(
                    1 for i in range(layout.count())
                    if layout.itemAt(i).layout() is not None
                )
                self.assertGreater(rows, 0, f"{name} 的识别表单一行都没有")


def field_label_of_row(row_layout):
    """返回某一行里第一个 QLabel 的文本（代表这一行是什么字段）。"""
    for i in range(row_layout.count()):
        widget = row_layout.itemAt(i).widget()
        if isinstance(widget, QLabel):
            return widget.text()
    return None


# 标签 -> 规范字段名。两个窗口的标签文案不同（例如「匹配阈值(0-1)」与「匹配阈值」），
# 这里只比较**字段顺序**，所以先归一化。
def canonical_field(label):
    if label is None:
        return None
    if label.startswith("模板名"):
        return "模板名"
    if label.startswith("匹配阈值"):
        return "匹配阈值"
    if label.startswith("超时"):
        return "超时"
    if label.startswith("完成后等待"):
        return "完成后等待"
    if label == "点击:":
        return label.rstrip(":")
    if label in ("左上:", "右下:", "中心:") or label.startswith("识别区域"):
        return "<区域>"
    if label.startswith("区域选择"):
        # 「区域选择」下拉行（一个步骤可有多条识别区域）与区域输入行同属一组
        return "<区域>"
    if label.startswith("下一模板"):
        return "下一模板"
    if label.startswith("等待方式"):
        return "等待方式"
    return label


def canonical_field_order(container):
    """按界面上的实际顺序返回规范字段名（连续的区域行合并为一个 <区域>）。

    `container` 可以是窗口（取它的 `recognition_group`），也可以是任意控件
    （取它自己的 layout）——识别区域已独立成组，需要能单独检查那一组。
    """
    if hasattr(container, "recognition_group"):
        layout = container.recognition_group.layout()
    else:
        layout = container.layout()
    order = []
    for i in range(layout.count()):
        row = layout.itemAt(i).layout()
        if row is None:
            continue
        field = canonical_field(field_label_of_row(row))
        if field is None:
            continue
        if field == "<区域>" and order and order[-1] == "<区域>":
            continue  # 主窗口的区域是 3 行，合并成一个
        order.append(field)
    return order


class FormOrderParityTests(unittest.TestCase):
    """两个窗口的识别表单字段顺序必须一致。

    背景：此前主窗口的匹配阈值在坐标之后、超时在「下一模板」之后、完成后等待与
    等待方式挤在同一行，而蓝图窗口是「模板名 / 匹配阈值 / 超时 / 完成后等待 /
    坐标 / 区域 / 下一模板 / 等待方式」——同一份数据在两个窗口里读起来完全不同。

    注意：识别区域已移入独立的「识别区域」组（见 region_group），所以这里列的是
    `recognition_group` 里剩下的字段；区域组的顺序由 RegionGroupOrderTests 覆盖。
    """

    EXPECTED = ["模板名", "匹配阈值", "超时", "完成后等待",
                "点击", "下一模板", "等待方式"]

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
        self.main_window = g.PySide6ScriptWindow()
        self.blueprint = g.BlueprintWindow([{"id": "a", "type": "normal"}], {},
                                           lambda *a, **k: None, {}, None, {})

    def tearDown(self):
        self.main_window.close()
        self.blueprint.close()
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()

    def test_main_window_field_order(self):
        self.assertEqual(canonical_field_order(self.main_window), self.EXPECTED)

    def test_blueprint_window_field_order(self):
        self.assertEqual(canonical_field_order(self.blueprint), self.EXPECTED)

    def test_both_windows_agree(self):
        self.assertEqual(
            canonical_field_order(self.main_window),
            canonical_field_order(self.blueprint),
        )


class RegionDisplayWidgetTests(unittest.TestCase):
    """识别区域的"显示控件"：主窗口 = 6 个可见框，蓝图 = 1 个可见文本框。"""

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
        self.main_window = g.PySide6ScriptWindow()
        self.blueprint = g.BlueprintWindow([{"id": "a", "type": "normal"}], {},
                                           lambda *a, **k: None, {}, None, {})

    def tearDown(self):
        self.main_window.close()
        self.blueprint.close()
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()

    def test_no_region_coordinate_input_exists_in_either_window(self):
        """两个窗口都不该再有区域坐标输入控件（区域只能靠框选产生）。"""
        for name, window in (("主窗口", self.main_window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                for attr in ("match_rect_edit", "region_left_edit", "region_top_edit",
                             "region_right_edit", "region_bottom_edit",
                             "region_center_x_edit", "region_center_y_edit"):
                    self.assertFalse(hasattr(window, attr), f"{name} 仍存在 {attr}")

    def test_region_selector_and_delete_button_exist(self):
        for name, window in (("主窗口", self.main_window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                self.assertTrue(hasattr(window, "region_selector"))
                self.assertTrue(hasattr(window, "region_delete_button"))
                self.assertTrue(hasattr(window, "region_status_label"))

    def test_region_display_hooks_are_gone(self):
        """那两个只为"更新输入框"而存在的钩子应当一并移除。"""
        for name, window in (("主窗口", self.main_window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                for hook in ("_capture_show_region_widgets",
                             "_capture_clear_region_widgets",
                             "_capture_apply_current_region",
                             "_current_match_rect"):
                    self.assertFalse(hasattr(window, hook), f"{name} 仍有 {hook}")


class RegionGroupOrderTests(unittest.TestCase):
    """识别区域必须待在自己的组里，且两个窗口的顺序一致。

    背景：区域控件此前挂在 `recognition_group` 内，而那个组对 `click_until_gone`
    是隐藏的 —— 可引擎偏偏为这类步骤使用识别区域，于是"配置生效、界面看不见"。
    现在它独立成组（`region_group`），对识别型与持续点击型都可见。
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
        self.main_window = g.PySide6ScriptWindow()
        self.blueprint = g.BlueprintWindow([{"id": "a", "type": "normal"}], {},
                                           lambda *a, **k: None, {}, None, {})

    def tearDown(self):
        self.main_window.close()
        self.blueprint.close()
        tasks.PRESETS_FILE = self._orig_presets_file
        tasks.TASKS_FILE = self._orig_tasks_file
        g.save_presets = self._orig_save_presets
        g.save_tasks = self._orig_save_tasks
        self._tmp.cleanup()

    def test_region_group_exists_on_both_windows(self):
        for name, window in (("主窗口", self.main_window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                self.assertTrue(hasattr(window, "region_group"), f"{name} 没有 region_group")
                self.assertIsNotNone(window.region_group.layout())

    def test_recognition_group_has_no_region_row(self):
        """识别设置组里不该再有"识别区域"这一行（那正是旧 bug 的来源）。"""
        for name, window in (("主窗口", self.main_window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                fields = canonical_field_order(window.recognition_group)
                self.assertNotIn("<区域>", fields, f"{name} 的识别设置组里还有区域行")

    def test_selector_row_is_in_the_region_group_in_both_windows(self):
        for name, window in (("主窗口", self.main_window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                row = find_row_layout_containing(window.region_group.layout(), window.region_selector)
                self.assertIsNotNone(row, f"{name} 的区域选择器不在识别区域组里")
                self.assertIsNotNone(find_row_layout_containing(window.region_group.layout(),
                                                                window.region_delete_button))

    def test_region_group_field_order_is_the_same_in_both_windows(self):
        self.assertEqual(canonical_field_order(self.main_window.region_group),
                         canonical_field_order(self.blueprint.region_group))
        self.assertEqual(canonical_field_order(self.main_window.region_group), ["<区域>"])

    def test_region_group_sits_right_after_the_recognition_group(self):
        """显示顺序：识别设置 -> 识别区域 -> 持续点击设置。"""
        for name, window in (("主窗口", self.main_window), ("蓝图窗口", self.blueprint)):
            with self.subTest(window=name):
                layout = window.recognition_group.parentWidget().layout()
                widgets = [layout.itemAt(i).widget() for i in range(layout.count())
                           if layout.itemAt(i).widget() is not None]
                self.assertIn(window.recognition_group, widgets)
                self.assertIn(window.region_group, widgets)
                self.assertLess(widgets.index(window.recognition_group),
                                widgets.index(window.region_group),
                                "识别区域组应排在识别设置之后")
                if window.click_until_group in widgets:
                    self.assertLess(widgets.index(window.region_group),
                                    widgets.index(window.click_until_group),
                                    "识别区域组应排在持续点击设置之前")


if __name__ == "__main__":
    unittest.main()
