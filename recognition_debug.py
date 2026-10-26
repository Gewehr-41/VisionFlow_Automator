# -*- coding: utf-8 -*-
"""最近一次识别的快照，供界面「识别调试」面板读取。

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
"""
import threading
import time

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


def record(frame, template=None, center=None, confidence=0.0, threshold=0.0,
           task="", hit=False, elapsed=0.0):
    """记录一次识别结果（worker 线程调用）。"""
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
            timestamp=time.time(),
        )


def snapshot():
    """返回当前快照的浅拷贝（GUI 线程调用）。"""
    with _LOCK:
        return dict(_STATE)


def clear():
    """清空快照（测试与"清空"按钮用）。"""
    with _LOCK:
        _STATE.update(frame=None, template=None, center=None, confidence=0.0,
                      threshold=0.0, task="", hit=False, elapsed=0.0, timestamp=0.0)
