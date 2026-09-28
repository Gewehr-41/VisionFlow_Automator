"""nodes.fill_missing_connections 的测试：用图数据补齐缺失连接。

这是 `saved_blueprint_graphs.json` 的**唯一读取路径**。它的安全边界是本文件的
重点：只补「为空」的跳转字段，绝不覆盖已有值、绝不删除、绝不触碰其它字段。

connections 用「节点 id」寻址，比任务字典里的 1-based 步骤序号更抗重排；
但图文件是历史快照，所以只把它当作补全来源，而不是权威数据源。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import nodes


def make_tasks():
    return [
        {"id": "a", "type": "normal", "template": "t", "timeout": 5},
        {"id": "b", "type": "normal", "template": "t", "timeout": 5},
        {"id": "c", "type": "normal", "template": "t", "timeout": 5},
    ]


class FillMissingConnectionsTests(unittest.TestCase):
    # ---------- 基本行为 ----------

    def test_fills_empty_flow_next_with_target_node_id(self):
        tasks = make_tasks()
        tasks[0]["flow_next"] = "c"
        payload = nodes.NodeGraph(tasks).to_payload()
        tasks[0].pop("flow_next")

        repaired = nodes.fill_missing_connections(tasks, payload)

        self.assertEqual(repaired, 1)
        self.assertEqual(tasks[0]["flow_next"], "c")

    def test_fills_empty_numeric_jump_with_one_based_index(self):
        tasks = make_tasks()
        tasks[0]["timeout_jump_to"] = 3
        payload = nodes.NodeGraph(tasks).to_payload()
        tasks[0].pop("timeout_jump_to")

        repaired = nodes.fill_missing_connections(tasks, payload)

        self.assertEqual(repaired, 1)
        self.assertEqual(tasks[0]["timeout_jump_to"], 3)

    def test_counts_every_repair(self):
        tasks = make_tasks()
        tasks[0]["flow_next"] = "b"
        tasks[0]["timeout_jump_to"] = 3
        tasks[1]["condition_true_jump_to"] = 1
        payload = nodes.NodeGraph(tasks).to_payload()
        for task in tasks:
            for field in nodes.JUMP_TARGET_FIELDS:
                if field != "wait_timeout_jump_to":
                    task.pop(field, None)
        tasks[0].pop("flow_next", None)

        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 3)

    # ---------- 安全边界：只补不覆盖 ----------

    def test_never_overwrites_existing_flow_next(self):
        tasks = make_tasks()
        tasks[0]["flow_next"] = "c"
        payload = nodes.NodeGraph(tasks).to_payload()
        tasks[0]["flow_next"] = "b"  # 用户后来改成了 b

        repaired = nodes.fill_missing_connections(tasks, payload)

        self.assertEqual(repaired, 0)
        self.assertEqual(tasks[0]["flow_next"], "b", "已有值绝不能被旧快照覆盖")

    def test_never_overwrites_existing_numeric_jump(self):
        tasks = make_tasks()
        tasks[0]["timeout_jump_to"] = 3
        payload = nodes.NodeGraph(tasks).to_payload()
        tasks[0]["timeout_jump_to"] = 2

        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 0)
        self.assertEqual(tasks[0]["timeout_jump_to"], 2)

    def test_empty_string_counts_as_empty(self):
        tasks = make_tasks()
        tasks[0]["flow_next"] = "c"
        payload = nodes.NodeGraph(tasks).to_payload()
        tasks[0]["flow_next"] = ""

        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 1)
        self.assertEqual(tasks[0]["flow_next"], "c")

    def test_zero_is_a_real_value_and_is_kept(self):
        tasks = make_tasks()
        tasks[0]["timeout_jump_to"] = 3
        payload = nodes.NodeGraph(tasks).to_payload()
        tasks[0]["timeout_jump_to"] = 0

        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 0)
        self.assertEqual(tasks[0]["timeout_jump_to"], 0)

    def test_does_not_touch_other_fields(self):
        tasks = make_tasks()
        tasks[0]["flow_next"] = "c"
        payload = nodes.NodeGraph(tasks).to_payload()
        tasks[0].pop("flow_next")
        before = {k: v for k, v in tasks[0].items()}

        nodes.fill_missing_connections(tasks, payload)

        for key, value in before.items():
            self.assertEqual(tasks[0][key], value, f"字段 {key} 不应被改动")
        self.assertEqual(set(tasks[0]) - set(before), {"flow_next"})

    def test_does_not_delete_anything(self):
        tasks = make_tasks()
        tasks[0]["flow_next"] = "c"
        payload = nodes.NodeGraph(tasks).to_payload()
        snapshot = [dict(task) for task in tasks]

        nodes.fill_missing_connections(tasks, payload)

        self.assertEqual(len(tasks), len(snapshot))
        for index, task in enumerate(tasks):
            for key, value in snapshot[index].items():
                self.assertEqual(task[key], value)

    # ---------- 抗重排（这个功能的真正理由） ----------

    def test_survives_reordering_because_ids_are_stable(self):
        tasks = make_tasks()
        tasks[0]["flow_next"] = "c"
        tasks[0]["timeout_jump_to"] = 3
        payload = nodes.NodeGraph(tasks).to_payload()

        # 真正换序为 c, a, b 并清空 a 的跳转（模拟重排后 1-based 序号已失效）
        tasks[:] = [tasks[2], tasks[0], tasks[1]]
        for task in tasks:
            task.pop("flow_next", None)
            for field in nodes.JUMP_TARGET_FIELDS:
                task.pop(field, None)

        repaired = nodes.fill_missing_connections(tasks, payload)

        by_id = {task["id"]: task for task in tasks}
        self.assertEqual([task["id"] for task in tasks], ["c", "a", "b"])
        self.assertEqual(repaired, 2)
        self.assertEqual(by_id["a"]["flow_next"], "c", "flow_next 存的是 id，与顺序无关")
        self.assertEqual(by_id["a"]["timeout_jump_to"], 1, "c 现在是第 1 步")

    # ---------- 异常与脏数据 ----------

    def test_unknown_output_port_is_ignored(self):
        tasks = make_tasks()
        payload = {"connections": [{"source": "a", "output": "没这个端口", "target": "c"}]}
        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 0)

    def test_dangling_ids_are_ignored(self):
        tasks = make_tasks()
        payload = {"connections": [
            {"source": "a", "output": "output", "target": "不存在"},
            {"source": "不存在", "output": "output", "target": "c"},
        ]}
        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 0)

    def test_self_connection_is_ignored(self):
        tasks = make_tasks()
        payload = {"connections": [{"source": "a", "output": "output", "target": "a"}]}
        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 0)
        self.assertNotIn("flow_next", tasks[0])

    def test_missing_target_id_for_flow_next_is_skipped(self):
        tasks = [{"type": "normal"}, {"id": "b", "type": "normal"}]
        payload = {"connections": [{"source": "b", "output": "output", "target": "b"}]}
        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 0)

    def test_degenerate_payloads_return_zero(self):
        tasks = make_tasks()
        for payload in (None, {}, {"connections": None}, {"connections": []},
                        {"connections": "不是列表"}, [], "字符串", 42):
            with self.subTest(payload=payload):
                self.assertEqual(nodes.fill_missing_connections(tasks, payload), 0)

    def test_non_dict_connection_entries_are_skipped(self):
        tasks = make_tasks()
        payload = {"connections": [None, "x", 7, [], {"source": "a", "output": "output", "target": "c"}]}
        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 1)
        self.assertEqual(tasks[0]["flow_next"], "c")

    def test_non_list_tasks_returns_zero(self):
        self.assertEqual(nodes.fill_missing_connections(None, {"connections": [{}]}), 0)

    def test_connection_with_unknown_port_does_not_break_valid_ones(self):
        tasks = make_tasks()
        payload = {"connections": [
            {"source": "a", "output": "??", "target": "c"},
            {"source": "a", "output": "output", "target": "b"},
        ]}
        self.assertEqual(nodes.fill_missing_connections(tasks, payload), 1)
        self.assertEqual(tasks[0]["flow_next"], "b")

    # ---------- 与真实写入路径对接 ----------

    def test_round_trip_through_to_payload(self):
        """to_payload 写出的图数据，清空字段后能被完整补回。"""
        tasks = make_tasks()
        tasks[0]["flow_next"] = "c"
        tasks[1]["timeout_jump_to"] = 3
        payload = nodes.NodeGraph(tasks).to_payload()
        original = [(task.get("flow_next"), task.get("timeout_jump_to")) for task in tasks]
        for task in tasks:
            task.pop("flow_next", None)
            task.pop("timeout_jump_to", None)

        nodes.fill_missing_connections(tasks, payload)

        restored = [(task.get("flow_next"), task.get("timeout_jump_to")) for task in tasks]
        self.assertEqual(restored, original)

    def test_from_payload_and_fill_agree_on_ports(self):
        """from_payload 与 fill_missing_connections 必须共享同一份端口映射。"""
        for output, field in nodes.CONNECTION_OUTPUT_FIELDS.items():
            with self.subTest(output=output):
                tasks = make_tasks()
                source_field = "flow_next" if field == "flow_next" else field
                tasks[0][source_field] = "c" if field == "flow_next" else 3
                payload = nodes.NodeGraph(tasks).to_payload()
                self.assertTrue(
                    any(c.get("output") == output for c in payload["connections"]),
                    f"to_payload 应输出端口 {output}",
                )
                tasks[0].pop(source_field, None)
                self.assertEqual(
                    nodes.fill_missing_connections(tasks, payload), 1,
                    f"端口 {output} 应能补回字段 {field}",
                )


class JumpFieldCoverageTests(unittest.TestCase):
    """跳转字段的各个维护点必须覆盖同一批字段。

    背景：`wait_timeout_jump_to` 曾被漏掉两处 —— 引擎（`main.py`）在读它、
    `JUMP_TARGET_FIELDS` 也含它（所以重编号是对的），但 `outgoing_targets`
    （校验与可达性）和 `CONNECTION_OUTPUT_FIELDS`（图数据）都没有它。
    漏字段不会报错，只会让校验/图数据静默看不见这条跳转，所以这里用不变量锁住。
    """

    # 图数据目前**有意不表达**的两个字段：它们只属于所有者要求"暂不修改"的
    # 节点类型（switch / event）。显式列在这里，是为了让缺口可见而不是被忘记。
    GRAPH_NOT_EXPRESSED = {"switch_default_jump_to", "event_timeout_target"}

    def test_outgoing_targets_covers_every_declared_jump_field(self):
        for field in nodes.JUMP_TARGET_FIELDS:
            with self.subTest(field=field):
                tasks = [{"id": "a", "type": "normal"},
                         {"id": "b", "type": "normal"},
                         {"id": "c", "type": "normal"}]
                tasks[0][field] = 3  # 指向第 3 步（下标 2）
                graph = nodes.NodeGraph(tasks)
                self.assertIn(
                    2, graph.outgoing_targets(0),
                    f"{field} 被 JUMP_TARGET_FIELDS 收录，却没被 outgoing_targets 计入",
                )

    def test_outgoing_targets_covers_flow_next_and_switch_cases(self):
        tasks = [{"id": "a", "type": "normal"}, {"id": "b", "type": "normal"}]
        tasks[0]["flow_next"] = "b"
        self.assertIn(1, nodes.NodeGraph(tasks).outgoing_targets(0))

        tasks = [{"id": "a", "type": "switch"}, {"id": "b", "type": "normal"}]
        tasks[0]["switch_cases"] = {"case1": 2}
        self.assertIn(1, nodes.NodeGraph(tasks).outgoing_targets(0))

    def test_connection_ports_cover_all_graph_expressed_fields(self):
        expected = (set(nodes.JUMP_TARGET_FIELDS) - self.GRAPH_NOT_EXPRESSED) | {"flow_next"}
        self.assertEqual(
            set(nodes.CONNECTION_OUTPUT_FIELDS.values()), expected,
            "端口映射与 JUMP_TARGET_FIELDS 不再一致（新增/漏掉字段时请同步更新）",
        )

    def test_every_expressed_field_round_trips_through_the_graph(self):
        for output, field in nodes.CONNECTION_OUTPUT_FIELDS.items():
            with self.subTest(field=field):
                tasks = [{"id": "a", "type": "normal"},
                         {"id": "b", "type": "normal"},
                         {"id": "c", "type": "normal"}]
                tasks[0][field] = "c" if field == "flow_next" else 3
                payload = nodes.NodeGraph(tasks).to_payload()
                tasks[0].pop(field, None)
                self.assertEqual(
                    nodes.fill_missing_connections(tasks, payload), 1,
                    f"{field}（端口 {output}）应能由图数据补回",
                )
                expected = "c" if field == "flow_next" else 3
                self.assertEqual(tasks[0][field], expected)

    # ---------- 显示名映射（界面校验的字段列表由它派生） ----------

    def test_jump_field_labels_cover_exactly_the_declared_fields(self):
        self.assertEqual(
            set(nodes.JUMP_FIELD_LABELS), set(nodes.JUMP_TARGET_FIELDS),
            "JUMP_FIELD_LABELS 的键必须与 JUMP_TARGET_FIELDS 完全一致，"
            "否则界面的蓝图校验会漏字段（漏字段不报错，只静默看不见）",
        )

    def test_jump_field_labels_are_non_empty(self):
        for field, label in nodes.JUMP_FIELD_LABELS.items():
            with self.subTest(field=field):
                self.assertTrue(str(label).strip(), f"{field} 的显示名不能为空")


class ReachableIndicesTests(unittest.TestCase):
    """可达性计算：必须认得全部跳转字段，且对非法入口不崩。"""

    def _chain_then_branch(self, branch_field):
        """a -flow_next-> b -（branch_field）-> c，且 b 禁用顺序落到下一步。"""
        return [
            {"id": "a", "type": "normal", "flow_next": "b"},
            {"id": "b", "type": "normal", branch_field: 3, "flow_next_disabled": True},
            {"id": "c", "type": "normal"},
        ]

    def test_every_declared_jump_field_extends_reachability(self):
        """仅靠某一个跳转字段可达的步骤，必须算作可达（覆盖全部 11 个字段）。"""
        for field in nodes.JUMP_TARGET_FIELDS:
            with self.subTest(field=field):
                tasks = self._chain_then_branch(field)
                self.assertEqual(
                    nodes.NodeGraph(tasks).reachable_indices(), {0, 1, 2},
                    f"仅靠 {field} 可达的步骤被算成了不可达",
                )

    def test_fall_through_reaches_next_step(self):
        tasks = [{"id": "a", "type": "normal"}, {"id": "b", "type": "normal"}]
        self.assertEqual(nodes.NodeGraph(tasks).reachable_indices(), {0, 1})

    def test_flow_next_disabled_stops_fall_through(self):
        tasks = [
            {"id": "a", "type": "normal", "flow_next_disabled": True},
            {"id": "b", "type": "normal"},
        ]
        self.assertEqual(nodes.NodeGraph(tasks).reachable_indices(), {0})

    def test_out_of_range_entry_returns_empty_instead_of_raising(self):
        graph = nodes.NodeGraph([{"id": "a"}, {"id": "b"}])
        self.assertEqual(graph.reachable_indices(999), set())

    def test_negative_entry_returns_empty_instead_of_walking_from_the_end(self):
        """旧实现会把 -1 当成最后一个节点继续遍历，静默给出错误结果。"""
        graph = nodes.NodeGraph([{"id": "a"}, {"id": "b"}, {"id": "c"}])
        self.assertEqual(graph.reachable_indices(-1), set())

    def test_empty_graph_is_safe(self):
        """空图以前会 IndexError：entry_index() 返回 0，旧实现直接取 self.nodes[0]。"""
        self.assertEqual(nodes.NodeGraph([]).reachable_indices(), set())


if __name__ == "__main__":
    unittest.main()
