import json
import os
from copy import deepcopy
from nodes import normalize_task


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


def make_drag_task(task_id, mode, description, *, start=(0, 0), end=(100, 100), duration=0.25, after_wait=0.2, required=True):
    return {
        "id": task_id,
        "mode": mode,
        "type": "drag",
        "enabled": True,
        "description": description,
        "template": "drag",
        "start_x": start[0],
        "start_y": start[1],
        "end_x": end[0],
        "end_y": end[1],
        "duration": duration,
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

TASKS_FILE = os.path.join(os.path.dirname(__file__), "saved_tasks.json")
PRESETS_FILE = os.path.join(os.path.dirname(__file__), "saved_presets.json")
BLUEPRINT_LAYOUT_FILE = os.path.join(os.path.dirname(__file__), "saved_blueprint_layouts.json")
BLUEPRINT_GRAPH_FILE = os.path.join(os.path.dirname(__file__), "saved_blueprint_graphs.json")


def load_tasks():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return [normalize_task(task) for task in data]
        except Exception:
            pass
    return deepcopy(DEFAULT_TASKS)


def save_tasks(tasks):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    return tasks


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
    except Exception:
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
                for name, preset_tasks in data.items():
                    if name not in ("custom", "__deleted__") and isinstance(preset_tasks, list):
                        presets[str(name)] = [normalize_task(task) for task in preset_tasks]
        except Exception:
            pass
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
    except Exception:
        return {}


def load_blueprint_layouts():
    if not os.path.exists(BLUEPRINT_LAYOUT_FILE):
        return {}
    try:
        with open(BLUEPRINT_LAYOUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_blueprint_layouts(layouts):
    with open(BLUEPRINT_LAYOUT_FILE, "w", encoding="utf-8") as f:
        json.dump(layouts, f, ensure_ascii=False, indent=2)
    return layouts


def load_blueprint_graphs():
    if not os.path.exists(BLUEPRINT_GRAPH_FILE):
        return {}
    try:
        with open(BLUEPRINT_GRAPH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_blueprint_graphs(graphs):
    with open(BLUEPRINT_GRAPH_FILE, "w", encoding="utf-8") as f:
        json.dump(graphs, f, ensure_ascii=False, indent=2)
    return graphs


PRESET_METADATA = load_preset_metadata()


def save_presets(presets):
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)
    return presets


def get_tasks_for_mode(mode_name):
    mode = (mode_name or "custom").strip().lower()
    if mode == "custom":
        return TASKS
    return USER_PRESETS.get(mode, [])
