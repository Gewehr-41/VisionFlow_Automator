# 任务与预设数据层：定义任务结构并负责 JSON 数据的加载、迁移和保存。
import json
import os
import tempfile
import uuid
from copy import deepcopy

from diagnostics import warn
from nodes import normalize_task
import paths


# 读取失败的提示统一走 diagnostics.warn（写 stderr + 收集起来给界面日志框）。
# 启动脚本用隐藏窗口跑 python，stderr 用户看不到，所以"打印一行"不等于"用户会知道"；
# 真正可见的那条路径是 gui_pyside6 的 _log_pending_warnings。

def make_normal_task(task_id, mode, description, template, *, timeout=5, after_wait=0.25, wait_for="time", wait_timeout=2.5, offset=(0, 0), click=True, required=True):
    return {
        "id": task_id,
        "mode": mode,
        "type": "normal",
        "enabled": True,
        "description": description,
        "template": template,
        "timeout": timeout,
        "click": click,
        "offset": offset,
        "after_wait": after_wait,
        "wait_for": wait_for,
        "wait_timeout": wait_timeout,
        "required": required,
    }


def make_keyboard_move_task(task_id, mode, description, *, move_steps, after_wait=1.0, required=True):
    return {
        "id": task_id,
        "mode": mode,
        "type": "keyboard_move",
        "enabled": True,
        "description": description,
        "template": "rest_room_entry",
        "click": False,
        "after_wait": after_wait,
        "wait_for": "time",
        "wait_timeout": 8,
        "required": required,
        "move_steps": move_steps,
    }


def make_key_press_task(task_id, mode, description, key, *, delay_before=0.0, hold_time=0.1, after_wait=0.2, required=True):
    return {
        "id": task_id,
        "mode": mode,
        "type": "key_press",
        "enabled": True,
        "description": description,
        "template": key,
        "key": key,
        "delay_before": delay_before,
        "hold_time": hold_time,
        "click": False,
        "after_wait": after_wait,
        "wait_for": "time",
        "wait_timeout": 1.0,
        "required": required,
    }


DEFAULT_TASKS = [
    make_normal_task("daily_auto_loop", "daily", "打开自动循环界面", "auto"),
    make_keyboard_move_task(
        "daily_restroom_move",
        "daily",
        "进入3D休息室并移动到指定位置",
        move_steps=[
            {"key": "W", "duration": 1.2},
            {"key": "A", "duration": 0.8},
            {"key": "D", "duration": 0.8},
            {"key": "S", "duration": 0.6},
        ],
    ),
    make_key_press_task("daily_press_e", "daily", "按下 E 键执行交互", "E", hold_time=0.1, after_wait=0.2),
    make_normal_task("daily_start_loop", "daily", "点击循环开始并等待完成", "start_loop", after_wait=1.0),
    make_normal_task("daily_back_to_main_menu_1", "daily", "返回主菜单", "back_to_main_menu", after_wait=0.5),
    make_normal_task("daily_shop", "daily", "进入商店领取每日礼包", "shop_daily", after_wait=0.5),
    make_normal_task("daily_back_to_main_menu_2", "daily", "返回主菜单", "back_to_main_menu", after_wait=0.5),
    make_normal_task("daily_guild", "daily", "进入公会领取每日奖励", "guild_daily", after_wait=0.5),
    make_normal_task("daily_back_to_main_menu_3", "daily", "返回主菜单", "back_to_main_menu", after_wait=0.5),
    make_normal_task("daily_monthly_card", "daily", "进入月卡领取奖励", "monthly_card", after_wait=0.5),
    make_normal_task("daily_back_to_main_menu_4", "daily", "返回主菜单", "back_to_main_menu", after_wait=0.5),
    make_normal_task("side_road_special", "side", "歧路：识别并点击特殊关卡", "side_road"),
]

TASKS_FILE = paths.TASKS_FILE
PRESETS_FILE = paths.PRESETS_FILE
BLUEPRINT_LAYOUT_FILE = paths.BLUEPRINT_LAYOUT_FILE
BLUEPRINT_GRAPH_FILE = paths.BLUEPRINT_GRAPH_FILE


def dump_json_atomic(path, payload):
    """原子写入 JSON：先写同目录临时文件，再 os.replace 覆盖目标。

    避免"直接覆盖写入时崩溃/断电导致 JSON 被截断"的风险。
    临时文件与目标同目录，保证 os.replace 在同盘内是原子操作。
    """
    directory = os.path.dirname(path)
    file_descriptor, temp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    return payload


def _extract_task_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("custom", "tasks", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for key, value in payload.items():
            if key in ("custom", "__deleted__", "__group_metadata__"):
                continue
            if isinstance(value, list):
                return value
    return None


def load_tasks():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            tasks = _extract_task_list(data)
            if tasks is not None:
                return normalize_task_list(tasks)
            warn(f"警告: {TASKS_FILE} 内容不是任务列表，将使用内置默认任务。")
        except Exception as exc:
            warn(f"警告: 读取 {TASKS_FILE} 失败（{exc}），将使用内置默认任务。")
    return deepcopy(DEFAULT_TASKS)


def normalize_task_list(tasks):
    normalized_tasks = [normalize_task(task) for task in tasks]
    used_ids = set()
    duplicate_id_map = {}
    for task in normalized_tasks:
        task_id = str(task.get("id"))
        if task_id not in used_ids:
            used_ids.add(task_id)
            continue
        new_id = str(uuid.uuid4())
        while new_id in used_ids:
            new_id = str(uuid.uuid4())
        duplicate_id_map.setdefault(task_id, []).append(new_id)
        task["id"] = new_id
        used_ids.add(new_id)

    for task in normalized_tasks:
        target_id = task.get("flow_next")
        if target_id in duplicate_id_map:
            task["flow_next"] = duplicate_id_map[target_id][0]
    return normalized_tasks


def save_tasks(tasks):
    return dump_json_atomic(TASKS_FILE, tasks)


TASKS = load_tasks()

TASK_PRESETS = {
    "custom": TASKS,
    "daily": [
        make_normal_task("preset_daily_auto_loop", "daily", "打开自动循环界面", "auto"),
        make_keyboard_move_task(
            "preset_daily_restroom_move",
            "daily",
            "进入3D休息室并移动到指定位置",
            move_steps=[
                {"key": "W", "duration": 1.2},
                {"key": "A", "duration": 0.8},
                {"key": "D", "duration": 0.8},
                {"key": "S", "duration": 0.6},
            ],
        ),
        make_key_press_task("preset_daily_press_e", "daily", "按下 E 键执行交互", "E", hold_time=0.1, after_wait=0.2),
        make_normal_task("preset_daily_start_loop", "daily", "点击循环开始并等待完成", "start_loop", after_wait=1.0),
        make_normal_task("preset_daily_back_home_1", "daily", "返回主菜单", "back_to_main_menu", after_wait=0.5),
        make_normal_task("preset_daily_shop", "daily", "进入商店领取每日礼包", "shop_daily", after_wait=0.5),
        make_normal_task("preset_daily_back_home_2", "daily", "返回主菜单", "back_to_main_menu", after_wait=0.5),
        make_normal_task("preset_daily_guild", "daily", "进入公会领取每日奖励", "guild_daily", after_wait=0.5),
        make_normal_task("preset_daily_back_home_3", "daily", "返回主菜单", "back_to_main_menu", after_wait=0.5),
        make_normal_task("preset_daily_monthly_card", "daily", "进入月卡领取奖励", "monthly_card", after_wait=0.5),
        make_normal_task("preset_daily_back_home_4", "daily", "返回主菜单", "back_to_main_menu", after_wait=0.5),
    ],
    "side": [
        make_normal_task("preset_side_road", "side", "歧路：识别并点击特殊关卡", "side_road"),
    ],
}


def load_deleted_preset_names():
    if not os.path.exists(PRESETS_FILE):
        return set()
    try:
        with open(PRESETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        deleted_names = data.get("__deleted__", []) if isinstance(data, dict) else []
        return {str(name) for name in deleted_names} if isinstance(deleted_names, list) else set()
    except Exception as exc:
        # 静默返回空集合的后果不只是"少了个名单"：删除名单一旦为空，保存时
        # __deleted__ 会被写成 []，此前删掉的预设会重新出现在界面上。
        warn(f"警告: 读取 {PRESETS_FILE} 的删除名单失败（{exc}），已删除的预设可能会重新出现。")
        return set()


DELETED_PRESET_NAMES = load_deleted_preset_names()


def load_presets():
    presets = {name: deepcopy(value) for name, value in TASK_PRESETS.items() if name != "custom"}
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for name in DELETED_PRESET_NAMES:
                    presets.pop(str(name), None)
                for name, preset_value in data.items():
                    if name in ("custom", "__deleted__", "__group_metadata__"):
                        continue
                    if isinstance(preset_value, list):
                        presets[str(name)] = normalize_task_list(preset_value)
                        continue
                    if isinstance(preset_value, dict):
                        task_list = _extract_task_list(preset_value)
                        if task_list is not None:
                            presets[str(name)] = normalize_task_list(task_list)
        except Exception as exc:
            warn(f"警告: 读取 {PRESETS_FILE} 失败（{exc}），用户预设将不会加载。")
    return presets


USER_PRESETS = load_presets()


def load_preset_metadata():
    if not os.path.exists(PRESETS_FILE):
        return {}

    try:
        with open(PRESETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        metadata = data.get("__group_metadata__", {}) if isinstance(data, dict) else {}
        return metadata if isinstance(metadata, dict) else {}
    except Exception as exc:
        warn(f"警告: 读取 {PRESETS_FILE} 的分组元数据失败（{exc}），分组名称与颜色将回退为默认值。")
        return {}


def load_blueprint_layouts():
    if not os.path.exists(BLUEPRINT_LAYOUT_FILE):
        return {}
    try:
        with open(BLUEPRINT_LAYOUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        warn(f"警告: 读取 {BLUEPRINT_LAYOUT_FILE} 失败（{exc}），蓝图的节点位置与缩放将不会恢复。")
        return {}


def save_blueprint_layouts(layouts):
    return dump_json_atomic(BLUEPRINT_LAYOUT_FILE, layouts)


def load_blueprint_graphs():
    if not os.path.exists(BLUEPRINT_GRAPH_FILE):
        return {}
    try:
        with open(BLUEPRINT_GRAPH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        warn(f"警告: 读取 {BLUEPRINT_GRAPH_FILE} 失败（{exc}），蓝图图数据将不会加载。")
        return {}


def save_blueprint_graphs(graphs):
    return dump_json_atomic(BLUEPRINT_GRAPH_FILE, graphs)


PRESET_METADATA = load_preset_metadata()


def save_presets(presets):
    return dump_json_atomic(PRESETS_FILE, presets)


def get_tasks_for_mode(mode_name):
    mode = (mode_name or "custom").strip().lower()
    if mode == "custom":
        return TASKS
    return USER_PRESETS.get(mode, [])
