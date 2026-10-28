# -*- coding: utf-8 -*-
"""步骤类型的内部值 ↔ 中文显示名。

内部/JSON 里仍保存英文 `type`（`normal`、`key_press`…），保证旧数据、预设和脚本兼容；
界面上统一显示中文。新增步骤类型时只改这一张表。
"""

STEP_TYPE_LABELS = {
    "normal": "普通步骤",
    "advanced": "多模板识别",
    "loop": "循环",
    "keyboard_move": "键盘移动",
    "key_press": "按键",
    "drag": "拖拽",
    "click_until_gone": "持续点击",
    "delay": "延迟",
    "condition": "条件",
    "switch": "选择",
    "event": "事件",
}

STEP_TYPE_ORDER = (
    "normal",
    "advanced",
    "loop",
    "keyboard_move",
    "key_press",
    "drag",
    "click_until_gone",
    "delay",
    "condition",
    "switch",
    "event",
)

_LABEL_TO_TYPE = {label: value for value, label in STEP_TYPE_LABELS.items()}


def type_label(value, default=None):
    """内部类型 → 中文名；未知类型原样返回（便于排障）。"""
    value = str(value if value is not None else (default or "normal"))
    return STEP_TYPE_LABELS.get(value, value)


def type_from_label(label, fallback="normal"):
    """中文名（或旧英文值）→ 内部类型。"""
    text = str(label or "").strip()
    if text in STEP_TYPE_LABELS:
        return text
    if text in _LABEL_TO_TYPE:
        return _LABEL_TO_TYPE[text]
    return fallback


def type_options(values=None):
    """返回 [(中文名, 内部值)]，用于 QComboBox/QInputDialog。"""
    values = values if values is not None else STEP_TYPE_ORDER
    return [(type_label(value), value) for value in values]
