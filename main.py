import time
import tkinter as tk
import uuid
from tkinter import messagebox

import numpy as np
import pyautogui

import config
from config import ICON_DIR, THRESHOLD, SHOW_PREVIEW, DEFAULT_TIMEOUT, DEFAULT_POLL_INTERVAL, USE_MULTI_SCALE, SCALE_RANGE, SCALE_STEP
from core.screen import capture_screen, to_global_position, get_window_rect
from core.template_match import load_templates, match_all_templates
from core.input import click_position, click_global_position, drag_position
from tasks import TASKS
from nodes import NodeGraph, TaskNode


templates = load_templates(ICON_DIR)
_active_pause_flag = None
_single_step_active = False


def get_task_threshold(task=None):
    """读取步骤匹配阈值；未设置时使用 config.py 中的全局默认值。"""
    value = task.get("threshold") if isinstance(task, dict) else None
    try:
        return float(config.THRESHOLD if value in (None, "") else value)
    except (TypeError, ValueError):
        return float(config.THRESHOLD)


def interruptible_sleep(duration, stop_flag=None):
    """等待指定时长，并在收到停止信号时立即返回是否已停止。"""
    duration = max(0.0, float(duration))
    if stop_flag is None:
        time.sleep(duration)
        return False
    deadline = time.monotonic() + duration
    while True:
        if stop_flag.is_set():
            return True
        pause_flag = _active_pause_flag
        if pause_flag is not None and pause_flag.is_set() and not _single_step_active:
            stop_flag.wait(0.1)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if stop_flag.wait(min(remaining, 0.1)):
            return True


def reload_templates():
    """重新加载图标目录中的模板，使新绑定的截图立即可用于识别。"""
    global templates
    templates = load_templates(ICON_DIR)
    return templates


def normalize_template_names(value):
    """把单个模板或逗号分隔的模板列表统一转换为列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(normalize_template_names(item))
        return out
    return [str(value).strip()]


def get_task_timeout(task, fallback=DEFAULT_TIMEOUT):
    """读取步骤超时，兼容旧配置中的 wait_timeout。"""
    value = task.get("timeout")
    if value is None:
        value = task.get("wait_timeout", fallback)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def find_first_match(results, template_names):
    """在多个候选模板中，返回置信度最高的命中。"""
    best_name = None
    best_center = None
    best_conf = -1.0
    for template_name in template_names:
        center, conf = results.get(template_name, (None, -1.0))
        if center is not None and conf > best_conf:
            best_name = template_name
            best_center = center
            best_conf = conf
    return best_name, best_center, best_conf


def find_leftmost_match(results, template_names):
    """在多个候选模板中，返回最左侧的命中；同一横坐标优先置信度高者。"""
    matches = []
    for template_name in template_names:
        center, conf = results.get(template_name, (None, -1.0))
        if center is not None:
            matches.append((center[0], -conf, template_name, center, conf))
    if not matches:
        return None, None, -1.0
    _, _, best_name, best_center, best_conf = min(matches)
    return best_name, best_center, best_conf


def wait_until_template_disappears(template_name, timeout=DEFAULT_TIMEOUT, poll_interval=DEFAULT_POLL_INTERVAL, stop_flag=None, threshold=THRESHOLD):
    """等待模板消失。"""
    start = time.time()
    while True:
        screen_img = capture_screen()
        results = match_all_templates(screen_img, templates, threshold, USE_MULTI_SCALE, SCALE_RANGE, SCALE_STEP)
        center, _ = results.get(template_name, (None, -1.0))
        if center is None:
            return True
        if timeout > 0 and (time.time() - start) > timeout:
            return False
        if interruptible_sleep(poll_interval, stop_flag):
            return False


def wait_until_template_appears(template_name, timeout=DEFAULT_TIMEOUT, poll_interval=DEFAULT_POLL_INTERVAL, search_rect=None, stop_flag=None, threshold=THRESHOLD):
    """等待模板出现。"""
    start = time.time()
    while True:
        screen_img = capture_screen()
        results = match_all_templates(screen_img, templates, threshold, USE_MULTI_SCALE, SCALE_RANGE, SCALE_STEP, search_rect=search_rect)
        center, conf = results.get(template_name, (None, -1.0))
        if center is not None:
            return center, conf
        if timeout > 0 and (time.time() - start) > timeout:
            return None, None
        if interruptible_sleep(poll_interval, stop_flag):
            return None, None


def wait_until_any_template_appears(template_names, timeout=DEFAULT_TIMEOUT, poll_interval=DEFAULT_POLL_INTERVAL, search_rect=None, stop_flag=None, threshold=THRESHOLD):
    """等待任一模板出现，并返回匹配到的模板名。"""
    template_names = normalize_template_names(template_names)
    if not template_names:
        return None, None, -1.0

    start = time.time()
    while True:
        screen_img = capture_screen()
        results = match_all_templates(screen_img, templates, threshold, USE_MULTI_SCALE, SCALE_RANGE, SCALE_STEP, search_rect=search_rect)
        name, center, conf = find_first_match(results, template_names)
        if center is not None:
            return name, center, conf
        if timeout > 0 and (time.time() - start) > timeout:
            return None, None, -1.0
        if interruptible_sleep(poll_interval, stop_flag):
            return None, None, -1.0


def wait_for_step_result(task, template_name=None, next_template_names=None, timeout=None, poll_interval=DEFAULT_POLL_INTERVAL, log_callback=None, stop_flag=None):
    """等待点击后的结果：只有在用户配置的 wait_for 条件达成后才继续下一步。固定时间不再作为继续条件。"""
    wait_mode = task.get("wait_for", "time")
    if wait_mode in (None, "", "time"):
        return True

    effective_timeout = get_task_timeout(task) if timeout is None else float(timeout)
    start_time = time.time()
    next_names = normalize_template_names(next_template_names or task.get("next_template") or task.get("next_templates"))
    baseline = capture_screen()
    screen_changed = False

    while True:
        current = capture_screen()
        results = match_all_templates(current, templates, get_task_threshold(task), USE_MULTI_SCALE, SCALE_RANGE, SCALE_STEP, search_rect=task.get("next_match_rect") or task.get("next_search_rect"))
        diff = float(np.mean(np.abs(current.astype(np.int16) - baseline.astype(np.int16))))
        if diff > 2.0:
            screen_changed = True

        if wait_mode == "disappear" and template_name:
            center, _ = results.get(template_name, (None, -1.0))
            if center is None:
                return True
        elif wait_mode == "next_appear" and next_names:
            _, next_center, _ = find_first_match(results, next_names)
            if next_center is not None:
                return True
        elif wait_mode == "change_then_appear":
            if screen_changed and next_names:
                _, next_center, _ = find_first_match(results, next_names)
                if next_center is not None:
                    return True
        else:
            if template_name:
                center, _ = results.get(template_name, (None, -1.0))
                if center is None:
                    return True
            if next_names:
                _, next_center, _ = find_first_match(results, next_names)
                if next_center is not None:
                    return True
            if diff > 2.0:
                return True

        if effective_timeout > 0 and time.time() - start_time > effective_timeout:
            log(f"  步骤结果等待超过 {effective_timeout:g} 秒，继续下一步。", log_callback)
            return False

        if interruptible_sleep(poll_interval, stop_flag):
            return False


def show_complete_message():
    """弹出结束提示。"""
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo("脚本执行完成", "所有步骤已完成，脚本已停止运行。")
    root.destroy()


def log(msg, log_callback=None):
    """统一日志输出，支持 GUI 回调。"""
    print(msg)
    if log_callback:
        log_callback(msg)


def execute_stage_farm_task(task, stop_flag=None, log_callback=None):
    """刷活动/主线：识别未完成关卡 -> 点击 -> 退出关卡 -> 继续扫描 -> 若页面全完成则拖动列表继续查找。支持多个候选关卡样式和多个二级界面。"""
    stage_templates = normalize_template_names(task.get("stage_templates") or task.get("template"))
    if not stage_templates:
        raise ValueError("stage_farm 任务必须至少配置一个未完成关卡模板。")
    next_templates = normalize_template_names(task.get("next_templates") or task.get("next_template"))
    direction = task.get("direction", 1)
    drag_distance = task.get("drag_distance", 220)
    drag_wait = task.get("drag_wait", 0.8)
    max_rounds = task.get("max_rounds", 12)
    round_count = 0
    current_window = get_window_rect() or {"left": 0, "top": 0, "width": 1920, "height": 1080}
    template_label = ", ".join(stage_templates)

    log(f"\n>>> 高级任务: {task.get('description', template_label)}", log_callback)

    while True:
        if stop_flag is not None and stop_flag.is_set():
            log("用户中止脚本执行。", log_callback)
            return False

        screen_img = capture_screen()
        results = match_task_templates(task, screen_img, stage_templates)
        match_name, center, conf = find_first_match(results, stage_templates)

        if center is not None:
            log(f"  识别到未完成关卡 '{match_name}'，置信度: {conf:.2f}，坐标: {center}", log_callback)
            if task.get("click", True):
                offset = task.get("offset", (0, 0))
                recorded_click = resolve_click_position(task)
                if recorded_click is not None:
                    screen_x, screen_y = recorded_click
                else:
                    screen_x, screen_y = to_global_position(center[0], center[1])
                click_global_position(screen_x, screen_y, offset)
                click_source = "记录点击点" if recorded_click is not None else "识别中心"
                log(f"  已点击{click_source}: ({screen_x + offset[0]}, {screen_y + offset[1]})", log_callback)
            wait_for_step_result(task, template_name=match_name, next_template_names=next_templates, log_callback=log_callback, stop_flag=stop_flag)
            if task.get("wait_for") == "next_appear" and next_templates:
                next_name, next_center, next_conf = wait_until_any_template_appears(
                    next_templates,
                    timeout=get_task_timeout(task, 12),
                    search_rect=task.get("next_match_rect") or task.get("next_search_rect"),
                    stop_flag=stop_flag,
                    threshold=get_task_threshold(task),
                )
                if next_center is not None:
                    log(f"  检测到下一步 UI '{next_name}'，置信度 {next_conf:.2f}，坐标 {next_center}", log_callback)
                    if task.get("next_click", True):
                        next_screen_x, next_screen_y = to_global_position(next_center[0], next_center[1])
                        click_global_position(next_screen_x, next_screen_y, task.get("next_offset", (0, 0)))
                        log(f"  已点击下一步 UI 坐标: ({next_screen_x + task.get('next_offset', (0, 0))[0]}, {next_screen_y + task.get('next_offset', (0, 0))[1]})", log_callback)
            return True

        if round_count >= max_rounds:
            log(f"  已在 {max_rounds} 次拖动后仍未找到任一候选关卡模板 ({template_label})，认为当前列表已扫描完毕。", log_callback)
            return True

        start_x = current_window["left"] + current_window["width"] * 0.5
        start_y = current_window["top"] + current_window["height"] * 0.75
        end_x = start_x + drag_distance * direction
        end_y = start_y
        log(f"  未发现未完成关卡，开始拖动列表方向={direction}，距={drag_distance}，轮次 {round_count + 1}/{max_rounds}", log_callback)
        if not drag_position(start_x, start_y, end_x, end_y, duration=0.25, stop_flag=stop_flag):
            return False
        if interruptible_sleep(drag_wait, stop_flag):
            return False
        round_count += 1
        direction *= -1
        if interruptible_sleep(0.1, stop_flag):
            return False


def resolve_click_position(task, center=None):
    """优先使用识别到的匹配中心，否则回退到任务配置的固定点击坐标。"""
    if center is not None:
        return to_global_position(center[0], center[1])

    click_position = task.get("click_position") or task.get("fallback_position")
    if click_position is not None:
        if isinstance(click_position, dict):
            x = click_position.get("x")
            y = click_position.get("y")
            if x is not None and y is not None:
                return int(x), int(y)
        elif isinstance(click_position, (list, tuple)) and len(click_position) >= 2:
            return int(click_position[0]), int(click_position[1])

    click_x = task.get("click_x")
    click_y = task.get("click_y")
    if click_x is not None and click_y is not None:
        return int(click_x), int(click_y)

    return None


def resolve_search_rect(task):
    """返回任务的匹配矩形区域（window-local 坐标），如果未配置则返回 None。"""
    rect = task.get("match_rect") or task.get("search_rect")
    if rect is not None:
        if isinstance(rect, dict):
            left = rect.get("left")
            top = rect.get("top")
            right = rect.get("right")
            bottom = rect.get("bottom")
            if all(value is not None for value in (left, top, right, bottom)):
                return (int(left), int(top), int(right), int(bottom))
        elif isinstance(rect, (list, tuple)) and len(rect) >= 4:
            return (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    return None


def resolve_search_rects(task):
    """返回任务配置的多个匹配矩形，并兼容旧的单矩形配置。"""
    rects = task.get("match_rects")
    if isinstance(rects, list):
        resolved = []
        for rect in rects:
            parsed = resolve_search_rect({"match_rect": rect})
            if parsed is not None:
                resolved.append(parsed)
        if resolved:
            return resolved
    rect = resolve_search_rect(task)
    return [rect] if rect is not None else []


def match_in_expanding_rect(screen_img, match_templates, search_rect, threshold=THRESHOLD):
    """先匹配框选区域，未命中时以中心为基准逐次扩大到整张截图。"""
    screen_height, screen_width = screen_img.shape[:2]
    left, top, right, bottom = map(int, search_rect)
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top

    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    half_width = max(0.5, (right - left) / 2.0)
    half_height = max(0.5, (bottom - top) / 2.0)

    while True:
        current_rect = (
            max(0, int(center_x - half_width)),
            max(0, int(center_y - half_height)),
            min(screen_width, int(center_x + half_width)),
            min(screen_height, int(center_y + half_height)),
        )
        results = match_all_templates(
            screen_img,
            match_templates,
            threshold,
            USE_MULTI_SCALE,
            SCALE_RANGE,
            SCALE_STEP,
            search_rect=current_rect,
        )
        if any(center is not None for center, _ in results.values()):
            return results

        if current_rect == (0, 0, screen_width, screen_height):
            return results

        half_width = min(screen_width, half_width * 2.0)
        half_height = min(screen_height, half_height * 2.0)


def task_match_region(task):
    """仅当任务配置了识别区域时返回匹配范围。"""
    match_rect = resolve_search_rect(task)
    if match_rect is not None:
        left, top, right, bottom = match_rect
        center = ((left + right) / 2.0, (top + bottom) / 2.0)
        radius = max(1.0, float(max(right - left, bottom - top)) * 0.5)
        return (int(center[0]), int(center[1])), radius

    return None, None


def match_task_templates(task, screen_img, template_names=None):
    """仅在配置的识别区域内匹配，否则进行全屏匹配。"""
    names = normalize_template_names(template_names) if template_names is not None else None
    if names:
        match_templates = {name: templates[name] for name in names if name in templates}
    else:
        match_templates = templates

    if not match_templates:
        return {}

    search_rects = resolve_search_rects(task)
    if len(search_rects) > 1:
        merged_results = {}
        for search_rect in search_rects:
            results = match_all_templates(
                screen_img,
                match_templates,
                get_task_threshold(task),
                USE_MULTI_SCALE,
                SCALE_RANGE,
                SCALE_STEP,
                search_rect=search_rect,
            )
            for name, result in results.items():
                current = merged_results.get(name)
                if current is None or result[1] > current[1]:
                    merged_results[name] = result
        return merged_results
    if search_rects:
        return match_in_expanding_rect(screen_img, match_templates, search_rects[0], get_task_threshold(task))

    center, radius = task_match_region(task)
    return match_all_templates(
        screen_img,
        match_templates,
        get_task_threshold(task),
        USE_MULTI_SCALE,
        SCALE_RANGE,
        SCALE_STEP,
        search_center=center,
        search_radius=radius,
    )


def click_template_name(template_name, offset=(0, 0), timeout=DEFAULT_TIMEOUT, fallback_position=None, log_callback=None, poll_interval=DEFAULT_POLL_INTERVAL, stop_flag=None, threshold=THRESHOLD):
    """持续扫描目标模板；记录点击点只用于点击，四元组参数才用于限制识别区域。"""
    start_time = time.time()
    while True:
        if stop_flag is not None and stop_flag.is_set():
            return False
        if timeout > 0 and (time.time() - start_time) > timeout:
            break

        screen_img = capture_screen()
        if isinstance(fallback_position, (list, tuple)) and len(fallback_position) >= 4:
            match_templates = {template_name: templates[template_name]} if template_name in templates else {}
            results = match_in_expanding_rect(screen_img, match_templates, fallback_position, threshold)
        else:
            results = match_all_templates(screen_img, templates, threshold, USE_MULTI_SCALE, SCALE_RANGE, SCALE_STEP)
        center, conf = results.get(template_name, (None, -1.0))
        if center is not None:
            click_target = None
            if isinstance(fallback_position, (list, tuple)) and len(fallback_position) == 2:
                click_target = resolve_click_position({"click_position": fallback_position})
            if click_target is not None:
                screen_x, screen_y = click_target
            else:
                screen_x, screen_y = to_global_position(center[0], center[1])
            click_global_position(screen_x, screen_y, offset)
            click_source = "记录点击点" if click_target is not None else "识别中心"
            log(f"  已点击{click_source} '{template_name}'，置信度 {conf:.2f}，坐标: ({screen_x + offset[0]}, {screen_y + offset[1]})", log_callback)
            return True

        if interruptible_sleep(poll_interval, stop_flag):
            return False

    return False


def execute_keyboard_move_task(task, stop_flag=None, log_callback=None):
    """执行每日 3D 休息室的键盘移动步骤。"""
    move_steps = task.get("move_steps", [])
    delay_before = max(0.0, float(task.get("delay_before", 0.0)))
    after_wait = max(0.0, float(task.get("after_wait", 0.0)))
    log(f"\n>>> 3D移动任务: {task.get('description', '移动操作')}", log_callback)

    if not move_steps:
        log("  未配置移动步骤，跳过。", log_callback)
        return True

    if delay_before > 0:
        log(f"  执行前延时 {delay_before} 秒。", log_callback)
        if interruptible_sleep(delay_before, stop_flag):
            return False

    for step in move_steps:
        if stop_flag is not None and stop_flag.is_set():
            log("用户中止脚本执行。", log_callback)
            return False
        key = str(step.get("key", "W")).upper()
        duration = float(step.get("duration", 1.0))
        log(f"  按键: {key} 持续 {duration} 秒", log_callback)
        pyautogui.keyDown(key)
        if interruptible_sleep(duration, stop_flag):
            pyautogui.keyUp(key)
            return False
        pyautogui.keyUp(key)

    wait_for_step_result(task, template_name=task.get("template"), next_template_names=task.get("next_template"), log_callback=log_callback, stop_flag=stop_flag)
    if after_wait > 0:
        log(f"  移动完成后等待 {after_wait} 秒。", log_callback)
        if interruptible_sleep(after_wait, stop_flag):
            return False
    return True


def execute_key_press_task(task, stop_flag=None, log_callback=None):
    """执行单次按键步骤。"""
    key = str(task.get("key") or task.get("template") or "E").upper()
    hold_time = float(task.get("hold_time", 0.1))
    delay_before = max(0.0, float(task.get("delay_before", 0.0)))
    log(f"\n>>> 按键任务: {task.get('description', f'按键 {key}')}", log_callback)

    if stop_flag is not None and stop_flag.is_set():
        log("用户中止脚本执行。", log_callback)
        return False

    if delay_before > 0:
        log(f"  延时 {delay_before} 秒后按键。", log_callback)
        if interruptible_sleep(delay_before, stop_flag):
            return False
        if stop_flag is not None and stop_flag.is_set():
            log("用户中止脚本执行。", log_callback)
            return False

    pyautogui.keyDown(key)
    if interruptible_sleep(max(0.05, hold_time), stop_flag):
        pyautogui.keyUp(key)
        return False
    pyautogui.keyUp(key)
    after_wait = max(0.0, float(task.get("after_wait", 0.2)))
    if after_wait > 0:
        log(f"  按键完成后等待 {after_wait} 秒。", log_callback)
        if interruptible_sleep(after_wait, stop_flag):
            return False
    wait_for_step_result(task, template_name=task.get("template"), log_callback=log_callback, stop_flag=stop_flag)
    return True


def execute_drag_task(task, stop_flag=None, log_callback=None):
    """执行拖拽步骤：从起点拖到终点。"""
    start_x = float(task.get("start_x", 0))
    start_y = float(task.get("start_y", 0))
    end_x = float(task.get("end_x", 100))
    end_y = float(task.get("end_y", 100))
    duration = float(task.get("duration", 0.25))
    log(f"\n>>> 拖曳任务: {task.get('description', '拖拽操作')}", log_callback)

    if stop_flag is not None and stop_flag.is_set():
        log("用户中止脚本执行。", log_callback)
        return False

    if not drag_position(start_x, start_y, end_x, end_y, duration=duration, stop_flag=stop_flag):
        return False
    wait_for_step_result(task, template_name=task.get("template"), next_template_names=task.get("next_template"), log_callback=log_callback, stop_flag=stop_flag)
    return True


def execute_reward_claim_task(task, stop_flag=None, log_callback=None):
    """执行奖励领取类任务：入口 -> 确认领取 -> 返回主菜单。"""
    log(f"\n>>> 奖励领取任务: {task.get('description', '领取奖励')}", log_callback)

    entry_template = task.get("template")
    confirm_template = task.get("reward_confirm_template")
    back_template = task.get("back_to_menu_template")
    fallback_position = resolve_search_rect(task) or resolve_click_position(task)

    if entry_template:
        if not click_template_name(entry_template, offset=task.get("offset", (0, 0)), timeout=get_task_timeout(task, 5), fallback_position=fallback_position, log_callback=log_callback, stop_flag=stop_flag, threshold=get_task_threshold(task)):
            if task.get("required", True):
                raise RuntimeError(f"奖励入口模板 '{entry_template}' 未出现，脚本停止。")
            return False
        wait_for_step_result(task, template_name=entry_template, log_callback=log_callback, stop_flag=stop_flag)

    if confirm_template:
        if not click_template_name(confirm_template, offset=(0, 0), timeout=get_task_timeout(task, 5), fallback_position=resolve_search_rect(task) or resolve_click_position({**task, "click_position": task.get("confirm_click_position") or task.get("click_position")}), log_callback=log_callback, stop_flag=stop_flag, threshold=get_task_threshold(task)):
            if task.get("required", True):
                raise RuntimeError(f"确认奖励模板 '{confirm_template}' 未出现，脚本停止。")
            return False
        wait_for_step_result(task, template_name=confirm_template, log_callback=log_callback, stop_flag=stop_flag)

    if back_template:
        if not click_template_name(back_template, offset=(0, 0), timeout=get_task_timeout(task, 5), fallback_position=resolve_search_rect(task) or resolve_click_position({**task, "click_position": task.get("back_click_position") or task.get("click_position")}), log_callback=log_callback, stop_flag=stop_flag, threshold=get_task_threshold(task)):
            if task.get("required", True):
                raise RuntimeError(f"返回主菜单模板 '{back_template}' 未出现，脚本停止。")
            return False
        wait_for_step_result(task, template_name=back_template, log_callback=log_callback, stop_flag=stop_flag)

    return True


def execute_event_entry_task(task, stop_flag=None, log_callback=None):
    """活动入口任务：识别并点击活动入口，并等待画面状态或新识别结果。"""
    tpl_name = task.get("template")
    if not tpl_name:
        return True
    clicked = click_template_name(tpl_name, offset=task.get("offset", (0, 0)), timeout=get_task_timeout(task, 8), fallback_position=resolve_search_rect(task) or resolve_click_position(task), log_callback=log_callback, stop_flag=stop_flag, threshold=get_task_threshold(task))
    if clicked:
        wait_for_step_result(task, template_name=tpl_name, next_template_names=task.get("next_template"), log_callback=log_callback, stop_flag=stop_flag)
    return clicked


def execute_click_until_gone_task(task, stop_flag=None, log_callback=None):
    """目标图片未出现时反复点击，识别到目标图片后停止。"""
    template_names = normalize_template_names(task.get("templates") or task.get("template"))
    stop_on_change = bool(task.get("stop_on_change", False))
    if not template_names and not stop_on_change:
        raise ValueError("持续点击步骤必须配置绑定图片。")

    click_interval = max(0.01, float(task.get("click_interval", 0.5)))
    stop_delay = max(0.0, float(task.get("stop_delay", 0.0)))
    timeout = float(task.get("timeout", DEFAULT_TIMEOUT))
    baseline_screen = capture_screen() if stop_on_change else None
    start_time = time.time()
    next_click_time = None
    template_label = ", ".join(template_names)
    log(f"\n>>> 持续点击任务: {task.get('description', template_label)}", log_callback)

    def continue_clicking_after_success():
        if stop_delay <= 0:
            return True
        click_position = resolve_click_position(task)
        if click_position is None:
            raise ValueError("持续点击步骤必须先记录点击点。")
        log(f"  已识别到目标，继续点击 {stop_delay} 秒后停止。", log_callback)
        stop_at = time.time() + stop_delay
        scheduled_click_time = time.time()
        while time.time() < stop_at:
            if stop_flag is not None and stop_flag.is_set():
                return False
            wait_time = scheduled_click_time - time.time()
            if wait_time > 0 and interruptible_sleep(wait_time, stop_flag):
                return False
            screen_x, screen_y = click_position
            click_global_position(screen_x, screen_y, task.get("offset", (0, 0)))
            log(f"  识别后继续点击记录位置: ({screen_x}, {screen_y})", log_callback)
            scheduled_click_time += click_interval
        return True

    while True:
        if stop_flag is not None and stop_flag.is_set():
            log("用户中止脚本执行。", log_callback)
            return False
        if timeout > 0 and time.time() - start_time > timeout:
            if task.get("continue_after_timeout", False):
                log(f"  持续点击图片 '{template_label}' 超时，按设置继续下一步骤。", log_callback)
                return True
            raise RuntimeError(f"持续点击直到识别图片 '{template_label}' 超时，脚本停止。")

        screen_img = capture_screen()
        if stop_on_change:
            difference = float(np.mean(np.abs(screen_img.astype(np.int16) - baseline_screen.astype(np.int16))))
            if difference > 2.0:
                log("  检测到画面变化，持续点击完成。", log_callback)
                return continue_clicking_after_success()
            center = None
        else:
            results = match_task_templates(task, screen_img, template_names)
            matched_name, center, confidence = find_first_match(results, template_names)
        if center is not None:
            if not continue_clicking_after_success():
                return False
            log(f"  已识别到 '{matched_name}'，持续点击完成。", log_callback)
            return True

        click_position = resolve_click_position(task)
        if click_position is None:
            raise ValueError("持续点击步骤必须先记录点击点。")
        wait_time = (next_click_time - time.time()) if next_click_time is not None else 0.0
        if wait_time > 0 and interruptible_sleep(wait_time, stop_flag):
            return False
        screen_x, screen_y = click_position
        click_started_at = time.time()
        click_global_position(screen_x, screen_y, task.get("offset", (0, 0)))
        next_click_time = click_started_at + click_interval
        log(f"  未识别到 '{template_label}'，点击记录位置: ({screen_x}, {screen_y})", log_callback)

        if stop_on_change:
            after_click_screen = capture_screen()
            difference = float(np.mean(np.abs(after_click_screen.astype(np.int16) - baseline_screen.astype(np.int16))))
            if difference > 2.0:
                log("  检测到点击后的画面变化，持续点击完成。", log_callback)
                return continue_clicking_after_success()

def execute_detour_task(task, stop_flag=None, log_callback=None):
    """执行当前外流程任务的迂回子流程，不允许子步骤再次进入迂回。"""
    detour_steps = task.get("detour_steps") or []
    if not detour_steps:
        return True

    log(f"\n>>> 迂回任务: {task.get('description', '迂回操作')}", log_callback)
    for detour in detour_steps:
        if stop_flag is not None and stop_flag.is_set():
            log("用户中止脚本执行。", log_callback)
            return False
        result = execute_task(detour, stop_flag=stop_flag, log_callback=log_callback, allow_detour=False)
        if result is False:
            return False
    return True


def execute_delay_task(task, stop_flag=None, log_callback=None):
    """执行蓝图中的独立延迟节点。"""
    duration = max(0.0, float(task.get("duration", task.get("after_wait", 1.0))))
    log(f"\n>>> 延迟节点: {duration:g} 秒", log_callback)
    return not interruptible_sleep(duration, stop_flag)


def execute_condition_task(task, stop_flag=None, log_callback=None):
    """执行条件节点：根据多个模板结果执行 AND、OR 或 NOT。"""
    template_names = normalize_template_names(task.get("condition_templates") or task.get("condition_template") or task.get("template"))
    if not template_names:
        raise ValueError("条件节点必须配置 condition_template。")
    screen_img = capture_screen()
    results = match_all_templates(screen_img, templates, get_task_threshold(task), USE_MULTI_SCALE, SCALE_RANGE, SCALE_STEP, search_rect=task.get("search_rect"))
    matches = [results.get(name, (None, -1.0))[0] is not None for name in template_names]
    operator = str(task.get("condition_operator", "any" if len(matches) > 1 else "all")).lower()
    if operator == "all":
        matched = all(matches)
    elif operator == "not":
        matched = not any(matches)
    else:
        matched = any(matches)
    if task.get("condition_invert") and operator != "not":
        matched = not matched
    _, _, confidence = find_first_match(results, template_names)
    result_label = "成立" if matched else "不成立"
    log(f"\n>>> 条件节点: {', '.join(template_names)} -> {result_label} (置信度 {confidence:.2f})", log_callback)
    target = task.get("condition_true_jump_to" if matched else "condition_false_jump_to")
    if target is None:
        return True
    return {"next_step": int(target)}


def execute_switch_task(task, stop_flag=None, log_callback=None):
    """执行静态值选择节点，返回匹配 case 或默认出口。"""
    value = str(task.get("switch_value", ""))
    cases = task.get("switch_cases") or {}
    target = cases.get(value, task.get("switch_default_jump_to"))
    log(f"\n>>> Switch 节点: 值={value!r}，出口={target or '默认顺序'}", log_callback)
    if target is None:
        return True
    return {"next_step": int(target)}


def execute_event_task(task, stop_flag=None, log_callback=None):
    """等待事件模板出现后继续蓝图流程。"""
    template_name = str(task.get("event_template") or task.get("template") or "").strip()
    if not template_name:
        raise ValueError("事件节点必须配置 event_template。")
    timeout = max(0.0, float(task.get("event_timeout", 30.0)))
    log(f"\n>>> 事件节点: 等待 '{template_name}' 出现", log_callback)
    center, confidence = wait_until_template_appears(
        template_name,
        timeout=timeout,
        search_rect=task.get("search_rect"),
        stop_flag=stop_flag,
        threshold=get_task_threshold(task),
    )
    if center is not None:
        log(f"  事件已触发，置信度 {confidence:.2f}。", log_callback)
        return True
    log("  事件等待超时。", log_callback)
    failure_target = task.get("event_timeout_target")
    if failure_target is not None:
        return {"next_step": int(failure_target)}
    return False


def execute_task(task, stop_flag=None, log_callback=None, allow_detour=True):
    """执行一个任务：识别 -> 点击 -> 等待状态变化。"""
    task_type = task.get("type", "normal")
    if task_type == "condition":
        return execute_condition_task(task, stop_flag=stop_flag, log_callback=log_callback)
    if task_type == "switch":
        return execute_switch_task(task, stop_flag=stop_flag, log_callback=log_callback)
    if task_type == "event":
        return execute_event_task(task, stop_flag=stop_flag, log_callback=log_callback)
    if task_type == "delay":
        return execute_delay_task(task, stop_flag=stop_flag, log_callback=log_callback)
    if task_type == "keyboard_move":
        return execute_keyboard_move_task(task, stop_flag=stop_flag, log_callback=log_callback)
    if task_type == "key_press":
        return execute_key_press_task(task, stop_flag=stop_flag, log_callback=log_callback)
    if task_type == "drag":
        return execute_drag_task(task, stop_flag=stop_flag, log_callback=log_callback)
    if task_type == "click_until_gone":
        return execute_click_until_gone_task(task, stop_flag=stop_flag, log_callback=log_callback)

    template_names = normalize_template_names(task.get("templates") or task.get("template"))
    if not template_names:
        raise ValueError("步骤至少需要配置一张绑定图片。")
    tpl_name = template_names[0]
    timeout = task.get("timeout", DEFAULT_TIMEOUT)
    click_requires_match = bool(task.get("click_requires_match", True))
    optional = bool(task.get("optional", not bool(task.get("required", True))))
    description = task.get("description", tpl_name)
    poll_interval = float(task.get("poll_interval", DEFAULT_POLL_INTERVAL))

    log(f"\n>>> 任务: {description}", log_callback)
    start_time = time.time()

    fixed_click = resolve_click_position(task)
    if task.get("click", True) and not click_requires_match and fixed_click is not None:
        click_x, click_y = fixed_click
        offset = task.get("offset", (0, 0))
        click_global_position(click_x, click_y, offset)
        log(f"  直接点击记录坐标: ({click_x + offset[0]}, {click_y + offset[1]})", log_callback)
        wait_for_step_result(task, template_name=tpl_name, next_template_names=task.get("next_template"), log_callback=log_callback, stop_flag=stop_flag)
        after_wait = max(0.0, float(task.get("after_wait", 0.0)))
        if after_wait > 0:
            if interruptible_sleep(after_wait, stop_flag):
                return False
        return True

    while True:
        if stop_flag is not None and stop_flag.is_set():
            log("用户中止脚本执行。", log_callback)
            return False

        screen_img = capture_screen()
        results = match_task_templates(task, screen_img, template_names)
        if task.get("type") == "advanced":
            matched_name, center, conf = find_leftmost_match(results, template_names)
        else:
            matched_name, center, conf = find_first_match(results, template_names)

        if center is not None:
            tpl_name = matched_name
            log(f"  识别到 '{tpl_name}'，置信度: {conf:.2f}，坐标: {center}", log_callback)
            if task.get("click", True):
                offset = task.get("offset", (0, 0))
                recorded_click = resolve_click_position(task)
                if recorded_click is not None:
                    screen_x, screen_y = recorded_click
                    click_source = "记录点击点"
                else:
                    screen_x, screen_y = to_global_position(center[0], center[1])
                    click_source = "识别中心"
                click_global_position(screen_x, screen_y, offset)
                log(f"  已点击{click_source}: ({screen_x + offset[0]}, {screen_y + offset[1]})", log_callback)

            wait_mode = task.get("wait_for", "time")
            wait_succeeded = True
            if wait_mode == "disappear":
                wait_result = wait_until_template_disappears(tpl_name, timeout=get_task_timeout(task, 12), stop_flag=stop_flag, threshold=get_task_threshold(task))
                wait_succeeded = bool(wait_result)
                if wait_result:
                    log(f"  '{tpl_name}' 已消失，继续下一步。", log_callback)
                else:
                    log(f"  等待 '{tpl_name}' 消失超时，继续下一步。", log_callback)
            elif wait_mode == "next_appear":
                next_tpl = task.get("next_template")
                if not next_tpl:
                    raise ValueError(f"任务 '{tpl_name}' 配置了 wait_for='next_appear'，但未提供 next_template。")
                next_center, next_conf = wait_until_template_appears(
                    next_tpl,
                    timeout=get_task_timeout(task, 12),
                    search_rect=task.get("next_match_rect") or task.get("next_search_rect"),
                    stop_flag=stop_flag,
                    threshold=get_task_threshold(task),
                )
                wait_succeeded = next_center is not None
                if next_center is None:
                    log(f"  等待 '{next_tpl}' 出现超时。", log_callback)
                else:
                    log(f"  检测到下一步 UI '{next_tpl}'，置信度 {next_conf:.2f}，坐标 {next_center}", log_callback)
            elif wait_mode == "change_then_appear":
                next_tpl = task.get("next_template")
                if not next_tpl:
                    raise ValueError(f"任务 '{tpl_name}' 配置了 wait_for='change_then_appear'，但未提供 next_template。")
                wait_succeeded = wait_for_step_result(task, template_name=tpl_name, next_template_names=next_tpl, log_callback=log_callback, stop_flag=stop_flag)
                log(f"  画面变化后检测到目标结果 '{next_tpl}'，继续下一步。", log_callback)
            else:
                wait_succeeded = wait_for_step_result(task, template_name=tpl_name, next_template_names=task.get("next_template"), log_callback=log_callback, stop_flag=stop_flag)

            if not wait_succeeded:
                timeout_target = task.get("wait_timeout_jump_to", task.get("timeout_jump_to"))
                if timeout_target is not None:
                    return {"next_step": int(timeout_target)}

            after_wait = max(0.0, float(task.get("after_wait", 0.0)))
            if after_wait > 0:
                log(f"  步骤完成后等待 {after_wait} 秒。", log_callback)
                if interruptible_sleep(after_wait, stop_flag):
                    return False

            success_jump_to = task.get("detour_success_jump_to")
            if success_jump_to is not None:
                log(f"  识别成功，跳转到编号步骤 {int(success_jump_to)}。", log_callback)
                return {"outer_jump_to": int(success_jump_to)}

            return True

        fixed_click = resolve_click_position(task)
        if task.get("click", True) and not click_requires_match and fixed_click is not None:
            click_x, click_y = fixed_click
            offset = task.get("offset", (0, 0))
            click_global_position(click_x, click_y, offset)
            log(f"  未开启识别点击，直接点击记录坐标: ({click_x + offset[0]}, {click_y + offset[1]})", log_callback)
            wait_for_step_result(task, template_name=tpl_name, next_template_names=task.get("next_template"), log_callback=log_callback, stop_flag=stop_flag)
            after_wait = max(0.0, float(task.get("after_wait", 0.0)))
            if after_wait > 0:
                if interruptible_sleep(after_wait, stop_flag):
                    return False
            return True

        if allow_detour and (task.get("detour_enabled") or task.get("detour_jump_to") is not None):
            log(f"  当前任务未识别到 '{tpl_name}'，执行迂回流程。", log_callback)
            if execute_detour_task(task, stop_flag=stop_flag, log_callback=log_callback):
                jump_target = task.get("detour_jump_to")
                if jump_target is not None:
                    jump_target = int(jump_target)
                    log(f"  迂回步骤已完成，返回外流程并跳转到编号步骤 {jump_target}。", log_callback)
                    return {"outer_jump_to": jump_target}
                return True
            return False

        if timeout > 0 and (time.time() - start_time) > timeout:
            log(f"  超时未检测到 '{tpl_name}'", log_callback)
            if task.get("timeout_jump_to") is not None:
                return {"next_step": int(task["timeout_jump_to"])}
            if optional:
                log(f"  该步骤为可选步骤，跳过。", log_callback)
                return False
            raise RuntimeError(f"必需步骤 '{tpl_name}' 未在超时时间内出现，脚本停止。")

        if interruptible_sleep(poll_interval, stop_flag):
            return False


def run_task_queue(tasks, loop=False, stop_flag=None, log_callback=None, execution_callback=None, execution_result_callback=None, pause_flag=None, single_step_flag=None, start_node_id=None):
    """按顺序执行任务列表。"""
    global _active_pause_flag, _single_step_active
    _active_pause_flag = pause_flag
    _single_step_active = False
    log("开始执行自动脚本，按 Ctrl+C 手动停止", log_callback)

    try:
        while True:
            if stop_flag is not None and stop_flag.is_set():
                log("脚本已被外部停止。", log_callback)
                return

            index = 0
            jump_chain = []
            flow_chain = []
            for task in tasks:
                task.setdefault("id", str(uuid.uuid4()))
            graph = NodeGraph(tasks, executor=execute_task)
            step_number_to_index = {
                int(task.get("_outer_step_number", task_index + 1)): task_index
                for task_index, task in enumerate(tasks)
            }
            task_id_to_index = graph.id_to_index
            loop_counts = {}
            graph_mode = any(
                task.get("flow_next") is not None or task.get("flow_next_disabled")
                for task in tasks
            )
            if graph_mode:
                entry_index = graph.entry_index()
                if start_node_id is not None:
                    entry_index = graph.resolve_id(start_node_id) if graph.resolve_id(start_node_id) is not None else entry_index
                index = entry_index
                log(f"蓝图执行入口：步骤 {entry_index + 1}", log_callback)
            elif start_node_id is not None:
                index = task_id_to_index.get(str(start_node_id), 0)
                log(f"执行入口：步骤 {index + 1}", log_callback)
            while index < len(tasks):
                if stop_flag is not None and stop_flag.is_set():
                    log("脚本已被外部停止。", log_callback)
                    return
                if pause_flag is not None:
                    while pause_flag.is_set() and not (stop_flag is not None and stop_flag.is_set()):
                        if single_step_flag is not None and single_step_flag.wait(0.1):
                            single_step_flag.clear()
                            _single_step_active = True
                            break
                    if stop_flag is not None and stop_flag.is_set():
                        return

                task = tasks[index]
                task.setdefault("id", str(uuid.uuid4()))
                if graph_mode:
                    log(f"蓝图节点：步骤 {index + 1} · {task.get('description', task.get('template', '未命名步骤'))}", log_callback)
                if execution_callback:
                    execution_callback(task)
                try:
                    node = TaskNode(task, executor=execute_task)
                    node_errors = node.validate()
                    if node_errors:
                        raise RuntimeError("；".join(node_errors))
                    if task.get("type") == "loop":
                        loop_key = str(task.get("id", index))
                        current_count = loop_counts.get(loop_key, 0)
                        max_count = max(0, int(task.get("loop_count", 1)))
                        if current_count < max_count:
                            loop_counts[loop_key] = current_count + 1
                            loop_target = task.get("loop_target")
                            result = {"next_step": int(loop_target)} if loop_target is not None else True
                            log(f"  循环第 {current_count + 1}/{max_count} 次。", log_callback)
                        else:
                            loop_counts[loop_key] = 0
                            exit_target = task.get("loop_exit_target")
                            result = {"next_step": int(exit_target)} if exit_target is not None else True
                            log("  循环次数已完成，离开循环。", log_callback)
                    else:
                        result = node.execute({"stop_flag": stop_flag, "log_callback": log_callback})
                    if _single_step_active:
                        _single_step_active = False
                    if execution_result_callback:
                        execution_result_callback(task, "success")
                    if isinstance(result, dict) and ("outer_jump_to" in result or "next_step" in result or "next_node_id" in result):
                        if "next_node_id" in result:
                            target_index = task_id_to_index.get(str(result["next_node_id"]))
                            jump_target = int(tasks[target_index].get("_outer_step_number", target_index + 1)) if target_index is not None else None
                        else:
                            jump_target = int(result.get("outer_jump_to", result.get("next_step")))
                        current_step_no = int(task.get("_outer_step_number", index + 1))
                        target_index = step_number_to_index.get(jump_target)

                        if jump_target == current_step_no:
                            log(f"  跳转目标编号 {jump_target} 与当前步骤相同，忽略，避免无限循环。", log_callback)
                        elif target_index is not None and target_index in jump_chain:
                            log(f"  跳转目标编号 {jump_target} 会形成循环，忽略。", log_callback)
                        elif target_index is not None:
                            jump_chain.append(index)
                            index = target_index
                            flow_chain = []
                            log(f"  跳转到编号步骤 {jump_target}。", log_callback)
                            continue
                        else:
                            log(f"  主流程编号 {jump_target} 不在当前已启用任务中，跳转忽略。", log_callback)
                except RuntimeError as e:
                    if execution_result_callback:
                        execution_result_callback(task, "failed")
                    log(f"错误: {e}", log_callback)
                    show_complete_message()
                    return
                if pause_flag is not None and single_step_flag is not None and pause_flag.is_set():
                    pause_flag.set()
                if task.get("flow_next") is not None or task.get("flow_next_disabled"):
                    flow_target = task.get("flow_next")
                    if flow_target is None:
                        log("  当前步骤未连接下一节点，流程结束。", log_callback)
                        break
                    flow_target_index = task_id_to_index.get(str(flow_target))
                    if flow_target_index is None:
                        log("  蓝图连接目标不存在，流程结束。", log_callback)
                        break
                    if flow_target_index == index or flow_target_index in flow_chain:
                        log("  蓝图连接形成循环，流程结束。", log_callback)
                        break
                    flow_chain.append(index)
                    index = flow_target_index
                    continue
                if graph_mode:
                    log("  当前蓝图节点没有下一连接，流程结束。", log_callback)
                    break
                flow_chain = []
                jump_chain = []
                index += 1

            log("\n任务队列执行完毕。", log_callback)
            if not loop:
                show_complete_message()
                return
            log("任务队列将重新开始循环...", log_callback)
            if interruptible_sleep(1, stop_flag):
                return

    except KeyboardInterrupt:
        log("\n用户手动停止脚本。", log_callback)
    finally:
        if _active_pause_flag is pause_flag:
            _active_pause_flag = None
        _single_step_active = False


if __name__ == "__main__":
    run_task_queue(TASKS, loop=False)
