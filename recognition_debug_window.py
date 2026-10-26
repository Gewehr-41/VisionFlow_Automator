# -*- coding: utf-8 -*-
"""识别调试窗口：显示最近一次识别用的画面、模板、得分与耗时。

为什么单独一个窗口
------------------
识别发生在 worker 线程；面板需要定时读 `recognition_debug` 的快照。做成独立窗口
的好处是：不改变主界面既有布局（左侧任务列表 / 右侧编辑面板 / 底部日志都有各自
的职责），关掉它也不影响任何执行逻辑；主窗口只持有一个单例引用。

使用方式
--------
主界面「识别调试」按钮打开。运行脚本时每 0.4 秒刷新一次；勾选「暂停刷新」可以
冻住当前帧仔细看（调试阈值时最常用）。
"""
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import recognition_debug
from ui_common import _frame_to_pixmap

_PREVIEW_MAX_WIDTH = 900


class RecognitionDebugWindow(QWidget):
    """最近一次识别的可视化面板（只读）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("识别调试（最近一次识别）")
        self.resize(960, 720)
        self._last_timestamp = 0.0

        layout = QVBoxLayout(self)

        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.info_label)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(360)
        self.image_label.setStyleSheet("background:#0f172a; color:#94a3b8;")
        layout.addWidget(self.image_label, 1)

        row = QHBoxLayout()
        self.pause_checkbox = QCheckBox("暂停刷新")
        row.addWidget(self.pause_checkbox)
        self.refresh_button = QPushButton("立即刷新")
        self.refresh_button.clicked.connect(lambda: self.refresh_from_snapshot())
        row.addWidget(self.refresh_button)
        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self._on_clear_clicked)
        row.addWidget(self.clear_button)
        row.addStretch(1)
        hint = QLabel("画面来自最近一次识别；红色方框=命中位置")
        row.addWidget(hint)
        layout.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start(400)

        self._render(recognition_debug.snapshot())

    # ---------- 刷新 ----------

    def _auto_refresh(self):
        if self.pause_checkbox.isChecked():
            return
        self.refresh_from_snapshot(only_if_new=True)

    def refresh_from_snapshot(self, only_if_new=False):
        state = recognition_debug.snapshot()
        if only_if_new and state["timestamp"] == self._last_timestamp:
            return
        self._last_timestamp = state["timestamp"]
        self._render(state)
        return state

    def _on_clear_clicked(self):
        recognition_debug.clear()
        self._last_timestamp = 0.0
        self._render(recognition_debug.snapshot())

    # ---------- 渲染 ----------

    def _info_text(self, state):
        if not state["timestamp"]:
            return "尚无识别记录。运行脚本后，这里会显示最近一次识别的画面与得分。"
        if state["hit"]:
            result = (f"[命中] 「{state['template']}」"
                      f"（得分 {state['confidence']:.2f} / 阈值 {state['threshold']:.2f}）")
            position = f"命中坐标：{state['center']}"
        else:
            result = (f"[未命中] 「{state['template']}」"
                      f"（阈值 {state['threshold']:.2f}）")
            position = "命中坐标：—"
        return (f"步骤：{state['task']}\n"
                f"{result}　耗时 {state['elapsed']:.1f} 秒\n"
                f"{position}")

    def _image_with_marker(self, state):
        pixmap = _frame_to_pixmap(state["frame"], max_width=_PREVIEW_MAX_WIDTH)
        if pixmap is None or not state["hit"] or state["center"] is None:
            return pixmap
        frame = state["frame"]
        frame_height, frame_width = frame.shape[:2]
        if not frame_width or not frame_height:
            return pixmap
        scale_x = pixmap.width() / float(frame_width)
        scale_y = pixmap.height() / float(frame_height)
        center_x, center_y = state["center"]
        marker_x = center_x * scale_x
        marker_y = center_y * scale_y
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(QColor("#ef4444"), 2))
            half = 16
            painter.drawRect(QRectF(marker_x - half, marker_y - half, half * 2, half * 2))
            painter.drawLine(marker_x - half * 2, marker_y, marker_x + half * 2, marker_y)
            painter.drawLine(marker_x, marker_y - half * 2, marker_x, marker_y + half * 2)
        finally:
            painter.end()
        return pixmap

    def _render(self, state):
        self.info_label.setText(self._info_text(state))
        pixmap = self._image_with_marker(state)
        if pixmap is None:
            self.image_label.setPixmap(_empty_pixmap())
            self.image_label.setText("（暂无画面）")
        else:
            self.image_label.setText("")
            self.image_label.setPixmap(pixmap)

    # ---------- 生命周期 ----------

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)


def _empty_pixmap():
    from PySide6.QtGui import QPixmap

    return QPixmap()
