"""跳转序号重编号测试。

背景：分支跳转会以「1-based 步骤序号」寻址，任何改变步骤下标的操作
（删除 / 拖动排序 / 应用蓝图重排）都必须同步重编号，否则跳转会静默指向
错误的步骤，而现有校验发现不了（序号仍在合法范围内）。

此前该逻辑散落在 4 处且覆盖度不一致：
  - 蓝图窗口删除：10 个字段
  - 主窗口删除：10 个字段
  - 应用蓝图重排：仅 2 个字段
  - 拖动排序 / 移到末尾：0 个字段   ← 会静默破坏现有任务
现已统一到 nodes.remap_jump_targets。

测试写法说明：期望值不写死数字，而是由 new_order 独立推算，
避免测试本身的期望值算错（初版就是这样错的）。
"""

import unittest

from nodes import JUMP_TARGET_FIELDS, remap_jump_targets


def make_tasks(count):
    """构造 count 个带 id 的最小步骤。"""
    return [{"id": f"step-{i}", "type": "normal", "description": f"步骤 {i}"} for i in range(count)]


def expected_number(old_index, new_order):
    """独立推算：旧下标 old_index 在新顺序中的 1-based 序号。"""
    return new_order.index(old_index) + 1


class RemapJumpTargetsTests(unittest.TestCase):
    # ---------- 拖动排序（此前的缺陷点：完全不重编号）----------

    def test_reorder_makes_jump_follow_the_moved_step(self):
        tasks = make_tasks(3)
        tasks[0]["detour_jump_to"] = 3                 # 指向步骤 3（旧下标 2）
        new_order = [1, 2, 0]
        reordered = [tasks[i] for i in new_order]
        remap_jump_targets(reordered, {old: new for new, old in enumerate(new_order)})

        carrier = next(t for t in reordered if t["id"] == "step-0")
        self.assertEqual(carrier["detour_jump_to"], expected_number(2, new_order))

    def test_reorder_makes_jump_follow_its_own_step(self):
        """步骤自己移动后，指向它自己的跳转也要跟随（保持自指关系）。"""
        tasks = make_tasks(3)
        tasks[0]["detour_jump_to"] = 1                 # 步骤 1 指向自己
        new_order = [1, 2, 0]
        reordered = [tasks[i] for i in new_order]
        remap_jump_targets(reordered, {old: new for new, old in enumerate(new_order)})

        carrier = next(t for t in reordered if t["id"] == "step-0")
        self.assertEqual(carrier["detour_jump_to"], expected_number(0, new_order))

    def test_reorder_remaps_timeout_jump_to(self):
        """timeout_jump_to 此前在重排路径上被漏掉。"""
        tasks = make_tasks(3)
        tasks[0]["timeout_jump_to"] = 3
        new_order = [2, 0, 1]
        reordered = [tasks[i] for i in new_order]
        remap_jump_targets(reordered, {old: new for new, old in enumerate(new_order)})

        carrier = next(t for t in reordered if t["id"] == "step-0")
        self.assertEqual(carrier["timeout_jump_to"], expected_number(2, new_order))

    def test_reorder_remaps_detour_success_jump_to(self):
        tasks = make_tasks(3)
        tasks[0]["detour_success_jump_to"] = 2
        new_order = [1, 2, 0]
        reordered = [tasks[i] for i in new_order]
        remap_jump_targets(reordered, {old: new for new, old in enumerate(new_order)})

        carrier = next(t for t in reordered if t["id"] == "step-0")
        self.assertEqual(carrier["detour_success_jump_to"], expected_number(1, new_order))

    # ---------- 删除 ----------

    def test_delete_clears_jumps_pointing_at_removed_step(self):
        tasks = make_tasks(3)
        tasks[0]["detour_jump_to"] = 3                 # 指向将被删除的步骤 3
        surviving = [0, 1]
        remaining = [tasks[i] for i in surviving]
        remap_jump_targets(remaining, {old: new for new, old in enumerate(surviving)})
        self.assertNotIn("detour_jump_to", remaining[0], "指向已删除步骤的跳转应被清除")

    def test_delete_shifts_jumps_pointing_after_removed_step(self):
        tasks = make_tasks(4)
        tasks[0]["detour_jump_to"] = 4                 # 指向步骤 4（旧下标 3）
        surviving = [0, 2, 3]                          # 删除旧下标 1
        remaining = [tasks[i] for i in surviving]
        remap_jump_targets(remaining, {old: new for new, old in enumerate(surviving)})
        self.assertEqual(remaining[0]["detour_jump_to"], expected_number(3, surviving))

    def test_delete_keeps_jumps_before_removed_step_unchanged(self):
        tasks = make_tasks(4)
        tasks[0]["detour_jump_to"] = 2                 # 指向步骤 2（旧下标 1）
        surviving = [0, 1, 3]                          # 删除旧下标 2
        remaining = [tasks[i] for i in surviving]
        remap_jump_targets(remaining, {old: new for new, old in enumerate(surviving)})
        self.assertEqual(remaining[0]["detour_jump_to"], expected_number(1, surviving))
        self.assertEqual(remaining[0]["detour_jump_to"], 2, "删除点之后的步骤不影响前面的序号")

    def test_delete_multiple_steps(self):
        tasks = make_tasks(5)
        tasks[0]["detour_jump_to"] = 5                 # 指向步骤 5（旧下标 4）
        surviving = [0, 4]                             # 只删除旧下标 1、2、3；步骤 5 保留
        remaining = [tasks[i] for i in surviving]
        remap_jump_targets(remaining, {old: new for new, old in enumerate(surviving)})
        self.assertEqual(remaining[0]["detour_jump_to"], expected_number(4, surviving))

    def test_delete_clears_jump_when_its_target_is_among_several_removed(self):
        tasks = make_tasks(5)
        tasks[0]["detour_jump_to"] = 3                 # 指向旧下标 2，该步骤将被删除
        surviving = [0, 1, 3, 4]
        remaining = [tasks[i] for i in surviving]
        remap_jump_targets(remaining, {old: new for new, old in enumerate(surviving)})
        self.assertNotIn("detour_jump_to", remaining[0])

    # ---------- switch_cases ----------

    def test_switch_cases_are_remapped(self):
        tasks = make_tasks(3)
        tasks[0]["switch_cases"] = {"a": 3, "b": 1}
        new_order = [2, 1, 0]
        reordered = [tasks[i] for i in new_order]
        remap_jump_targets(reordered, {old: new for new, old in enumerate(new_order)})

        carrier = next(t for t in reordered if t["id"] == "step-0")
        self.assertEqual(carrier["switch_cases"]["a"], expected_number(2, new_order))
        self.assertEqual(carrier["switch_cases"]["b"], expected_number(0, new_order))

    def test_switch_cases_entry_removed_when_target_deleted(self):
        tasks = make_tasks(3)
        tasks[0]["switch_cases"] = {"a": 3}
        surviving = [0, 1]
        remaining = [tasks[i] for i in surviving]
        remap_jump_targets(remaining, {old: new for new, old in enumerate(surviving)})
        self.assertNotIn("a", remaining[0]["switch_cases"])

    # ---------- 覆盖全部已声明字段 ----------

    def test_all_declared_fields_are_remapped(self):
        """JUMP_TARGET_FIELDS 中的每个字段都应被处理（防止将来新增字段时漏改）。"""
        new_order = [1, 2, 0]
        for field in JUMP_TARGET_FIELDS:
            with self.subTest(field=field):
                tasks = make_tasks(3)
                tasks[0][field] = 3                    # 指向旧下标 2
                reordered = [tasks[i] for i in new_order]
                remap_jump_targets(reordered, {old: new for new, old in enumerate(new_order)})
                carrier = next(t for t in reordered if t["id"] == "step-0")
                self.assertEqual(carrier[field], expected_number(2, new_order),
                                 f"{field} 未被重编号")

    # ---------- 边界情况 ----------

    def test_invalid_value_is_left_untouched(self):
        tasks = make_tasks(2)
        tasks[0]["detour_jump_to"] = "不是数字"
        remap_jump_targets(tasks, {0: 0, 1: 1})
        self.assertEqual(tasks[0]["detour_jump_to"], "不是数字")

    def test_none_is_left_untouched(self):
        tasks = make_tasks(2)
        tasks[0]["detour_jump_to"] = None
        remap_jump_targets(tasks, {0: 0, 1: 1})
        self.assertIsNone(tasks[0]["detour_jump_to"])

    def test_out_of_range_target_is_cleared(self):
        """指向不存在步骤的跳转应被清除，而不是留下越界序号。"""
        tasks = make_tasks(2)
        tasks[0]["detour_jump_to"] = 99
        remap_jump_targets(tasks, {0: 0, 1: 1})
        self.assertNotIn("detour_jump_to", tasks[0])

    def test_identity_map_changes_nothing(self):
        tasks = make_tasks(3)
        tasks[0]["detour_jump_to"] = 3
        tasks[1]["timeout_jump_to"] = 1
        remap_jump_targets(tasks, {0: 0, 1: 1, 2: 2})
        self.assertEqual(tasks[0]["detour_jump_to"], 3)
        self.assertEqual(tasks[1]["timeout_jump_to"], 1)

    def test_tasks_without_any_jump_field(self):
        tasks = make_tasks(3)
        remap_jump_targets(tasks, {0: 2, 1: 0, 2: 1})
        for task in tasks:
            for field in JUMP_TARGET_FIELDS:
                self.assertNotIn(field, task)

    def test_empty_task_list(self):
        remap_jump_targets([], {})

    # ---------- 字段清单本身的护栏 ----------

    def test_field_list_covers_currently_used_fields(self):
        """当前存档数据里实际在用的 3 个跳转字段必须在清单内。"""
        for field in ("detour_jump_to", "detour_success_jump_to", "timeout_jump_to"):
            self.assertIn(field, JUMP_TARGET_FIELDS)


if __name__ == "__main__":
    unittest.main()
