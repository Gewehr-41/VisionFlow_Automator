# -*- coding: utf-8 -*-
"""任务列表控件（项绘制、拖放）与执行 worker。

从 gui_pyside6.py 外移出来：TaskItemDelegate / TaskListWidget 是纯 Qt 控件；
TaskWorker 负责在线程里跑 run_task_queue，并把日志 / 状态转成信号。

名字在 gui_pyside6 里重新导出，既有用法不变。
"""
import traceback

from PySide6.QtCore import QObject, QRect, QRectF, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget

from main import run_task_queue

class TaskItemDelegate(QStyledItemDelegate):
    HANDLE_WIDTH = 30

    @staticmethod
    def _checkbox_rect(option_rect):
        """复选框外框：20x20，位置与原 QSS indicator 一致（item 左缘 +7）。"""
        return QRect(option_rect.left() + 7, option_rect.center().y() - 10, 20, 20)

    def _paint_checkbox(self, painter, option, index):
        """自绘启用复选框（未选中/选中/半选），支持悬浮态，任何主题下都常驻可见。"""
        state = index.data(Qt.CheckStateRole)
        try:
            # Qt 返回的是 int（例如 2），而 `2 == Qt.CheckState.Checked` 在 PySide6 里是 False；
            # 必须显式转成枚举，否则勾选态永远按"未选中"画——之前 QSS SVG 掩盖了这个 bug，
            # Windows 11 原生样式不画 QSS indicator 时就完全看不到勾（所有者 2026-10-27 反馈）。
            state = Qt.CheckState(int(state))
        except (TypeError, ValueError):
            return
        box = self._checkbox_rect(option.rect)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        checked = state == Qt.CheckState.Checked
        partial = state == Qt.CheckState.PartiallyChecked
        if checked:
            fill = QColor("#0369a1") if hovered else QColor("#0284c7")
            border = QColor("#ffffff")
        elif partial:
            fill = QColor("#0ea5e9") if hovered else QColor("#38bdf8")
            border = QColor("#ffffff")
        else:
            fill = QColor("#f0f9ff") if hovered else QColor("#ffffff")
            border = QColor("#0284c7") if hovered else QColor("#94a3b8")
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(border, 2))
            painter.setBrush(QBrush(fill))
            painter.drawRoundedRect(QRectF(box.adjusted(1, 1, -1, -1)), 4, 4)
            if checked:
                painter.setPen(QPen(QColor("#ffffff"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                painter.drawLine(box.left() + 5, box.center().y(), box.left() + 8, box.bottom() - 7)
                painter.drawLine(box.left() + 8, box.bottom() - 7, box.right() - 5, box.top() + 7)
            elif partial:
                painter.setPen(QPen(QColor("#0369a1"), 2, Qt.SolidLine, Qt.RoundCap))
                painter.drawLine(box.left() + 6, box.center().y(), box.right() - 6, box.center().y())
        finally:
            painter.restore()

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
        # 复选框自己画：Windows 11 原生样式下 QSS 的 indicator 在组行上不会常驻绘制
        # （只有鼠标悬浮/重绘才出现，所有者 2026-10-27 实测反馈），自绘才能保证一直可见。
        self._paint_checkbox(painter, option, index)

        if is_group:
            return
        rect = option.rect
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
    task_drop_requested = Signal(int, int, bool, object)          # 源下标, 目标下标, 是否插到后面, 目标所属组
    task_into_group_requested = Signal(int, str)                  # 步骤 -> 放进某个组
    task_before_group_requested = Signal(int, str)                # 步骤 -> 放到某个组前面
    task_to_root_requested = Signal(int)                          # 步骤 -> 移到根层末尾
    group_reparent_requested = Signal(str, object)                # 组 -> 挂到某个组（None=根层）
    group_reorder_requested = Signal(str, int, bool)              # 组 -> 移到某步骤前/后

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_handle_pressed = False
        self._drag_source_item = None
        # 拖拽开始时把"多选里的所有步骤"记下来：Qt 拖拽信号只带按下的那一个，
        # 且拖拽过程中选择集可能被折叠，处理函数需要靠它做整块移动。
        self.drag_task_indices = []

    def drawRow(self, painter, options, index):
        """组行底色铺满整行（含缩进/展开箭头区域）。

        QTreeView 默认只给 item 区域涂背景，根/嵌套组的缩进那一小段仍是白底；
        组行本来有分组颜色，所以复选框左边会露出一道白边（所有者 2026-10-27 反馈）。
        """
        item = self.itemFromIndex(index)
        if item is not None and item.data(0, Qt.UserRole) == "group":
            brush = item.background(0)
            if brush.style() != Qt.BrushStyle.NoBrush and brush.color().alpha() > 0:
                color = brush.color()
                color.setAlpha(255)
                painter.fillRect(options.rect, color)
        super().drawRow(painter, options, index)

    @staticmethod
    def _task_index_of(item):
        if item is None or item.data(0, Qt.UserRole) == "group":
            return None
        index = item.data(0, Qt.UserRole + 1)
        return int(index) if index is not None else None

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        self._drag_handle_pressed = False
        self._drag_source_item = None
        self.drag_task_indices = []
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            if item is not None:
                if item.data(0, Qt.UserRole) == "group":
                    # 组没有右侧手柄：整行都能拖（拖动=调整分组归属/层级）
                    self._drag_source_item = item
                else:
                    rect = self.visualItemRect(item)
                    if pos.x() >= rect.right() - 30:
                        self._drag_handle_pressed = True
                        self._drag_source_item = item
        super().mousePressEvent(event)
        if self._drag_handle_pressed and self._drag_source_item is not None:
            source_index = self._task_index_of(self._drag_source_item)
            if source_index is not None:
                selected = sorted({
                    index for item in self.selectedItems()
                    if (index := self._task_index_of(item)) is not None
                })
                if source_index not in selected:
                    selected.append(source_index)
                    selected.sort()
                self.drag_task_indices = selected

    def startDrag(self, supportedActions):
        source = self._drag_source_item
        if not self._drag_handle_pressed and not (
                source is not None and source.data(0, Qt.UserRole) == "group"):
            return
        super().startDrag(supportedActions)

    def _group_id_of(self, item):
        """某个树项所属的组 id：组项返回自己的 id，步骤项返回父组 id，根层返回 ""。"""
        if item is None:
            return ""
        if item.data(0, Qt.UserRole) == "group":
            return str(item.data(0, Qt.UserRole + 1))
        parent = item.parent()
        if parent is not None and parent.data(0, Qt.UserRole) == "group":
            return str(parent.data(0, Qt.UserRole + 1))
        return ""

    def dropEvent(self, event):
        """拖放语义：
        · 组 → 组：把整组挂到目标组下面（拖到空白=回根层）
        · 组 → 步骤：挂到该步骤所在的组
        · 步骤 → 组：加入该组（空组也可以）
        · 步骤 → 步骤：插到目标步骤前后，并改为目标所在的组
        · 步骤 → 空白：移到根层末尾（此前只调整顺序、留在原组，等于无法拖出组）
        """
        target = self.itemAt(event.position().toPoint())
        source = self._drag_source_item or self.currentItem()
        if source is None:
            event.ignore()
            return
        if source.data(0, Qt.UserRole) == "group":
            source_group = str(source.data(0, Qt.UserRole + 1))
            if target is not None and target.data(0, Qt.UserRole) != "group":
                # 拖到某个**步骤**上：把整组插到这个步骤的前/后（可插进父组两步之间）
                target_index = target.data(0, Qt.UserRole + 1)
                if target_index is None:
                    event.ignore()
                    return
                target_rect = self.visualItemRect(target)
                insert_after = event.position().y() >= target_rect.center().y()
                event.accept()
                QTimer.singleShot(0, lambda: self.group_reorder_requested.emit(
                    source_group, int(target_index), insert_after))
                return
            target_group = self._group_id_of(target) if target is not None else ""
            event.accept()
            QTimer.singleShot(0, lambda: self.group_reparent_requested.emit(
                source_group, target_group or None))
            return
        source_index = source.data(0, Qt.UserRole + 1)
        if source_index is None:
            event.ignore()
            return
        if target is not None and target.data(0, Qt.UserRole) == "group":
            target_group = str(target.data(0, Qt.UserRole + 1))
            target_rect = self.visualItemRect(target)
            drop_above = event.position().y() < target_rect.center().y()
            event.accept()
            if drop_above:
                QTimer.singleShot(0, lambda: self.task_before_group_requested.emit(
                    int(source_index), target_group))
            else:
                QTimer.singleShot(0, lambda: self.task_into_group_requested.emit(
                    int(source_index), target_group))
            return
        if target is None:
            event.accept()
            QTimer.singleShot(0, lambda: self.task_to_root_requested.emit(int(source_index)))
            return
        target_index = target.data(0, Qt.UserRole + 1)
        if target_index is None:
            event.ignore()
            return
        target_rect = self.visualItemRect(target)
        insert_after = event.position().y() >= target_rect.center().y()
        target_group = self._group_id_of(target)
        event.accept()
        QTimer.singleShot(0, lambda: self.task_drop_requested.emit(
            int(source_index), int(target_index), insert_after, target_group or None))
class TaskWorker(QObject):
    log = Signal(str)
    execution_started = Signal(dict)
    execution_result = Signal(dict, str)
    completed = Signal(str)
    finished = Signal()

    def __init__(self, tasks, loop, stop_event, pause_event, single_step_event, start_node_id, fallback_steps=None):
        super().__init__()
        self.tasks = tasks
        self.loop = loop
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.single_step_event = single_step_event
        self.start_node_id = start_node_id
        self.fallback_steps = fallback_steps or []

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
                fallback_steps=self.fallback_steps,
            )
        except Exception as exc:
            # 保留完整堆栈，便于定位真实出错位置（此前只记录异常消息，排障困难）。
            self.log.emit(f"脚本异常: {exc}")
            self.log.emit(traceback.format_exc())
            self.completed.emit("failed")
        finally:
            self.finished.emit()
