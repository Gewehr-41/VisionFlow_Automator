# 屏幕与窗口工具：捕获目标区域并处理窗口坐标转换。
import cv2
import numpy as np
import mss
import pygetwindow as gw

import config

sct = mss.mss()
monitor = sct.monitors[config.MONITOR_INDEX]


def get_window_rect():
    """返回目标窗口的坐标，优先使用配置中的窗口标题；即使窗口不在前台也可以工作。"""
    title = getattr(config, "TARGET_WINDOW_TITLE", None)
    if title:
        wins = gw.getWindowsWithTitle(title)
        if wins:
            win = wins[0]
            return {
                "left": win.left,
                "top": win.top,
                "width": max(1, win.width),
                "height": max(1, win.height),
            }

    foreground = gw.getActiveWindow()
    if foreground:
        return {
            "left": foreground.left,
            "top": foreground.top,
            "width": max(1, foreground.width),
            "height": max(1, foreground.height),
        }

    return {
        "left": 0,
        "top": 0,
        "width": monitor["width"] if isinstance(monitor, dict) else 1920,
        "height": monitor["height"] if isinstance(monitor, dict) else 1080,
    }


def to_global_position(local_x, local_y):
    """把窗口内坐标转成屏幕全局坐标。"""
    rect = get_window_rect()
    if rect is None:
        return (local_x, local_y)
    return (rect["left"] + local_x, rect["top"] + local_y)


def capture_screen():
    """截取当前屏幕画面，优先捕获指定窗口区域；即使窗口在后台也支持识别。"""
    if getattr(config, "USE_WINDOW_MODE", False):
        rect = get_window_rect()
        screenshot = sct.grab(rect)
        frame = np.array(screenshot)
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    screenshot = sct.grab(monitor)
    frame = np.array(screenshot)
    return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
