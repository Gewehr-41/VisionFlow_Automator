# -*- coding: utf-8 -*-
"""步骤片段：把常用的一组步骤存下来，之后一键插到任意预设里（F13）。

数据放在哪
----------
`saved_presets.json` 的保留键 `__snippets__`：`{片段名: [步骤字典, ...]}`。
- 它**全局共享**（不随预设切换）——片段的用途就是"在 A 预设里攒、在 B 预设里用"。
- 用保留键而不是新开一个数据文件：不增加 path/打包/部署的面，和 `__group_metadata__`、
  `__fallback__` 是同一套模式（`tasks.load_presets` 会跳过它，不会被当成预设）。

跳转怎么处理（这是片段最容易做错的地方）
----------------------------------------
步骤里的跳转字段是**1-based 绝对编号**（`condition_true_jump_to` 等），直接跨预设复制会
指到别处。所以：
- **保存**：用 `nodes.remap_jump_targets` 把"指向片段内部"的跳转改写成**片段内相对编号**，
  指向片段外部的跳转直接丢弃（它在新预设里没有意义）；
- **插入**：先把插入点之后所有现有步骤的跳转整体后移，再把片段的相对编号换算成新列表里的
  绝对编号；`flow_next` 是按 id 寻址的，所以只重生成 id 并保留片段内部的连接。

这两个函数都是纯数据操作（不依赖 Qt），便于单测。
"""
from copy import deepcopy
import uuid

from nodes import remap_jump_targets

# 片段里不该保留的运行时/内部字段
_INTERNAL_PREFIX = "_"


def _strip_internal(task):
    """去掉以 `_` 开头的内部字段（如 `_outer_step_number`，那是运行时临时塞的）。"""
    for key in [key for key in task if str(key).startswith(_INTERNAL_PREFIX)]:
        task.pop(key, None)
    return task


def extract_snippet(tasks, indices):
    """从任务列表里取出若干步骤，做成可复用的片段（深拷贝，不影响原数据）。

    `indices` 是任务列表里的 0-based 下标；内部跳转改写成"片段内相对编号"，
    指向片段外部的跳转字段会被**丢弃**（有日志提示的必要交给调用方）。
    """
    selected = sorted({int(index) for index in indices if 0 <= int(index) < len(tasks)})
    if not selected:
        return []
    snippet = [_strip_internal(deepcopy(tasks[index])) for index in selected]
    # 旧下标（全集） → 片段内下标；不在片段里的步骤映射不到，指向它们的跳转会被清除
    index_map = {old_index: position for position, old_index in enumerate(selected)}
    remap_jump_targets(snippet, index_map)
    return snippet


def insert_snippet(target_tasks, snippet, position=None):
    """把片段插到 `position`（0-based）处，返回**新的**任务列表。

    - `position` 为空时追加到末尾
    - 插入点之后现有步骤的跳转整体后移 `len(snippet)`
    - 片段的相对跳转换算成新列表的绝对编号
    - 片段内部按 id 寻址的 `flow_next` 保持连接（重新生成 id 并改引用）
    """
    tasks = [deepcopy(task) for task in (target_tasks or [])]
    snippet = [deepcopy(task) for task in (snippet or []) if isinstance(task, dict)]
    if not snippet:
        return tasks
    if position is None:
        position = len(tasks)
    position = max(0, min(int(position), len(tasks)))

    # ① 现有步骤的跳转后移（插入点及其之后）
    shift_map = {
        old_index: (old_index + len(snippet) if old_index >= position else old_index)
        for old_index in range(len(tasks))
    }
    remap_jump_targets(tasks, shift_map)

    # ② 片段副本：新 id + 内部 flow_next 引用重映射
    copies = []
    id_map = {}
    for task in snippet:
        new_id = str(uuid.uuid4())
        id_map[str(task.get("id"))] = new_id
        task["id"] = new_id
        copies.append(_strip_internal(task))
    for task in copies:
        flow_target = task.get("flow_next")
        if flow_target is not None:
            mapped = id_map.get(str(flow_target))
            if mapped is None:
                # 片段保存时已丢弃外部连线；这里再兜一层，避免指向不存在 id
                task.pop("flow_next", None)
            else:
                task["flow_next"] = mapped

    # ③ 片段的相对编号 → 绝对编号
    relative_map = {offset: position + offset for offset in range(len(copies))}
    remap_jump_targets(copies, relative_map)

    tasks[position:position] = copies
    return tasks


def snippet_summary(snippet):
    """一行摘要，用于日志与菜单（`3 步：点击开始 / 等待加载 / …`）。"""
    labels = []
    for task in snippet or []:
        labels.append(str(task.get("description") or task.get("template") or task.get("type", "步骤")))
    shown = " / ".join(labels[:3]) + (" …" if len(labels) > 3 else "")
    return f"{len(labels)} 步：{shown}" if labels else "空片段"
