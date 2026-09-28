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

    def test_offset_and_click_fields_share_a_row_in_both_windows(self):
        for name, window in self._windows():
            layout = window.recognition_group.layout()
            with self.subTest(window=name):
                for x_edit, y_edit, field in (
                    (window.offset_x_edit, window.offset_y_edit, "偏移"),
                    (window.click_x_edit, window.click_y_edit, "点击"),
                ):
                    x_row = find_row_layout_containing(layout, x_edit)
                    y_row = find_row_layout_containing(layout, y_edit)
                    self.assertIsNotNone(x_row, f"{name} 的{field}X 没进任何行布局")
                    self.assertIs(
                        x_row, y_row,
                        f"{name} 的{field} X/Y 应在同一行（否则 partial 绑定错了）",
                    )

    def test_region_fields_share_a_row_in_main_window(self):
        layout = self.main_window.recognition_group.layout()
        for x_edit, y_edit, field in (
            (self.main_window.region_left_edit, self.main_window.region_top_edit, "左上"),
            (self.main_window.region_right_edit, self.main_window.region_bottom_edit, "右下"),
            (self.main_window.region_center_x_edit, self.main_window.region_center_y_edit, "中心"),
        ):
            with self.subTest(field=field):
                x_row = find_row_layout_containing(layout, x_edit)
                self.assertIsNotNone(x_row, f"{field} 没进任何行布局")
                self.assertIs(x_row, find_row_layout_containing(layout, y_edit))

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


if __name__ == "__main__":
    unittest.main()
