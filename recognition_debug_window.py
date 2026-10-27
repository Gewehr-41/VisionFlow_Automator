# -*- coding: utf-8 -*-
"""识别调试窗口：显示最近一次识别用的画面、模板、得分与耗时。

为什么要单独一个窗口
--------------------
识别发生在 worker 线程，界面需要定时读取 `recognition_debug` 的快照。做成独立窗口
的好处是：不改动主界面布局（主界面左侧列表 / 右侧编辑器 / 底部日志各有职责），
出错也不影响任何执行逻辑——它只是"只读的调试面板"。

使用方式
--------
主界面「识别调试」按钮打开。运行脚本时每 0.4 秒刷新一次；可勾选「暂停刷新」冻结。
「立即刷新」手动抓一帧；「清空」清空快照与历史。

执行步骤历史（2026-10-27 新增）
-------------------------------
左侧列出打开本窗口后**执行过的步骤**（显示步骤号码与命中结果）；点击某一行会冻结
实时预览，改为显示**那一步当时的识别画面**；点「回到最新」恢复实时画面。
历史只在窗口打开时记录（避免正常跑脚本额外编码开销），最多 100 步。
"""
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import recognition_debug
from ui_common import _frame_to_pixmap

_PREVIEW_MAX_WIDTH = 900
_HISTORY_WIDTH = 250


class RecognitionDebugWindow(QWidget):
    """最近一次识别可视化面板（只读）。"""

    def __init__(self, parent=None):
        # 独立式：Qt.Window；只给 parent 是 QWidget 的话会变成**子控件**（会被嵌在
        # 主界面里画不出来、也没有标题栏、关不掉）——2026-10-26 实测反馈过这个问题。
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("识别调试（最近一次识别）")
        self.resize(1160, 720)
        self._last_timestamp = 0.0
        self._viewing_history = False

        layout = QVBoxLayout(self)

        body = QHBoxLayout()
        layout.addLayout(body, 1)

        # ---------- 左侧：执行步骤历史 ----------
        history_column = QVBoxLayout()
        history_title = QLabel("执行步骤（点步骤号码看当时的识别画面）")
        history_title.setWordWrap(True)
        history_column.addWidget(history_title)
        self.history_list = QListWidget()
        self.history_list.setFixedWidth(_HISTORY_WIDTH)
        self.history_list.itemClicked.connect(self._on_history_item_clicked)
        history_column.addWidget(self.history_list, 1)
        self.history_hint = QLabel(
            "运行脚本并打开本窗口时，会在这里记录每一步的识别画面。\n"
            "点击某一步可回看当时的画面；点「回到最新」恢复实时预览。")
        self.history_hint.setWordWrap(True)
        self.history_hint.setObjectName("statusInfo")
        history_column.addWidget(self.history_hint)
        self.back_to_live_button = QPushButton("回到最新")
        self.back_to_live_button.clicked.connect(self._on_back_to_live)
        history_column.addWidget(self.back_to_live_button)
        body.addLayout(history_column)

        # ---------- 右侧：实时/历史画面 ----------
        preview_column = QVBoxLayout()
        body.addLayout(preview_column, 1)

        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview_column.addWidget(self.info_label)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(360)
        self.image_label.setStyleSheet("background:#0f172a; color:#94a3b8;")
        preview_column.addWidget(self.image_label, 1)

        row = QHBoxLayout()
        self.pause_checkbox = QCheckBox("暂停刷新")
        row.addWidget(self.pause_checkbox)
        self.refresh_button = QPushButton("立即刷新")
        self.refresh_button.clicked.connect(self._on_refresh_clicked)
        row.addWidget(self.refresh_button)
        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self._on_clear_clicked)
        row.addWidget(self.clear_button)
        # 多一个「关闭」按钮：有些系统上 X 已经能关，但远程桌面/嵌入式场景下不明显，
        # 统一给一个显式入口，避免使用者反馈"关不掉"。
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.close_button)
        row.addStretch(1)
        hint = QLabel("画面来自最近一次识别；红色方框=命中位置")
        row.addWidget(hint)
        preview_column.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start(400)

        self._refresh_history_list()
        self._render(recognition_debug.snapshot())

    # ---------- 刷新 ----------

    def _auto_refresh(self):
        if self.pause_checkbox.isChecked():
            return
        if self._viewing_history:
            self._refresh_history_list()   # 用户在看历史：只更新左侧列表，不覆盖右侧画面
            return
        self.refresh_from_snapshot(only_if_new=True)

    def refresh_from_snapshot(self, only_if_new=False):
        state = recognition_debug.snapshot()
        self._refresh_history_list()
        if only_if_new and state["timestamp"] == self._last_timestamp:
            return
        if state["timestamp"] != self._last_timestamp:
            self._last_timestamp = state["timestamp"]
        if self._viewing_history:
            return state
        self._render(state)
        return state

    def _on_refresh_clicked(self):
        self._viewing_history = False
        self._clear_history_selection()
        self._last_timestamp = 0.0
        self.refresh_from_snapshot()

    def _on_back_to_live(self):
        self._viewing_history = False
        self._clear_history_selection()
        self._last_timestamp = 0.0
        self.refresh_from_snapshot()

    def _on_clear_clicked(self):
        recognition_debug.clear()
        self._last_timestamp = 0.0
        self._viewing_history = False
        self._refresh_history_list()
        self._render(recognition_debug.snapshot())

    # ---------- 历史列表 ----------

    @staticmethod
    def _history_label(entry):
        step_number = entry.get("step_number")
        prefix = f"第 {step_number} 步" if step_number else "流程外"
        mark = "✔" if entry.get("hit") else "✘"
        task = str(entry.get("task") or "未命名步骤")
        if len(task) > 14:
            task = task[:13] + "…"
        return f"{prefix} {mark} {task}"

    def _refresh_history_list(self):
        entries = recognition_debug.history_snapshot()
        selected_index = self.history_list.currentRow() if self._viewing_history else -1
        self.history_list.blockSignals(True)
        try:
            self.history_list.clear()
            for entry in entries:
                item = QListWidgetItem(self._history_label(entry))
                item.setToolTip(
                    f"{self._history_label(entry)}\n"
                    f"模板：{entry.get('template') or '—'}\n"
                    f"置信度：{entry.get('confidence', 0.0):.2f} / 阈值 {entry.get('threshold', 0.0):.2f}\n"
                    f"耗时：{entry.get('elapsed', 0.0):.1f} 秒")
                self.history_list.addItem(item)
            if selected_index >= 0 and selected_index < len(entries):
                self.history_list.setCurrentRow(selected_index)
            elif not self._viewing_history and entries:
                self.history_list.setCurrentRow(len(entries) - 1)
        finally:
            self.history_list.blockSignals(False)
        self.history_hint.setVisible(not entries)
        self.back_to_live_button.setEnabled(self._viewing_history)

    def _on_history_item_clicked(self, item):
        index = self.history_list.row(item)
        entries = recognition_debug.history_snapshot()
        if not 0 <= index < len(entries):
            return
        self._viewing_history = True
        self.back_to_live_button.setEnabled(True)
        self._render_history_entry(entries[index])

    def _clear_history_selection(self):
        self.history_list.blockSignals(True)
        try:
            self.history_list.clearSelection()
            self.history_list.setCurrentRow(-1)
        finally:
            self.history_list.blockSignals(False)
        self.back_to_live_button.setEnabled(False)

    def _render_history_entry(self, entry):
        state = dict(entry)
        entry_frame = recognition_debug.decode_frame(entry.get("frame_jpeg"))
        state["frame"] = entry_frame
        self._render(state, historical=True)

    # ---------- 渲染 ----------

    def _info_text(self, state, historical=False):
        if not state["timestamp"]:
            return "尚无识别记录。运行脚本后这里会显示最近一次识别的画面与得分。"
        if state["hit"]:
            result = (f"[命中] 「{state['template']}」"
                      f"（得分 {state['confidence']:.2f} / 阈值 {state['threshold']:.2f}）")
            position = f"命中坐标：{state['center']}"
        else:
            result = (f"[未命中] 「{state['template']}」"
                      f"（阈值 {state['threshold']:.2f}）")
            position = "命中坐标：—"
        text = (f"步骤：{state['task']}\n"
                f"{result}，耗时 {state['elapsed']:.1f} 秒\n"
                f"{position}")
        if historical:
            text = ("【历史画面】\n" + text +
                    "\n（点「回到最新」可恢复实时预览）")
        return text

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

    def _render(self, state, historical=False):
        self.info_label.setText(self._info_text(state, historical=historical))
        pixmap = self._image_with_marker(state)
        if pixmap is None:
            self.image_label.setPixmap(_empty_pixmap())
            self.image_label.setText("（暂无画面）" if not historical else "（历史画面不可用）")
        else:
            self.image_label.setText("")
            self.image_label.setPixmap(pixmap)

    # ---------- 窗口生命周期 ----------

    def showEvent(self, event):
        """重新打开时恢复自动刷新，并开始记录执行步骤历史。"""
        super().showEvent(event)
        if not self._timer.isActive():
            self._timer.start(400)
        recognition_debug.set_history_enabled(True)
        self._refresh_history_list()
        self.refresh_from_snapshot()

    def closeEvent(self, event):
        self._timer.stop()
        recognition_debug.set_history_enabled(False)
        super().closeEvent(event)


def _empty_pixmap():
    from PySide6.QtGui import QPixmap

    return QPixmap()
