# -*- coding: utf-8 -*-
"""任务列表控件（项绘制、拖放）与执行 worker。

从 gui_pyside6.py 外移出来：TaskItemDelegate / TaskListWidget 是纯 Qt 控件；
TaskWorker 负责在线程里跑 run_task_queue，并把日志 / 状态转成信号。

名字在 gui_pyside6 里重新导出，既有用法不变。
"""
import traceback

from PySide6.QtCore import QObject, QRect, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget

from main import run_task_queue

class TaskItemDelegate(QStyledItemDelegate):
    HANDLE_WIDTH = 30

    def paint(self, painter, option, index):
        is_group = index.data(Qt.UserRole) == "group"
        if is_group:
            super().paint(painter, option, index)
        else:
            text_option = QStyleOptionViewItem(option)
            text_option.rect = QRect(
                option.rect.left(),
                option.rect.top(),
                max(0, option.rect.width() - self.HANDLE_WIDTH),
                option.rect.height(),
            )
            super().paint(painter, text_option, index)
        rect = option.rect
        painter.save()
        indicator_rect = QRect(rect.left() + 6, rect.center().y() - 8, 16, 16)
        state = index.data(Qt.CheckStateRole)
        if state == Qt.CheckState.Checked:
            painter.setPen(QPen(QColor("#ffffff"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawLine(indicator_rect.left() + 4, indicator_rect.center().y(), indicator_rect.left() + 7, indicator_rect.bottom() - 4)
            painter.drawLine(indicator_rect.left() + 7, indicator_rect.bottom() - 4, indicator_rect.right() - 3, indicator_rect.top() + 4)
        elif state == Qt.CheckState.PartiallyChecked:
            painter.setPen(QPen(QColor("#0369a1"), 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(indicator_rect.left() + 4, indicator_rect.center().y(), indicator_rect.right() - 4, indicator_rect.center().y())
        painter.restore()

        if is_group:
            return
        painter.save()
        painter.setPen(QPen(QColor("#94a3b8"), 1.6))
        handle_x = rect.right() - self.HANDLE_WIDTH // 2
        cy = rect.center().y()
        for dy in (-4, 0, 4):
            painter.drawLine(handle_x - 6, cy + dy, handle_x + 6, cy + dy)
        painter.restore()


class TaskListWidget(QTreeWidget):
    toggle_requested = Signal()
    order_changed = Signal()
    task_drop_requested = Signal(int, int, bool)
    task_drop_to_end_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_handle_pressed = False
        self._drag_source_item = None

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        self._drag_handle_pressed = False
        self._drag_source_item = None
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            if item is not None and item.data(0, Qt.UserRole) != "group":
                rect = self.visualItemRect(item)
                if pos.x() >= rect.right() - 30:
                    self._drag_handle_pressed = True
                    self._drag_source_item = item
        super().mousePressEvent(event)

    def startDrag(self, supportedActions):
        if not self._drag_handle_pressed:
            return
        super().startDrag(supportedActions)

    def dropEvent(self, event):
        target = self.itemAt(event.position().toPoint())
        source = self._drag_source_item or self.currentItem()
        if source is None or source.data(0, Qt.UserRole) == "group":
            event.ignore()
            return
        source_index = source.data(0, Qt.UserRole + 1)
        if source_index is None:
            event.ignore()
            return
        if target is None or target.data(0, Qt.UserRole) == "group":
            # 拖到空白处或组头：移动到末尾
            event.accept()
            QTimer.singleShot(0, lambda: self.task_drop_to_end_requested.emit(int(source_index)))
            return
        target_index = target.data(0, Qt.UserRole + 1)
        if target_index is None:
            event.ignore()
            return
        target_rect = self.visualItemRect(target)
        insert_after = event.position().y() >= target_rect.center().y()
        event.accept()
        QTimer.singleShot(0, lambda: self.task_drop_requested.emit(int(source_index), int(target_index), insert_after))
class TaskWorker(QObject):
    log = Signal(str)
    execution_started = Signal(dict)
    execution_result = Signal(dict, str)
    completed = Signal(str)
    finished = Signal()

    def __init__(self, tasks, loop, stop_event, pause_event, single_step_event, start_node_id):
        super().__init__()
        self.tasks = tasks
        self.loop = loop
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.single_step_event = single_step_event
        self.start_node_id = start_node_id

    @Slot()
    def run(self):
        try:
            run_task_queue(
                self.tasks,
                loop=self.loop,
                stop_flag=self.stop_event,
                log_callback=self.log.emit,
                execution_callback=self.execution_started.emit,
                execution_result_callback=self.execution_result.emit,
                pause_flag=self.pause_event,
                single_step_flag=self.single_step_event,
                start_node_id=self.start_node_id,
                completion_callback=self.completed.emit,
            )
        except Exception as exc:
            # 保留完整堆栈，便于定位真实出错位置（此前只记录异常消息，排障困难）。
            self.log.emit(f"脚本异常: {exc}")
            self.log.emit(traceback.format_exc())
            self.completed.emit("failed")
        finally:
            self.finished.emit()
