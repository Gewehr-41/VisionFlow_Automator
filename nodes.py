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
            field_by_output = {
                "output": "flow_next",
                "true": "condition_true_jump_to",
                "false": "condition_false_jump_to",
                "success": "detour_success_jump_to",
                "failure": "detour_jump_to",
                "timeout": "timeout_jump_to",
                "body": "loop_target",
                "exit": "loop_exit_target",
                "triggered": "event_trigger_target",
            }
            field = field_by_output.get(output)
            if field == "flow_next":
                graph.nodes[source].task[field] = graph.nodes[target].node_id
            elif field:
                graph.nodes[source].task[field] = target_number
        return graph

    def to_payload(self):
        connections = []
        for index, node in enumerate(self.nodes):
            target_index = self.resolve_id(node.task.get("flow_next"))
            if target_index is not None:
                connections.append({"source": node.node_id, "output": "output", "target": self.nodes[target_index].node_id})
            for key, output in (("condition_true_jump_to", "true"), ("condition_false_jump_to", "false"), ("detour_success_jump_to", "success"), ("detour_jump_to", "failure"), ("timeout_jump_to", "timeout")):
                target_index = self.resolve_number(node.task.get(key))
                if target_index is not None:
                    connections.append({"source": node.node_id, "output": output, "target": self.nodes[target_index].node_id})
            for key, output in (("loop_target", "body"), ("loop_exit_target", "exit"), ("event_trigger_target", "triggered")):
                target_index = self.resolve_number(node.task.get(key))
                if target_index is not None:
                    connections.append({"source": node.node_id, "output": output, "target": self.nodes[target_index].node_id})
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
            "event_timeout_target", "timeout_jump_to",
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
            if index in reachable:
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
