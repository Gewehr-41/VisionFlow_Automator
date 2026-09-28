"""蓝图校验的测试。

背景（两处真实缺陷）
--------------------
1. `_validate_blueprint_connections()` 里手工维护的字段列表只列了 7 个跳转字段，
   漏掉 `wait_timeout_jump_to` / `event_timeout_target` / `event_trigger_target`。
   漏掉的字段完全不会被校验——包括越界目标与自连接。
2. `validate_blueprint()` 把 `entry_index` + `reachable_indices` 的逻辑**内联
   重写**了一遍，而那份字段列表只有 9 个（同样漏 `event_trigger_target` 与
   `wait_timeout_jump_to`），于是仅靠这两类跳转才可达的步骤会被
   **误报为"不可达步骤"**。

现在两处都改为复用 `nodes.NodeGraph` 与 `nodes.JUMP_FIELD_LABELS`。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

_APP = QApplication.instance() or QApplication([])

import gui_pyside6 as g
import nodes


def make_window(tasks):
    """构造蓝图窗口：save_callback 传空实现，closeEvent 不会写任何文件。"""
    return g.BlueprintWindow(tasks, {}, lambda *args, **kwargs: None, {}, None, {})


class BlueprintFieldValidationTests(unittest.TestCase):
    """被漏校验的三个字段现在必须能被查出来。"""

    def _errors_for(self, task):
        tasks = [
            {"id": "a", "type": "normal"},
            {"id": "b", "type": "normal"},
            {"id": "c", "type": "normal"},
        ]
        tasks[0].update(task)
        window = make_window(tasks)
        try:
            return window._validate_blueprint_connections()
        finally:
            window.close()

    def _assert_reported(self, task, label):
        errors = self._errors_for(task)
        self.assertTrue(
            any(label in error for error in errors),
            f"未校验出「{label}」，实际错误: {errors}",
        )

    # ---------- 此前完全漏掉的三个字段 ----------

    def test_out_of_range_wait_timeout_jump_to_is_reported(self):
        self._assert_reported({"wait_timeout_jump_to": 99}, "等待超时")

    def test_non_numeric_wait_timeout_jump_to_is_reported(self):
        self._assert_reported({"wait_timeout_jump_to": "abc"}, "等待超时")

    def test_self_referencing_wait_timeout_jump_to_is_reported(self):
        self._assert_reported({"wait_timeout_jump_to": 1}, "等待超时")

    def test_out_of_range_event_timeout_target_is_reported(self):
        self._assert_reported({"event_timeout_target": 99}, "事件超时")

    def test_out_of_range_event_trigger_target_is_reported(self):
        self._assert_reported({"event_trigger_target": 99}, "事件触发")

    def test_self_referencing_event_trigger_target_is_reported(self):
        self._assert_reported({"event_trigger_target": 1}, "事件触发")

    # ---------- 原本就在校验的字段没有被弄坏 ----------

    def test_previously_covered_fields_still_reported(self):
        for field, label in (
            ("detour_jump_to", "未识别"),
            ("detour_success_jump_to", "识别成功"),
            ("timeout_jump_to", "超时"),
            ("condition_true_jump_to", "条件成立"),
            ("condition_false_jump_to", "条件不成立"),
            ("loop_target", "循环体"),
            ("loop_exit_target", "循环退出"),
            ("switch_default_jump_to", "Switch 默认"),
        ):
            with self.subTest(field=field):
                self._assert_reported({field: 99}, label)

    def test_switch_case_target_is_reported(self):
        self._assert_reported({"switch_cases": {"案例": 99}}, "Switch")

    def test_dangling_flow_next_is_reported(self):
        self._assert_reported({"flow_next": "不存在的节点"}, "普通连接目标不存在")

    def test_duplicate_ids_are_reported(self):
        tasks = [{"id": "same"}, {"id": "same"}]
        window = make_window(tasks)
        try:
            errors = window._validate_blueprint_connections()
        finally:
            window.close()
        self.assertTrue(any("重复节点 ID" in error for error in errors), errors)

    def test_clean_tasks_produce_no_errors(self):
        tasks = [
            {"id": "a", "type": "normal", "flow_next": "b"},
            {"id": "b", "type": "normal", "detour_jump_to": 1},
        ]
        window = make_window(tasks)
        try:
            self.assertEqual(window._validate_blueprint_connections(), [])
        finally:
            window.close()

    def test_label_map_covers_every_field_that_gets_validated(self):
        """防止再次出现"手写列表漏字段"：逐个字段都必须能被查出来。"""
        for field in nodes.JUMP_TARGET_FIELDS:
            with self.subTest(field=field):
                if field == "switch_default_jump_to":
                    continue  # 也在校验，只是错误文案走 Switch 分支
                errors = self._errors_for({field: 99})
                label = nodes.JUMP_FIELD_LABELS[field]
                self.assertTrue(
                    any(label in error for error in errors),
                    f"字段 {field}（{label}）没有被校验",
                )


class BlueprintReachabilityTests(unittest.TestCase):
    """仅靠事件/等待超时跳转可达的步骤，不能再被误报为不可达。"""

    def _messages_from_validate(self, tasks):
        """返回 (warnings, informations)：有错误走 warning，无错误走 information。"""
        warnings = []
        informations = []
        original_warning = g.QMessageBox.warning
        original_information = g.QMessageBox.information
        g.QMessageBox.warning = staticmethod(
            lambda *args: warnings.append(args[2] if len(args) > 2 else ""))
        g.QMessageBox.information = staticmethod(
            lambda *args: informations.append(args[2] if len(args) > 2 else ""))
        window = make_window(tasks)
        try:
            window.validate_blueprint()
        finally:
            window.close()
            g.QMessageBox.warning = original_warning
            g.QMessageBox.information = original_information
        return warnings, informations

    def _branch_tasks(self, branch_field):
        return [
            {"id": "a", "type": "normal", "flow_next": "b"},
            {"id": "b", "type": "normal", branch_field: 3, "flow_next_disabled": True},
            {"id": "c", "type": "normal"},
        ]

    def test_event_trigger_only_step_is_not_reported_unreachable(self):
        warnings, informations = self._messages_from_validate(self._branch_tasks("event_trigger_target"))
        self.assertEqual(warnings, [], f"误报为有问题: {warnings}")
        self.assertEqual(len(informations), 1, "应当报「蓝图连接完整」")

    def test_wait_timeout_only_step_is_not_reported_unreachable(self):
        warnings, informations = self._messages_from_validate(self._branch_tasks("wait_timeout_jump_to"))
        self.assertEqual(warnings, [], f"误报为有问题: {warnings}")
        self.assertEqual(len(informations), 1, "应当报「蓝图连接完整」")

    def test_every_jump_field_alone_keeps_the_graph_reachable(self):
        for field in nodes.JUMP_TARGET_FIELDS:
            with self.subTest(field=field):
                warnings, _ = self._messages_from_validate(self._branch_tasks(field))
                self.assertEqual(warnings, [], f"仅靠 {field} 可达的步骤被误报: {warnings}")

    def test_genuinely_unreachable_step_is_still_reported(self):
        tasks = [
            {"id": "a", "type": "normal", "flow_next_disabled": True},
            {"id": "b", "type": "normal", "flow_next_disabled": True},
        ]
        warnings, _ = self._messages_from_validate(tasks)
        self.assertTrue(warnings, "真正不可达的步骤必须照旧报出来")
        self.assertIn("不可达步骤", warnings[0])

    def test_clean_linear_blueprint_reports_success(self):
        tasks = [
            {"id": "a", "type": "normal", "flow_next": "b"},
            {"id": "b", "type": "normal"},
        ]
        warnings, informations = self._messages_from_validate(tasks)
        self.assertEqual(warnings, [])
        self.assertIn("未发现不可达步骤", informations[0])


if __name__ == "__main__":
    unittest.main()
