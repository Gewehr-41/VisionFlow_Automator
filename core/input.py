# 输入工具：通过 Win32 API 或 PyAutoGUI 模拟鼠标点击和拖曳。
import ctypes
import threading
import time

import pyautogui

from config import CLICK_DELAY


user32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

# 记录脚本当前按住的键盘按键，便于紧急停止时统一释放。
_PRESSED_KEYS = set()
_PRESSED_KEYS_LOCK = threading.Lock()


def key_down(key):
    """按住指定键并登记，用于紧急停止时释放。"""
    pyautogui.keyDown(key)
    with _PRESSED_KEYS_LOCK:
        _PRESSED_KEYS.add(str(key).lower())


def key_up(key):
    """松开指定键并取消登记。"""
    pyautogui.keyUp(key)
    with _PRESSED_KEYS_LOCK:
        _PRESSED_KEYS.discard(str(key).lower())


def release_all_inputs():
    """紧急停止时释放脚本当前按住的全部键盘按键和鼠标左键，避免键位卡死。"""
    with _PRESSED_KEYS_LOCK:
        pressed = list(_PRESSED_KEYS)
        _PRESSED_KEYS.clear()
    for key in pressed:
        try:
            pyautogui.keyUp(key)
        except Exception:
            pass
    try:
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception:
        pass



def _win32_click(target_x, target_y, duration=CLICK_DELAY):
    """用 Win32 API 模拟鼠标事件，支持后台/非前台窗口点击。"""
    if duration > 0:
        time.sleep(min(duration, 0.05))

    user32.SetCursorPos(int(target_x), int(target_y))
    time.sleep(max(0.01, min(duration, 0.03)))
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.02)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def drag_position(start_x, start_y, end_x, end_y, duration=0.25, stop_flag=None):
    """从一个点拖到另一个点，适合列表滚动/拖动界面。"""
    duration = max(0.0, float(duration))
    user32.SetCursorPos(int(start_x), int(start_y))
    time.sleep(0.02)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    steps = max(8, int(max(abs(end_x - start_x), abs(end_y - start_y)) / 5))
    start_time = time.monotonic()
    for i in range(1, steps + 1):
        if stop_flag is not None and stop_flag.is_set():
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return False
        progress = i / steps
        x = int(start_x + (end_x - start_x) * progress)
        y = int(start_y + (end_y - start_y) * progress)
        user32.SetCursorPos(x, y)
        remaining = start_time + duration * progress - time.monotonic()
        if remaining > 0:
            if stop_flag is not None and stop_flag.wait(remaining):
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                return False
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return True


def click_position(x, y, offset=(0, 0), duration=CLICK_DELAY):
    """点击屏幕上某个坐标，可带偏移。"""
    target_x = x + offset[0]
    target_y = y + offset[1]
    try:
        _win32_click(target_x, target_y, duration=duration)
    except Exception:
        pyautogui.moveTo(target_x, target_y, duration=duration)
        pyautogui.click(target_x, target_y)


def click_global_position(x, y, offset=(0, 0), duration=CLICK_DELAY):
    """点击屏幕全局坐标，适合后台/窗口模式。"""
    target_x = x + offset[0]
    target_y = y + offset[1]
    try:
        _win32_click(target_x, target_y, duration=duration)
    except Exception:
        pyautogui.click(target_x, target_y, duration=duration)
