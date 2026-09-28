# -*- coding: utf-8 -*-
"""两个窗口共用的叶子级 UI 辅助。

这里放的都是不依赖任何窗口类的小东西：小部件行的构造器、数值解析、
模板缩略图加载、屏幕采集矩形。它们此前散落在 gui_pyside6.py 顶部，
而 `CaptureOverlayMixin` 与两个窗口都要用；外移成叶子模块后
`capture_overlay.py` 可以独立引用，不会与 gui_pyside6 形成循环导入。

所有名字仍在 gui_pyside6 里重新导出，因此 `gui_pyside6.<名字>` 的既有用法
（含 tests/ 里的调用）完全不变。
"""
import os

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel

import config
from core.screen import get_window_rect


def _jump_index_map(old_indices_in_new_order):
    """构造 remap_jump_targets 需要的「旧下标 → 新下标」映射。

    参数是「按新顺序排列的旧下标序列」：
    - 拖动排序时传入重排后的完整序列；
    - 删除步骤时传入保留下来的旧下标（升序），被删的下标不在序列中，
      于是指向它们的跳转会被 remap_jump_targets 清除。
    """
    return {old_index: new_index for new_index, old_index in enumerate(old_indices_in_new_order)}


def _load_template_pixmap(template_name):
    """按模板名从 icons 目录读取 PNG，找不到或读取失败时返回 None。"""
    if not template_name:
        return None
    name = os.path.splitext(str(template_name).strip())[0]
    if not name:
        return None
    path = os.path.join(config.ICON_DIR, f"{name}.png")
    if not os.path.isfile(path):
        return None
    pixmap = QPixmap(path)
    return pixmap if not pixmap.isNull() else None


def _first_available_template_pixmap(templates, limit=12):
    """从逗号分隔的模板名列表中取第一个能找到图片的，返回 (名字, pixmap)。

    高级步骤可以绑定多个候选模板，显示第一个即可；但若第一个恰好缺图，
    继续往后找比直接显示空白更有用。limit 用于避免模板名很多时逐个探盘。
    """
    names = [item.strip() for item in str(templates or "").replace("，", ",").split(",") if item.strip()]
    for name in names[:limit]:
        pixmap = _load_template_pixmap(name)
        if pixmap is not None:
            return name, pixmap
    return (names[0] if names else None), None


# ---------------------------------------------------------------------------
# 表单行的共享构造器
#
# 两个窗口此前各自在 _build_ui 里定义了同名的局部闭包（add_row /
# add_coordinate_row / add_click_until_row），三对函数体逐字节相同、只是闭包
# 变量不同，共 6 份。抽到模块级后由 partial 绑定各自的布局，调用点不变。
# ---------------------------------------------------------------------------


def _add_form_row(layout, label, widget):
    """一行「标签 + 控件」。"""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(QLabel(label), 0)
    row.addWidget(widget, 1)
    layout.addLayout(row)


def _add_coordinate_row(layout, label, x_edit, y_edit, extra=None):
    """一行「标签 + X + Y（+ 可选按钮）」。

    两个短输入并排，避免每个数字独占一整行。``extra`` 用于把与这组坐标配套的按钮
    放在同一行末尾（「点击:」这一行放的是「清空点击点」）。
    """
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(QLabel(label), 0)
    row.addWidget(QLabel("X"), 0)
    row.addWidget(x_edit, 1)
    row.addWidget(QLabel("Y"), 0)
    row.addWidget(y_edit, 1)
    if extra is not None:
        row.addWidget(extra, 0)
    layout.addLayout(row)


def _add_click_until_row(layout, label, widget):
    """持续点击设置区的一行「标签 + 控件」。"""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(QLabel(label), 0)
    row.addWidget(widget, 1)
    layout.addLayout(row)


def _add_region_selector_row(layout, selector, status_label, delete_button):
    """识别区域的选择行：「区域选择」下拉 + 共 N 个 + 删除本区域。

    一个步骤可以配置多条识别区域（引擎会逐条尝试），所以除了当前正在编辑的那条
    之外，还需要一个地方看到"一共几条、现在编辑的是第几条"。
    """
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(QLabel("区域选择:"), 0)
    row.addWidget(selector, 1)
    row.addWidget(status_label, 0)
    row.addWidget(delete_button, 0)
    layout.addLayout(row)


def _to_float(value, fallback):
    """把输入解析为 float，失败时返回 fallback。

    主窗口与蓝图窗口此前各有一份完全相同的实现
    （_float/_float_value），这里合并为单一版本。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _to_int(value):
    """把输入解析为 int（先转 float 以兼容 "3.0" 这类输入），失败时返回 None。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _capture_rect():
    """返回屏幕采集覆盖层应覆盖的矩形。

    选择目标窗口时用该窗口矩形，否则用主屏几何范围。
    主窗口与蓝图窗口此前各有一份完全相同的实现。
    """
    if config.USE_WINDOW_MODE and config.TARGET_WINDOW_TITLE:
        return get_window_rect()
    screen = QApplication.primaryScreen().geometry()
    return {"left": screen.left(), "top": screen.top(), "width": screen.width(), "height": screen.height()}
