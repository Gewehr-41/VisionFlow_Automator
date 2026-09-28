# 蓝图节点模型：为任务字典提供统一的节点、连接和校验接口。
"""统一蓝图节点适配层。

任务 JSON 仍然是持久化格式，TaskNode 让执行器和编辑器逐步脱离对字典结构的直接依赖。
"""
from copy import deepcopy
import uuid


def normalize_task(task):
    """把旧版任务字典迁移为节点适配器可识别的最小结构。"""
    normalized = deepcopy(task) if isinstance(task, dict) else {}
    normalized.setdefault("id", str(uuid.uuid4()))
    normalized.setdefault("type", "normal")
    normalized.setdefault("enabled", True)
    if "timeout" not in normalized and "wait_timeout" in normalized:
        normalized["timeout"] = normalized["wait_timeout"]
    if normalized.get("type") == "condition":
        if "condition_templates" not in normalized and normalized.get("condition_template"):
            normalized["condition_templates"] = [normalized["condition_template"]]
        normalized.setdefault("condition_operator", "any")
    return normalized


class Node:
    """蓝图节点的最小统一接口。"""

    def execute(self, context):
        raise NotImplementedError

    def validate(self, context=None):
        return []

    def inputs(self):
        return ("input",)

    def outputs(self):
        return ("output",)


# ---------------------------------------------------------------------------
# 蓝图连接（connections）的「输出端口名 → 任务字段」映射
#
# 图数据（saved_blueprint_graphs.json）用节点 id 记录连接，而任务字典用
# 「flow_next = 目标 id」+「1-based 步骤序号」两种寻址。下面这份映射是
# to_payload / from_payload / fill_missing_connections 三处共用的唯一来源，
# 避免同一个端口在不同函数里映射到不同字段。
# ---------------------------------------------------------------------------

CONNECTION_OUTPUT_FIELDS = {
    "output": "flow_next",
    "true": "condition_true_jump_to",
    "false": "condition_false_jump_to",
    "success": "detour_success_jump_to",
    "failure": "detour_jump_to",
    "timeout": "timeout_jump_to",
    "wait_timeout": "wait_timeout_jump_to",
    "body": "loop_target",
    "exit": "loop_exit_target",
    "triggered": "event_trigger_target",
}


class TaskNode(Node):
    """把旧版任务字典适配为统一节点对象。"""

    def __init__(self, task, executor=None):
        self.task = task
        self._executor = executor

    @property
    def node_id(self):
        return self.task.get("id")

    @property
    def node_type(self):
        return self.task.get("type", "normal")

    def execute(self, context=None):
        if self._executor is None:
            raise RuntimeError("TaskNode 未绑定执行器。")
        context = context or {}
        return self._executor(self.task, **context)

    def validate(self, context=None):
        errors = []
        if not self.node_id:
            errors.append("节点缺少稳定 id。")
        if not self.node_type:
            errors.append("节点缺少 type。")
        return errors

    def inputs(self):
        return ("input",)

    def outputs(self):
        task_outputs = self.task.get("outputs")
        if isinstance(task_outputs, list) and task_outputs:
            return tuple(str(output) for output in task_outputs)
        if self.node_type == "condition":
            return ("true", "false")
        if self.node_type == "switch":
            return tuple((self.task.get("switch_cases") or {}).keys()) + ("default",)
        if self.node_type == "loop":
            return ("body", "exit")
        if self.node_type == "event":
            return ("triggered", "timeout")
        return ("output",)


class NodeGraph:
    """基于节点 ID 的执行图，兼容旧版数字跳转字段。"""

    def __init__(self, tasks, executor=None):
        self.nodes = [task_to_node(task, executor=executor) for task in tasks]
        self.id_to_index = {
            str(node.node_id): index
            for index, node in enumerate(self.nodes)
            if node.node_id is not None
        }

    @classmethod
    def from_payload(cls, payload, executor=None):
        nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
        tasks = [normalize_task(node.get("task", node)) for node in nodes if isinstance(node, dict)]
        graph = cls(tasks, executor=executor)
        graph._native_connections = list(payload.get("connections", [])) if isinstance(payload, dict) else []
        for connection in graph._native_connections:
            source = graph.resolve_id(connection.get("source"))
            target = graph.resolve_id(connection.get("target"))
            if source is None or target is None:
                continue
            output = connection.get("output")
            target_number = target + 1
            field = CONNECTION_OUTPUT_FIELDS.get(output)
            if field == "flow_next":
                graph.nodes[source].task[field] = graph.nodes[target].node_id
            elif field:
                graph.nodes[source].task[field] = target_number
        return graph

    def to_payload(self):
        connections = []
        for node in self.nodes:
            # 端口 → 字段只认 CONNECTION_OUTPUT_FIELDS 这一份，避免与
            # from_payload / fill_missing_connections 漂移。
            for output, field in CONNECTION_OUTPUT_FIELDS.items():
                if field == "flow_next":
                    target_index = self.resolve_id(node.task.get(field))
                else:
                    target_index = self.resolve_number(node.task.get(field))
                if target_index is None:
                    continue
                connections.append({
                    "source": node.node_id,
                    "output": output,
                    "target": self.nodes[target_index].node_id,
                })
        return {
            "version": 1,
            "nodes": [{"id": node.node_id, "type": node.node_type, "task": deepcopy(node.task)} for node in self.nodes],
            "connections": connections,
        }

    def entry_index(self):
        incoming = {
            target_index
            for node in self.nodes
            for target_index in [self.resolve_id(node.task.get("flow_next"))]
            if target_index is not None
        }
        return next((index for index in range(len(self.nodes)) if index not in incoming), 0)

    def resolve_id(self, node_id):
        if node_id is None:
            return None
        return self.id_to_index.get(str(node_id))

    def resolve_number(self, number):
        try:
            target = int(number) - 1
        except (TypeError, ValueError):
            return None
        return target if 0 <= target < len(self.nodes) else None

    def outgoing_targets(self, index):
        if not (0 <= index < len(self.nodes)):
            return []
        task = self.nodes[index].task
        targets = []
        flow_target = self.resolve_id(task.get("flow_next"))
        if flow_target is not None:
            targets.append(flow_target)
        for key in (
            "detour_jump_to", "detour_success_jump_to",
            "condition_true_jump_to", "condition_false_jump_to",
            "switch_default_jump_to", "loop_target", "loop_exit_target",
            "event_timeout_target", "event_trigger_target",
            "timeout_jump_to", "wait_timeout_jump_to",
        ):
            target = self.resolve_number(task.get(key))
            if target is not None:
                targets.append(target)
        for target_number in (task.get("switch_cases") or {}).values():
            target = self.resolve_number(target_number)
            if target is not None:
                targets.append(target)
        return list(dict.fromkeys(targets))

    def reachable_indices(self, entry_index=None):
        reachable = set()
        pending = [self.entry_index() if entry_index is None else entry_index]
        while pending:
            index = pending.pop()
            # 越界守卫：调用方传入的 entry_index 或跳转目标都可能越界，
            # 少了这一步会在 self.nodes[index] 处直接 IndexError。
            if index in reachable or not (0 <= index < len(self.nodes)):
                continue
            reachable.add(index)
            pending.extend(self.outgoing_targets(index))
            if not self.nodes[index].task.get("flow_next") and not self.nodes[index].task.get("flow_next_disabled"):
                if index + 1 < len(self.nodes):
                    pending.append(index + 1)
        return reachable

    def validate(self):
        errors = []
        for index, node in enumerate(self.nodes):
            errors.extend(f"节点 {index + 1}: {error}" for error in node.validate())
            for target in self.outgoing_targets(index):
                if target == index:
                    errors.append(f"节点 {index + 1} 不能连接到自身。")
        return list(dict.fromkeys(errors))


def task_to_node(task, executor=None):
    return TaskNode(normalize_task(task), executor=executor)


def tasks_to_nodes(tasks, executor=None):
    return [task_to_node(deepcopy(task), executor=executor) for task in tasks]


def fill_missing_connections(tasks, payload):
    """用图数据里的 id 连接补齐任务中「为空」的跳转字段，返回补齐的条数。

    背景
    ----
    `saved_blueprint_graphs.json` 里的 connections 用**节点 id** 寻址，
    比任务字典里的 1-based 步骤序号更抗重排（插入/删除/拖动都不会让它错位）；
    但图文件终究是历史快照，若直接以它为准去重写跳转字段，就会把用户在
    任务侧后来的改动覆盖掉。因此这里只做「补空」，规则严格限定为：

      - 只在源任务的对应字段为 `None` 或 `""` 时写入；
      - 已有值的字段一律不动；
      - 不删除任何字段，不触碰跳转以外的任何字段；
      - 源/目标 id 解析不到、端口名未知、自连接一律跳过。

    参数 `tasks` 是任务字典列表（就地修改），`payload` 是图数据字典。
    """
    connections = payload.get("connections") if isinstance(payload, dict) else None
    if not isinstance(connections, list) or not connections:
        return 0
    if not isinstance(tasks, list):
        return 0

    graph = NodeGraph(tasks)  # 只借用它的 id -> 下标解析
    repaired = 0
    for connection in connections:
        if not isinstance(connection, dict):
            continue
        field = CONNECTION_OUTPUT_FIELDS.get(connection.get("output"))
        if field is None:
            continue
        source = graph.resolve_id(connection.get("source"))
        target = graph.resolve_id(connection.get("target"))
        if source is None or target is None or source == target:
            continue
        task = tasks[source]
        if not isinstance(task, dict):
            continue
        if task.get(field) not in (None, ""):
            continue  # 只补空，绝不覆盖
        if field == "flow_next":
            target_id = graph.nodes[target].node_id
            if target_id is None:
                continue
            task[field] = target_id
        else:
            task[field] = target + 1
        repaired += 1
    return repaired


# ---------------------------------------------------------------------------
# 步骤序号重编号（跳转目标的唯一维护点）
#
# 背景：分支跳转（condition / switch / loop / event 的字段，以及所有步骤共用的
# 迂回与超时字段）都用「1-based 步骤序号」寻址。任何改变步骤下标的操作——
# 删除、拖动排序、应用蓝图重排——都必须同步重编号这些字段，否则跳转会静默
# 指向错误的步骤（序号仍在合法范围内，现有校验也发现不了）。
#
# 此前这段逻辑散落在多处且覆盖度不一致（删除 4 个字段 / 重排 2 个字段 /
# 拖动 0 个字段）。现在统一到本函数，作为单一维护点。
# ---------------------------------------------------------------------------

# 所有以 1-based 步骤序号寻址的标量字段。
JUMP_TARGET_FIELDS = (
    "detour_jump_to",
    "detour_success_jump_to",
    "timeout_jump_to",
    "wait_timeout_jump_to",
    "condition_true_jump_to",
    "condition_false_jump_to",
    "switch_default_jump_to",
    "loop_target",
    "loop_exit_target",
    "event_timeout_target",
    "event_trigger_target",
)

# 每个跳转字段在界面上的人话名字。
#
# 这是**唯一**一份「跳转字段 -> 显示名」映射：界面的蓝图校验此前各自手工维护
# 字段列表（一处 7 个、一处 9 个），于是同一个新字段总是漏掉几处，而漏掉不会
# 报错、只会让校验静默看不见那条跳转。
#
# 键必须与 JUMP_TARGET_FIELDS 完全一致；由
# tests/test_graph_connections.py::JumpFieldCoverageTests 强制校验。
JUMP_FIELD_LABELS = {
    "detour_jump_to": "未识别",
    "detour_success_jump_to": "识别成功",
    "timeout_jump_to": "超时",
    "wait_timeout_jump_to": "等待超时",
    "condition_true_jump_to": "条件成立",
    "condition_false_jump_to": "条件不成立",
    "switch_default_jump_to": "Switch 默认",
    "loop_target": "循环体",
    "loop_exit_target": "循环退出",
    "event_timeout_target": "事件超时",
    "event_trigger_target": "事件触发",
}


def remap_jump_targets(tasks, index_map):
    """按 index_map（旧下标 → 新下标）重编号所有跳转目标。

    - 键是旧下标，值是它在新列表中的下标；1-based 字段存的是「下标 + 1」。
    - index_map 中不存在的旧下标表示该步骤已被删除，指向它的跳转会被清除。
    - 无法解析为整数的取值保持原样，不做修改。
    """
    for task in tasks:
        for field in JUMP_TARGET_FIELDS:
            raw_value = task.get(field)
            if raw_value is None:
                continue
            try:
                old_index = int(raw_value) - 1
            except (TypeError, ValueError):
                continue
            new_index = index_map.get(old_index)
            if new_index is None:
                task.pop(field, None)
            else:
                task[field] = new_index + 1

        cases = task.get("switch_cases")
        if isinstance(cases, dict):
            for case_key, raw_value in list(cases.items()):
                try:
                    old_index = int(raw_value) - 1
                except (TypeError, ValueError):
                    continue
                new_index = index_map.get(old_index)
                if new_index is None:
                    cases.pop(case_key, None)
                else:
                    cases[case_key] = new_index + 1
