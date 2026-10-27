# -*- coding: utf-8 -*-
"""最近一次识别快照 + 执行步骤历史（供「识别调试」面板读取）。

为什么要这个模块
----------------
识别发生在 worker 线程，界面在 GUI 线程；两者之间只能通过**不可变快照**交流。
面板要给人看的是四样东西：当时的一帧画面、用了哪张模板、命中坐标与得分、耗时，
所以这里只保存"最后一次"，并统一加锁读写。

为什么保存 frame 的**引用**而不是拷贝
-------------------------------------
一帧 2560×1440×3 的画面约 11 MB。识别轮询每 0.1 秒一次，若每次都深拷贝，
仅这一项就会把内存和 GC 拖垮。`capture_screen()` 每次都返回**新数组**、之后
不再原地修改，所以保存引用是安全的；真正需要的拷贝（转 QPixmap）只发生在
界面刷新时，而那时候由界面侧自己做。

执行步骤历史（2026-10-27 新增）
-------------------------------
调试窗口可以列出"执行过的步骤"，点步骤号码回看**当时的识别画面**。
为了不把内存吃爆，历史里不直接存 numpy 帧，而是存 **JPEG 编码后的字节**
（解码在 GUI 线程按需做）。历史默认关闭，只有调试窗口打开时才记录
（`set_history_enabled`），避免正常跑脚本时白白付出编码开销。
"""
import threading
import time

import cv2
import numpy as np

_LOCK = threading.Lock()
_STATE = {
    "frame": None,        # 最近一帧 BGR numpy 数组（引用，不拷贝）
    "template": None,     # 本次用于判定的模板名
    "center": None,       # 命中点（画面坐标），未命中为 None
    "confidence": 0.0,    # 命中置信度；未命中时为 0
    "threshold": 0.0,     # 该步骤当时的匹配阈值（便于判断"差多少"）
    "task": "",           # 步骤名称（日志里那个 description）
    "hit": False,         # 这一次识别是否命中
    "elapsed": 0.0,       # 该步骤已经开始等待的秒数
    "timestamp": 0.0,     # 记录时刻（time.time()）
}

HISTORY_LIMIT = 100       # 最多保留多少个"步骤执行"的画面
_HISTORY = []             # 每项：步骤号码 + 识别结果 + frame_jpeg（按执行顺序）
_HISTORY_ENABLED = False
_CURRENT_STEP_KEY = None  # 同一步骤的多次重试，只保留最后一次的画面


def set_history_enabled(enabled):
    """调试窗口打开/关闭时调用：打开才记录历史，避免平时额外编码开销。"""
    global _HISTORY_ENABLED
    with _LOCK:
        _HISTORY_ENABLED = bool(enabled)


def history_enabled():
    with _LOCK:
        return _HISTORY_ENABLED


def _encode_frame(frame):
    """BGR 帧 → JPEG 字节；编码失败返回 None（历史里就没有画面）。"""
    try:
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    except Exception:
        return None
    return buffer.tobytes() if ok else None


def decode_frame(frame_jpeg):
    """JPEG 字节 → BGR 帧（GUI 线程按需调用）；失败返回 None。"""
    if not frame_jpeg:
        return None
    try:
        data = np.frombuffer(frame_jpeg, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def record(frame, template=None, center=None, confidence=0.0, threshold=0.0,
           task="", hit=False, elapsed=0.0, step_number=None):
    """记录一次识别结果（worker 线程调用）。

    `step_number` 是列表里的步骤号码（1-based，来自 `_outer_step_number`）；
    流程外步骤（兜底/迂回）可能没有号码，历史里会显示成"流程外"。
    """
    global _CURRENT_STEP_KEY
    timestamp = time.time()
    entry = {
        "step_number": step_number,
        "task": str(task or ""),
        "template": template,
        "center": center,
        "confidence": float(confidence or 0.0),
        "threshold": float(threshold or 0.0),
        "hit": bool(hit),
        "elapsed": float(elapsed or 0.0),
        "timestamp": timestamp,
    }
    frame_jpeg = _encode_frame(frame) if (_HISTORY_ENABLED and frame is not None) else None
    with _LOCK:
        _STATE.update(
            frame=frame,
            template=template,
            center=center,
            confidence=float(confidence or 0.0),
            threshold=float(threshold or 0.0),
            task=str(task or ""),
            hit=bool(hit),
            elapsed=float(elapsed or 0.0),
            timestamp=timestamp,
        )
        if not _HISTORY_ENABLED or frame_jpeg is None:
            return
        key = (step_number, str(task or ""))
        if _HISTORY and _CURRENT_STEP_KEY == key:
            # 同一步骤的重试：只更新最后一次识别画面，不新增列表项
            _HISTORY[-1].update(entry)
            _HISTORY[-1]["frame_jpeg"] = frame_jpeg
        else:
            entry["frame_jpeg"] = frame_jpeg
            _HISTORY.append(entry)
            del _HISTORY[:-HISTORY_LIMIT]
        _CURRENT_STEP_KEY = key


def snapshot():
    """返回当前快照的浅拷贝（GUI 线程调用）。"""
    with _LOCK:
        return dict(_STATE)


def history_snapshot():
    """返回步骤历史（按执行顺序；每项是 dict 拷贝，frame_jpeg 为 bytes）。"""
    with _LOCK:
        return [dict(entry) for entry in _HISTORY]


def clear():
    """清空快照与历史（测试与「清空」按钮用）。"""
    global _CURRENT_STEP_KEY
    with _LOCK:
        _STATE.update(frame=None, template=None, center=None, confidence=0.0,
                      threshold=0.0, task="", hit=False, elapsed=0.0, timestamp=0.0)
        _HISTORY.clear()
        _CURRENT_STEP_KEY = None
