# -*- coding: utf-8 -*-
"""步骤片段：把常用的一组步骤（可以带分组）存下来，之后一键插到任意预设里（F13 / 本轮增强）。

数据结构（v2，兼容 v1）
-----------------------
`saved_presets.json` 的保留键 `__snippets__`：

    {片段名: {"version": 2,
              "tasks": [步骤...],
              "groups": {临时组 id: {"name": 名称, "color": 颜色, "parent": 父组或 None}},
              "group_order": [临时组 id...]}}

v1 的老数据是纯步骤列表 `{片段名: [步骤...]}`，读取时按"没有分组"处理，仍然可以插入。

跳转怎么处理（最容易做错的地方）
--------------------------------
步骤里的跳转字段是 **1-based 绝对编号**，跨预设复制会指到别处，所以：
- 保存：指向片段内部的跳转改写成**片段内相对编号**，指向外部的丢弃；
- 插入：先把插入点之后现有步骤的跳转整体后移，再把片段的相对编号换算成绝对编号；
  `flow_next` 按 id 寻址，插入时重新生成 id 并保留片段内部连线。
分组同理：保存时记录分组之间的父子关系（片段外的父组记为 None），插入时生成**新的组 id**
并把父组映射到新的 id，避免和现有分组撞名。
"""
from copy import deepcopy
import uuid

from nodes import remap_jump_targets

SNIPPET_VERSION = 2
_INTERNAL_PREFIX = "_"
_GROUP_DEFAULT = "group_default"


# 片段不该带走的字段：
#   · `_` 开头的运行时字段（如 _outer_step_number）
#   · 蓝图**布局/路由**字段：折线转折点坐标是按"这条边连到谁"算的，跨预设复制后连接对象变了，
#     旧折线会画错位置；节点折叠状态也只对原图有意义（颜色/注释属于样式，保留）
_DROP_WHEN_SAVING = ("blueprint_bends", "blueprint_collapsed")


def _strip_internal(task):
    """去掉运行时内部字段与蓝图布局字段。"""
    for key in list(task.keys()):
        if str(key).startswith(_INTERNAL_PREFIX) or str(key) in _DROP_WHEN_SAVING:
            task.pop(key, None)
    return task


def normalize_payload(snippet):
    """把 v1（纯列表）/ v2（带分组）统一成 (tasks, groups, group_order)。"""
    if isinstance(snippet, dict):
        tasks = [task for task in (snippet.get("tasks") or []) if isinstance(task, dict)]
        groups = snippet.get("groups") if isinstance(snippet.get("groups"), dict) else {}
        order = snippet.get("group_order") if isinstance(snippet.get("group_order"), list) else list(groups)
        return tasks, dict(groups), [str(gid) for gid in order]
    if isinstance(snippet, list):
        return [task for task in snippet if isinstance(task, dict)], {}, []
    return [], {}, []


def snippet_group_count(snippet):
    _tasks, groups, _order = normalize_payload(snippet)
    return len(groups)


def _expand_selected_groups(group_metadata, group_ids):
    """选中的组 + 全部后代组（片段要把子组一起带走）。"""
    children = (group_metadata or {}).get("children", {}) or {}
    result = []
    stack = [str(gid) for gid in group_ids]
    seen = set()
    while stack:
        group_id = stack.pop(0)
        if not group_id or group_id in seen or group_id == _GROUP_DEFAULT:
            continue
        seen.add(group_id)
        result.append(group_id)
        stack.extend(str(child) for child in (children.get(group_id) or []))
    return result


def extract_snippet(tasks, indices, group_metadata=None, group_ids=()):
    """把选中的步骤 / 分组做成片段（v2 载荷）。

    - `indices`：选中的步骤（0-based 下标）
    - `group_ids`：选中的分组，会把组内全部步骤（含子组）一起带上
    - 目标里出现过的分组都会记录名称/颜色/父子关系；父组在片段外时记为 None
    """
    group_metadata = group_metadata if isinstance(group_metadata, dict) else {}
    names = group_metadata.get("names", {}) or {}
    colors = group_metadata.get("colors", {}) or {}
    parents = group_metadata.get("parents", {}) or {}
    order = group_metadata.get("order", []) or []

    selected_groups = set(_expand_selected_groups(group_metadata, group_ids))
    selected = {int(index) for index in indices if 0 <= int(index) < len(tasks)}
    for index, task in enumerate(tasks):
        group_id = str(task.get("group_id") or _GROUP_DEFAULT)
        if group_id in selected_groups:
            selected.add(index)
    if not selected and not selected_groups:
        return {"version": SNIPPET_VERSION, "tasks": [], "groups": {}, "group_order": []}

    ordered = sorted(selected)
    snippet_tasks = [_strip_internal(deepcopy(tasks[index])) for index in ordered]
    # 跳转：旧全集下标 → 片段内下标；指向片段外的会被清除
    remap_jump_targets(snippet_tasks, {old: new for new, old in enumerate(ordered)})

    # 片段里出现的分组：步骤自身的组 + 选中组（含后代）+ 它们的祖先链
    involved = {str(task.get("group_id") or _GROUP_DEFAULT) for task in snippet_tasks}
    involved |= selected_groups
    for group_id in list(involved):
        current = str(parents.get(group_id)) if parents.get(group_id) else None
        seen = set()
        while current and current != _GROUP_DEFAULT and current not in seen:
            seen.add(current)
            involved.add(current)
            current = str(parents.get(current)) if parents.get(current) else None
    involved.discard(_GROUP_DEFAULT)

    groups = {}
    for group_id in involved:
        parent = parents.get(group_id)
        parent = str(parent) if parent else None
        # 组名回退顺序：元数据名 → 组内步骤的 group_name → 通用名
        # （直接回退到 group_id 会让界面出现 "group_xxxx" 这种看不懂的名字）
        member_name = ""
        for task in snippet_tasks:
            if str(task.get("group_id") or "") == str(group_id) and task.get("group_name"):
                member_name = str(task["group_name"])
                break
        groups[group_id] = {
            "name": str(names.get(group_id) or member_name or "分组"),
            "color": str(colors.get(group_id) or ""),
            # 片段外的父组不算数：插入时统一挂到目标分组（或根层）
            "parent": parent if parent in involved else None,
        }
    group_order = [str(gid) for gid in order if str(gid) in involved]
    group_order += [gid for gid in groups if gid not in group_order]
    return {"version": SNIPPET_VERSION, "tasks": snippet_tasks, "groups": groups,
            "group_order": group_order}


def insert_snippet(target_tasks, snippet, position=None, parent_group_id=None):
    """把片段插到 `position`（0-based）处。

    返回 `(新任务列表, 新分组列表)`；`新分组列表` 里每项是
    `{"id": 新组 id, "name", "color", "parent": 新父组 id 或 None}`，调用方据此写进分组元数据。
    """
    tasks = [deepcopy(task) for task in (target_tasks or [])]
    snippet_tasks, snippet_groups, snippet_order = normalize_payload(snippet)
    if not snippet_tasks and not snippet_groups:
        return tasks, []
    if snippet_tasks and snippet_groups:
        # 片段里既然有步骤，就不再为"没有任何步骤的组"创建空壳组（它们通常只是保存时
        # 顺带选中的空组/失效引用）。祖先组如果是被需要的，会在下面 used 链里保留。
        used = {str(task.get("group_id")) for task in snippet_tasks if task.get("group_id")}
        keep = set()
        for group_id in used:
            current = group_id
            seen = set()
            while current and current in snippet_groups and current not in seen:
                seen.add(current)
                keep.add(current)
                parent = (snippet_groups.get(current) or {}).get("parent")
                current = str(parent) if parent else ""
        snippet_order = [group_id for group_id in snippet_order if group_id in keep]
        snippet_groups = {group_id: info for group_id, info in snippet_groups.items() if group_id in keep}
    if position is None:
        position = len(tasks)
    position = max(0, min(int(position), len(tasks)))

    # ① 现有步骤的跳转后移
    remap_jump_targets(tasks, {
        old_index: (old_index + len(snippet_tasks) if old_index >= position else old_index)
        for old_index in range(len(tasks))
    })

    # ② 分组：给片段里的每个组生成新 id，并重映射父子关系
    group_id_map = {}
    for old_id in snippet_order:
        group_id_map[str(old_id)] = f"group_{uuid.uuid4().hex[:8]}"
    new_groups = []
    for old_id in snippet_order:
        info = snippet_groups.get(str(old_id)) or {}
        old_parent = info.get("parent")
        new_parent = group_id_map.get(str(old_parent)) if old_parent else None
        if new_parent is None and parent_group_id:
            # 片段内的顶层组 / 指向片段外的父组：统一挂到插入点所在的分组下面
            # （这样"把带组的片段插进某个组里"才会真的嵌套在组内）
            new_parent = str(parent_group_id)
        new_groups.append({
            "id": group_id_map[str(old_id)],
            "name": str(info.get("name") or "分组"),
            "color": str(info.get("color") or ""),
            "parent": new_parent,
        })
    group_by_id = {item["id"]: item for item in new_groups}

    # ③ 步骤副本：新 id / 内部 flow_next / 分组映射 / 相对跳转 → 绝对
    copies = []
    id_map = {}
    for task in snippet_tasks:
        new_id = str(uuid.uuid4())
        id_map[str(task.get("id"))] = new_id
        task["id"] = new_id
        _strip_internal(task)
        old_group = task.get("group_id")
        if old_group is None and parent_group_id:
            # 插进某个组时，片段里没有分组的步骤也归属到这个组
            task["group_id"] = str(parent_group_id)
            task["group_name"] = ""
        if old_group is not None:
            mapped_group = group_id_map.get(str(old_group))
            if mapped_group is None:
                # 片段外的分组（理论上不会出现）：落到插入目标分组或根层
                if parent_group_id:
                    task["group_id"] = str(parent_group_id)
                else:
                    task.pop("group_id", None)
                    task.pop("group_name", None)
            else:
                task["group_id"] = mapped_group
                task["group_name"] = group_by_id[mapped_group]["name"]
        copies.append(task)
    for task in copies:
        flow_target = task.get("flow_next")
        if flow_target is not None:
            mapped = id_map.get(str(flow_target))
            if mapped is None:
                task.pop("flow_next", None)
            else:
                task["flow_next"] = mapped
    remap_jump_targets(copies, {offset: position + offset for offset in range(len(copies))})

    tasks[position:position] = copies
    return tasks, new_groups


def snippet_summary(snippet):
    """一行摘要，用于日志与菜单。"""
    snippet_tasks, snippet_groups, _order = normalize_payload(snippet)
    labels = []
    for task in snippet_tasks:
        labels.append(str(task.get("description") or task.get("template") or task.get("type", "步骤")))
    shown = " / ".join(labels[:3]) + (" …" if len(labels) > 3 else "")
    head = f"{len(labels)} 步" if labels else "空片段"
    if snippet_groups:
        head += f"，{len(snippet_groups)} 个分组"
    return f"{head}：{shown}" if labels else head
