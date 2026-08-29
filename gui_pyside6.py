# PySide6 主界面：新版任务编辑器、蓝图编辑器和线程化执行控制。
import glob
import json
import os
import sys
import threading
import uuid
import zipfile
from copy import deepcopy

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")

import pygetwindow as gw
from PySide6.QtCore import QObject, QThread, QTimer, QRect, QRectF, QPointF, QLineF, Signal, Slot, Qt
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QBrush, QFont, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QColorDialog,
    QDialog,
    QFormLayout,
    QFileDialog,
    QGroupBox,
    QGridLayout,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import config
from main import reload_templates, run_task_queue
from tasks import (
    DELETED_PRESET_NAMES,
    PRESET_METADATA,
    TASKS,
    USER_PRESETS,
    get_tasks_for_mode,
    load_blueprint_graphs,
    load_blueprint_layouts,
    save_blueprint_graphs,
    save_blueprint_layouts,
    save_presets,
    save_tasks,
)
from nodes import NodeGraph, TaskNode
from core.screen import get_window_rect


class CaptureOverlay(QWidget):
    clicked = Signal(int, int)
    region_selected = Signal(int, int, int, int)
    image_selected = Signal(int, int, int, int)
    too_small = Signal()
    cancelled = Signal()

    def __init__(self, capture_rect, mode, parent=None):
        super().__init__(parent)
        self.capture_rect = capture_rect
        self.mode = mode
        self.start_point = None
        self.current_point = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.update)
        self.setGeometry(QRect(capture_rect["left"], capture_rect["top"], capture_rect["width"], capture_rect["height"]))
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

    def start(self):
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self.refresh_timer.start(50)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.start_point = event.position().toPoint()
            self.current_point = self.start_point
            self.update()

    def mouseMoveEvent(self, event):
        if self.start_point is not None:
            self.current_point = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self.start_point is None:
            return
        end_point = event.position().toPoint()
        start_global = self.mapToGlobal(self.start_point)
        end_global = self.mapToGlobal(end_point)
        if self.mode == "click":
            self.clicked.emit(end_global.x(), end_global.y())
        elif self.mode == "image":
            left = min(start_global.x(), end_global.x())
            top = min(start_global.y(), end_global.y())
            right = max(start_global.x(), end_global.x())
            bottom = max(start_global.y(), end_global.y())
            if right > left and bottom > top:
                self.image_selected.emit(left, top, right, bottom)
            else:
                self.too_small.emit()
        else:
            left = min(start_global.x(), end_global.x()) - self.capture_rect["left"]
            top = min(start_global.y(), end_global.y()) - self.capture_rect["top"]
            right = max(start_global.x(), end_global.x()) - self.capture_rect["left"]
            bottom = max(start_global.y(), end_global.y()) - self.capture_rect["top"]
            if right > left and bottom > top:
                self.region_selected.emit(left, top, right, bottom)
            else:
                self.too_small.emit()
        self.close()

    def closeEvent(self, event):
        self.refresh_timer.stop()
        super().closeEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 46))
        if self.start_point is not None and self.current_point is not None:
            selection = QRect(self.start_point, self.current_point).normalized()
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(selection, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(Qt.cyan, 2))
            painter.drawRect(selection)


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


class BlueprintNodeItem(QGraphicsRectItem):
    PORT_COLORS = {
        "output": "#e5e7eb",   # 执行流（exec），UE5 中为白色连线
        "success": "#22c55e",
        "failure": "#f97316",
        "timeout": "#eab308",
        "true": "#22c55e",
        "false": "#f97316",
        "body": "#38bdf8",
        "exit": "#94a3b8",
        "triggered": "#22c55e",
        "event_timeout": "#eab308",
        "default": "#94a3b8",
    }

    @staticmethod
    def _type_color(task_type):
        return {
            "normal": "#2563eb",
            "advanced": "#d97706",
            "keyboard_move": "#16a34a",
            "key_press": "#7c3aed",
            "drag": "#db2777",
            "condition": "#0ea5e9",
            "switch": "#14b8a6",
            "loop": "#ef4444",
            "event": "#9333ea",
        }.get(task_type, "#475569")

    @staticmethod
    def _dim_color(color_hex):
        color = QColor(color_hex)
        h, s, l, a = color.getHslF()
        l = max(0.08, min(0.85, l * 0.55))
        s = max(0.0, min(1.0, s * 0.45))
        color.setHslF(h, s, l, a)
        return color.name()

    def __init__(self, index, task):
        task_type = task.get("type", "normal")
        if task.get("blueprint_collapsed"):
            height = 44
        elif task_type in ("normal", "advanced"):
            height = 120
        else:
            height = max(92, 34 + len(self.output_ports(task)) * 28)
        super().__init__(0, 0, 230, height)
        self.index = index
        self.task = task
        self.group_id = str(task.get("group_id") or "group_default")
        self.setPen(Qt.NoPen)
        self.setBrush(Qt.NoBrush)
        self.setFlags(QGraphicsRectItem.ItemIsMovable | QGraphicsRectItem.ItemIsSelectable)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self._move_callback = None
        self._connected_outputs = set()
        self._has_input = False
        self._runtime_state = None

    @staticmethod
    def output_ports(task):
        """返回节点输出端口列表，每项为 (名称, 标签, 相对中心偏移)。"""
        if task.get("blueprint_collapsed"):
            return [("output", "顺序", 0)]
        task_type = task.get("type", "normal")
        if task_type == "condition":
            return [("output", "顺序", 0), ("false", "不成立", -20), ("true", "成立", 20)]
        if task_type == "switch":
            ports = [("output", "顺序", 0)]
            cases = list((task.get("switch_cases") or {}).keys())
            for index, case in enumerate(cases):
                ports.append((str(case), str(case), 18 + index * 14))
            ports.append(("default", "默认", -18))
            return ports
        if task_type == "loop":
            return [("output", "顺序", 0), ("exit", "退出", -15), ("body", "循环体", 15)]
        if task_type == "event":
            return [("output", "顺序", 0), ("event_timeout", "超时", -15), ("triggered", "触发", 15)]
        if task_type in ("normal", "advanced"):
            return [("output", "顺序", 0), ("failure", "未识别", -16), ("success", "成功", 16), ("timeout", "超时", -32)]
        return [("output", "顺序", 0)]

    def ports(self):
        return self.output_ports(self.task)

    def port_y(self, offset):
        return self.rect().height() / 2 + offset

    def port_for_name(self, name):
        for spec in self.ports():
            if spec[0] == name:
                return spec
        return None

    def boundingRect(self):
        return super().boundingRect().adjusted(-10, -10, 10, 10)

    def paint(self, painter, option, widget=None):
        rect = self.rect()
        header_height = 24.0
        header_color = QColor(self.task.get("blueprint_color") or self._type_color(self.task.get("type", "normal")))
        selected = self.isSelected()
        collapsed = bool(self.task.get("blueprint_collapsed"))

        # 主体
        if self._runtime_state == "success":
            body_color = QColor("#4f8f68")
        elif self._runtime_state == "timeout":
            body_color = QColor("#a18a4f")
        elif self._runtime_state in {"running", "failed"}:
            body_color = QColor("#a15f5f")
        else:
            body_color = QColor("#1e293b") if self.task.get("enabled", True) else QColor("#374151")
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(body_color))
        painter.drawRoundedRect(rect, 4, 4)

        # 框头（步骤类型 + 序号）
        header_rect = QRectF(rect.left(), rect.top(), rect.width(), header_height)
        painter.setBrush(QBrush(header_color))
        painter.drawRoundedRect(header_rect, 4, 4)
        painter.drawRect(QRectF(rect.left(), rect.top() + header_height / 2, rect.width(), header_height / 2))
        painter.setPen(QPen(QColor("#ffffff")))
        painter.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        painter.drawText(header_rect.adjusted(8, 0, -4, 0), Qt.AlignLeft | Qt.AlignVCenter, f"{self.index + 1:02d}  {self.task.get('type', 'normal')}")

        # 主体基本信息
        if not collapsed:
            painter.setPen(QPen(QColor("#e2e8f0")))
            painter.setFont(QFont("Microsoft YaHei", 9))
            body_rect = QRectF(rect.left() + 8, rect.top() + header_height + 4, rect.width() - 16, rect.height() - header_height - 8)
            description = str(self.task.get("description", self.task.get("template", "未命名步骤")))
            lines = [description[:26]]
            template = str(self.task.get("template", ""))
            if template:
                lines.append(f"模板: {template[:20]}")
            comment = str(self.task.get("blueprint_comment", ""))
            if comment:
                lines.append(comment[:20])
            painter.drawText(body_rect, Qt.AlignLeft | Qt.AlignTop, "\n".join(lines))

        # 边框
        if selected:
            painter.setPen(QPen(QColor("#ffffff"), 2))
        else:
            painter.setPen(QPen(QColor("#475569"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)

        # 端口（未连接在原色上变灰，已连接显示正常颜色）
        center_y = rect.height() / 2
        input_base = "#f8fafc"
        input_color = QColor(input_base) if self._has_input else QColor(self._dim_color(input_base))
        input_outline = QColor("#ffffff") if self._has_input else QColor("#334155")
        painter.setBrush(QBrush(input_color))
        painter.setPen(QPen(input_outline, 1))
        painter.drawEllipse(-5, center_y - 5, 10, 10)
        painter.setFont(QFont("Microsoft YaHei", 7))
        for name, label, offset in self.ports():
            base = self.PORT_COLORS.get(name, "#94a3b8")
            if name in self._connected_outputs:
                color = base
                outline = QColor("#ffffff")
            else:
                color = self._dim_color(base)
                outline = QColor("#334155")
            painter.setBrush(QBrush(QColor(color)))
            painter.setPen(QPen(outline, 1))
            y = self.port_y(offset)
            painter.drawEllipse(rect.width() - 5, y - 5, 10, 10)
            painter.setPen(QPen(QColor(color)))
            painter.drawText(QRectF(rect.width() - 80, y - 8, 70, 16), Qt.AlignRight | Qt.AlignVCenter, label)

    def output_at(self, point):
        if point.x() < self.rect().width() - 45:
            return None
        for name, _label, offset in self.ports():
            if abs(point.y() - self.port_y(offset)) <= 14:
                return name
        return None

    def input_at(self, point):
        if point.x() > 18:
            return None
        center_y = self.rect().height() / 2
        if abs(point.y() - center_y) <= 14:
            return "input"
        return None

    def set_move_callback(self, callback):
        self._move_callback = callback

    def set_connection_state(self, connected_outputs, has_input):
        self._connected_outputs = set(connected_outputs)
        self._has_input = bool(has_input)
        self.update()

    def set_runtime_state(self, state):
        self._runtime_state = state
        self.update()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            # 选中时置顶，取消选中后恢复默认层级
            self.setZValue(5 if value else 0)
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and self._move_callback is not None:
            self._move_callback(self)
        return super().itemChange(change, value)


class BlueprintWireItem(QGraphicsPathItem):
    """UE5 风格连线：执行流（exec）为白色并带箭头，分支为彩色数据线；支持转折点。"""

    def __init__(self):
        super().__init__()
        self.is_exec = False
        self._color = QColor("#94a3b8")
        self._start = QPointF(0, 0)
        self._end = QPointF(0, 0)
        self._arrow = QPolygonF()
        self.bends = []
        self._width = 2
        self._highlighted = False
        self.setZValue(-1)

    def update_wire(self, start, end, color, is_exec, bends=None, width=2, highlighted=False):
        self.is_exec = is_exec
        self._start = QPointF(start)
        self._end = QPointF(end)
        self.bends = [QPointF(float(point[0]), float(point[1])) for point in (bends or [])]
        self._width = width
        self._highlighted = bool(highlighted)

        path = QPainterPath(self._start)
        if self.bends:
            for bend in self.bends:
                path.lineTo(bend)
            path.lineTo(self._end)
        else:
            sign = 1.0 if self._end.x() >= self._start.x() else -1.0
            dx = max(40.0, abs(self._end.x() - self._start.x()) * 0.5) * sign
            path.cubicTo(self._start.x() + dx, self._start.y(), self._end.x() - dx, self._end.y(), self._end.x(), self._end.y())
        self.setPath(path)

        self._color = QColor(color)
        self.setPen(QPen(self._color, width))
        self.update()

        # 箭头方向取连线末端实际切线，保证箭头始终贴合线体
        if self.bends:
            tangent = self._end - self.bends[-1]
        else:
            # 贝塞尔曲线末端切线为水平方向（进入目标输入口时水平）
            sign = 1.0 if self._end.x() >= self._start.x() else -1.0
            tangent = QPointF(sign, 0.0)
        length = (tangent.x() ** 2 + tangent.y() ** 2) ** 0.5
        if length < 1e-6:
            tangent = QPointF(1.0, 0.0)
            length = 1.0
        direction = QPointF(tangent.x() / length, tangent.y() / length)
        perpendicular = QPointF(-direction.y(), direction.x())
        arrow_len = 10.0
        arrow_half = 4.0
        base = QPointF(self._end.x() - direction.x() * arrow_len, self._end.y() - direction.y() * arrow_len)
        self._arrow = QPolygonF([
            self._end,
            QPointF(base.x() + perpendicular.x() * arrow_half, base.y() + perpendicular.y() * arrow_half),
            QPointF(base.x() - perpendicular.x() * arrow_half, base.y() - perpendicular.y() * arrow_half),
        ])

    def bend_positions(self):
        return [(bend.x(), bend.y()) for bend in self.bends]

    def boundingRect(self):
        return super().boundingRect().adjusted(-14, -14, 14, 14)

    def paint(self, painter, option, widget=None):
        if self._highlighted:
            # 高亮仅在外围描一圈白色细边，不改变主线颜色、不超出线太多
            painter.setPen(QPen(QColor("#ffffff"), self._width + 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(self.path())
        super().paint(painter, option, widget)
        # 每根线的尽头都绘制小箭头，指向目标节点
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(self._color))
        painter.drawPolygon(self._arrow)


class BendHandleItem(QGraphicsEllipseItem):
    """连线转折点手柄，拖动可改变连线路径。"""

    def __init__(self, move_callback=None):
        super().__init__(-5, -5, 10, 10)
        self._move_callback = move_callback
        self.setBrush(QBrush(QColor("#ffffff")))
        self.setPen(QPen(QColor("#0f172a"), 1))
        self.setZValue(10)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and self._move_callback is not None:
            self._move_callback(self)
        return super().itemChange(change, value)


class BlueprintScene(QGraphicsScene):
    """UE5 风格深色点阵网格背景。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid_size = 20

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        painter.fillRect(rect, QColor("#1a1d24"))
        painter.setPen(QPen(QColor(82, 86, 100, 150), 1))
        grid = self.grid_size
        left = int(rect.left()) - (int(rect.left()) % grid)
        top = int(rect.top()) - (int(rect.top()) % grid)
        x = left
        while x < rect.right():
            y = top
            while y < rect.bottom():
                painter.drawPoint(x, y)
                y += grid
            x += grid


class BlueprintGroupItem(QGraphicsRectItem):
    HEADER_HEIGHT = 18.0

    def __init__(self, group_id, rect, node_items, name="", color="#38bdf8", info=""):
        super().__init__(*rect)
        self.group_id = group_id
        self.node_items = node_items
        self._press_scene_pos = None
        self._drag_from_header = False
        self._release_callback = None
        self._move_callback = None
        self._name = name
        self._info = info
        self._color = QColor(color)
        self.setPen(Qt.NoPen)
        self.setBrush(Qt.NoBrush)
        self.setZValue(-2)
        self.setFlags(QGraphicsRectItem.ItemIsSelectable)

    def set_release_callback(self, callback):
        self._release_callback = callback

    def set_move_callback(self, callback):
        self._move_callback = callback

    def set_info(self, info):
        self._info = info
        self.update()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            # 选中组时置顶，取消选中后回到节点之下
            self.setZValue(5 if value else -2)
        return super().itemChange(change, value)

    def shape(self):
        # 仅组头参与命中，透明组身不拦截点击，避免组置顶后挡住组内节点
        path = QPainterPath()
        rect = self.rect()
        path.addRect(QRectF(rect.left(), rect.top(), rect.width(), self.HEADER_HEIGHT))
        return path

    def paint(self, painter, option, widget=None):
        rect = self.rect()
        header_height = self.HEADER_HEIGHT
        color = self._color
        # 透明背景 + 虚线边框
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
        painter.drawRect(rect)
        # 组头
        header_rect = QRectF(rect.left(), rect.top(), rect.width(), header_height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawRect(header_rect)
        # 组头文字（组名 + 基本信息）
        painter.setPen(QPen(QColor("#ffffff")))
        painter.setFont(QFont("Microsoft YaHei", 8, QFont.Bold))
        text = self._name if not self._info else f"{self._name} · {self._info}"
        painter.drawText(header_rect.adjusted(6, 0, -4, 0), Qt.AlignLeft | Qt.AlignVCenter, text)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_scene_pos = event.scenePos()
            scene = self.scene()
            if scene is not None:
                scene.clearSelection()
            self.setSelected(True)
            # 仅点击组头时才允许拖动整组，避免误拖动组内空白区域
            self._drag_from_header = (event.pos().y() - self.rect().top() <= self.HEADER_HEIGHT)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_scene_pos is not None and self._drag_from_header:
            delta = event.scenePos() - self._press_scene_pos
            if self.node_items:
                for node in self.node_items:
                    node.moveBy(delta.x(), delta.y())
            else:
                # 空组：直接移动组头并持久化位置
                rect = self.rect()
                self.setRect(rect.left() + delta.x(), rect.top() + delta.y(), rect.width(), rect.height())
                if self._move_callback is not None:
                    self._move_callback(self, delta.x(), delta.y())
            self._press_scene_pos = event.scenePos()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._press_scene_pos is not None:
            self._press_scene_pos = None
            self._drag_from_header = False
            if self._release_callback is not None:
                self._release_callback()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class BlueprintView(QGraphicsView):
    zoom_changed = Signal(float)
    connection_requested = Signal(int, int, str)
    interaction_finished = Signal()
    group_toggle_requested = Signal(str)
    node_toggle_requested = Signal(int)
    wire_clicked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pending_connection = None
        self._connection_preview = None
        self._panning = False
        self._pan_last = None

    def _node_item_from(self, item):
        if isinstance(item, BlueprintNodeItem):
            return item
        if isinstance(item, QGraphicsTextItem):
            parent = item.parentItem()
            if isinstance(parent, BlueprintNodeItem):
                return parent
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_last = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        scene_position = self.mapToScene(event.position().toPoint())
        item = self.scene().itemAt(scene_position, self.transform())
        if isinstance(item, BlueprintWireItem):
            self.wire_clicked.emit(item)
            event.accept()
            return
        node = self._node_item_from(item)
        if node is not None:
            local = node.mapFromScene(scene_position)
            output = node.output_at(local)
            if output is not None:
                self.pending_connection = (node, output, "output")
                event.accept()
                return
            if node.input_at(local) is not None:
                self.pending_connection = (node, "input", "input")
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning and self._pan_last is not None:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        if self.pending_connection is not None:
            self._update_connection_preview(event.position())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _connection_start_position(self, node, side, port):
        if side == "output":
            spec = node.port_for_name(port) if port else None
            sy = node.port_y(spec[2]) if spec else node.rect().height() / 2
            return QPointF(node.x() + node.rect().width(), node.y() + sy)
        return QPointF(node.x(), node.y() + node.rect().height() / 2)

    def _node_at_scene_pos(self, scene_pos):
        # 预览线层级最高，会挡住节点，命中测试时先临时隐藏
        preview = self._connection_preview
        if preview is not None:
            preview.setVisible(False)
        try:
            item = self.scene().itemAt(scene_pos, self.transform())
            return self._node_item_from(item)
        finally:
            if preview is not None:
                preview.setVisible(True)

    def _connection_target(self, scene_pos, source_node, side):
        target = self._node_at_scene_pos(scene_pos)
        if target is None or target is source_node:
            return None, None
        local = target.mapFromScene(scene_pos)
        if not target.rect().adjusted(-16, -16, 16, 16).contains(local):
            return None, None
        if side == "output":
            return target, "input"
        best = None
        best_dist = 1e9
        for name, _label, offset in target.ports():
            dist = abs(local.y() - target.port_y(offset))
            if dist < best_dist:
                best_dist = dist
                best = name
        return target, best

    def _update_connection_preview(self, view_pos):
        if self.pending_connection is None:
            return
        node, port, side = self.pending_connection
        start = self._connection_start_position(node, side, port)
        scene_pos = self.mapToScene(view_pos.toPoint())
        target, target_port = self._connection_target(scene_pos, node, side)
        if target is not None:
            if side == "output":
                end = self._connection_start_position(target, "input", None)
            else:
                end = self._connection_start_position(target, "output", target_port)
        else:
            end = scene_pos
        color = QColor("#22c55e") if target is not None else QColor("#38bdf8")
        if self._connection_preview is None:
            self._connection_preview = QGraphicsLineItem()
            self._connection_preview.setZValue(20)
            self._connection_preview.setAcceptedMouseButtons(Qt.NoButton)
            self.scene().addItem(self._connection_preview)
        self._connection_preview.setPen(QPen(color, 2, Qt.DashLine))
        self._connection_preview.setLine(QLineF(start, end))

    def _clear_connection_preview(self):
        if self._connection_preview is not None:
            self.scene().removeItem(self._connection_preview)
            self._connection_preview = None

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self._pan_last = None
            self.unsetCursor()
            event.accept()
            return
        if self.pending_connection is not None:
            scene_position = self.mapToScene(event.position().toPoint())
            source_node, port, side = self.pending_connection
            source_index = source_node.index
            self.pending_connection = None
            self._clear_connection_preview()
            target, target_port = self._connection_target(scene_position, source_node, side)
            if target is not None and target.index != source_index:
                if side == "output":
                    self.connection_requested.emit(source_index, target.index, port)
                else:
                    self.connection_requested.emit(target.index, source_index, target_port)
            self.interaction_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        self.interaction_finished.emit()

    def mouseDoubleClickEvent(self, event):
        scene_position = self.mapToScene(event.position().toPoint())
        item = self.scene().itemAt(scene_position, self.transform())
        if isinstance(item, BlueprintGroupItem):
            scene_rect = item.sceneBoundingRect()
            if scene_position.y() - scene_rect.top() <= item.HEADER_HEIGHT:
                self.group_toggle_requested.emit(item.group_id)
            event.accept()
            return
        node = self._node_item_from(item)
        if node is not None:
            self.node_toggle_requested.emit(node.index)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            # 以鼠标位置为锚点缩放，避免放大后内容飞离当前视野
            self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
            self.scale(factor, factor)
            self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
            self.zoom_changed.emit(self.transform().m11())
            event.accept()
            return
        super().wheelEvent(event)


class BlueprintWindow(QMainWindow):
    _BASIC_FIELDS = {
        "id", "type", "mode", "enabled", "template", "templates", "description",
        "threshold", "timeout", "wait_timeout", "after_wait", "click", "optional", "required",
        "click_x", "click_y", "click_position", "match_rect", "search_rect", "match_rects",
        "next_template", "next_templates", "wait_for", "group_id", "group_name", "group_color",
        "flow_next", "flow_next_disabled", "blueprint_bends", "blueprint_collapsed",
        "blueprint_color", "blueprint_comment", "detour_enabled", "detour_steps",
        "detour_jump_to", "detour_success_jump_to",
    }

    def __init__(self, tasks, layout_data, save_callback, group_metadata=None, parent=None, execution_states=None):
        super().__init__(parent)
        self.tasks = tasks
        self.save_callback = save_callback
        self.layout_data = layout_data if isinstance(layout_data, dict) else {}
        self.group_metadata = group_metadata if isinstance(group_metadata, dict) else {}
        self.history = []
        self.redo_history = []
        self.clipboard = []
        self.grid_snap = False
        self.collapsed_groups = self._load_collapsed_groups()
        self.current_index = -1
        self.current_group_id = None
        self._pending_group_color = None
        self.selected_edge = None
        self._refreshing = False
        self._scene_rect_pending = False
        self.capture_overlay = None
        self._capture_callback = None
        self.execution_states = dict(execution_states or {})
        self.setWindowTitle("蓝图流程 - PySide6")
        self.resize(1480, 860)

        self.scene = BlueprintScene(self)
        self.view = BlueprintView(self)
        self.view.setScene(self.scene)
        self.view.setDragMode(QGraphicsView.RubberBandDrag)
        self.view.setBackgroundBrush(QBrush(QColor("#1a1d24")))
        self.view.setRenderHint(QPainter.Antialiasing, True)
        self.view.connection_requested.connect(self.connect_nodes)
        self.view.interaction_finished.connect(self.sync_positions)
        self.view.group_toggle_requested.connect(self.toggle_group)
        self.view.node_toggle_requested.connect(self.toggle_collapse)
        self.view.wire_clicked.connect(self._on_wire_clicked)

        # 画布面板：工具栏 + 视图
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        for label, handler in (
            ("刷新流程图", self.refresh_flowchart),
            ("应用蓝图", self.apply_blueprint),
            ("检查蓝图", self.validate_blueprint),
            ("自动排列", self.auto_arrange),
            ("对齐选中", self.align_selected),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            toolbar_layout.addWidget(button)
        self.grid_snap_checkbox = QCheckBox("网格吸附")
        self.grid_snap_checkbox.toggled.connect(self.set_grid_snap)
        toolbar_layout.addWidget(self.grid_snap_checkbox)
        toolbar_layout.addWidget(QLabel("点击步骤编辑，拖动步骤调整布局"))
        toolbar_layout.addStretch(1)

        canvas_panel = QGroupBox("蓝图流程")
        canvas_layout = QVBoxLayout(canvas_panel)
        canvas_layout.addWidget(toolbar)
        canvas_layout.addWidget(self.view, 1)

        # 编辑面板
        editor_panel = QGroupBox("当前步骤设置")
        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor_content = QWidget()
        self.editor_layout = QVBoxLayout(editor_content)
        editor_scroll.setWidget(editor_content)
        editor_panel_layout = QVBoxLayout(editor_panel)
        editor_panel_layout.addWidget(editor_scroll)
        self._build_editor_panel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(canvas_panel)
        splitter.addWidget(editor_panel)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([900, 520])
        self.setCentralWidget(splitter)

        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.show_context_menu)
        self.scene.selectionChanged.connect(self._on_selection_changed)

        for shortcut, handler in (
            (QKeySequence.Undo, self.undo),
            (QKeySequence.Redo, self.redo),
            (QKeySequence.Copy, self.copy_selected),
            (QKeySequence.Paste, self.paste_tasks),
            (QKeySequence.Delete, self.delete_selected),
        ):
            action = QAction(self.view)
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            action.triggered.connect(handler)
            self.view.addAction(action)

        self.refresh()

    def _build_editor_panel(self):
        self.selected_label = QLabel("未选择步骤")
        self.selected_label.setWordWrap(True)
        self.editor_layout.addWidget(self.selected_label)

        self.template_preview = QLabel()
        self.template_preview.setAlignment(Qt.AlignCenter)
        self.template_preview.setFixedSize(180, 110)
        self.template_preview.setStyleSheet("border: 1px solid #cbd5e1; background: #f8fafc;")
        self.template_preview.setVisible(False)
        self.editor_layout.addWidget(self.template_preview)

        self.editor_actions = QWidget()
        action_layout = QHBoxLayout(self.editor_actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        for label, handler in (
            ("绑定图片", self.select_template_file),
            ("记录点击点", self.start_click_capture),
            ("框选识别区域", self.start_region_capture),
            ("清空识别区域", self.clear_match_region),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            action_layout.addWidget(button)
        self.apply_button = QPushButton("应用修改")
        self.apply_button.clicked.connect(self._apply_editor)
        action_layout.addWidget(self.apply_button)
        self.editor_layout.addWidget(self.editor_actions)

        name_form = QFormLayout()
        self.description_edit = QLineEdit()
        self.enabled_checkbox = QCheckBox("启用步骤")
        name_form.addRow("步骤名称:", self.description_edit)
        name_form.addRow("状态:", self.enabled_checkbox)
        self.editor_layout.addLayout(name_form)

        self.recognition_group = QWidget()
        recognition_layout = QVBoxLayout(self.recognition_group)
        recognition_layout.setContentsMargins(0, 0, 0, 0)
        self.template_edit = QLineEdit()
        self.threshold_edit = QLineEdit()
        self.timeout_edit = QLineEdit()
        self.after_wait_edit = QLineEdit()
        self.offset_x_edit = QLineEdit()
        self.offset_y_edit = QLineEdit()
        self.click_x_edit = QLineEdit()
        self.click_y_edit = QLineEdit()
        self.match_rect_edit = QLineEdit()
        self.next_template_edit = QLineEdit()
        self.wait_for_combo = QComboBox()
        self.wait_for_combo.addItems(["1. 画面结果变化", "2. 等待目标模板出现", "3. 画面变化后目标结果出现"])
        self.click_checkbox = QCheckBox("执行点击")
        self.match_required_checkbox = QCheckBox("必须识别到图片再点击")
        self.optional_checkbox = QCheckBox("可选步骤（跳过）")

        def add_row(label, widget):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(label), 0)
            row.addWidget(widget, 1)
            recognition_layout.addLayout(row)

        add_row("模板名:", self.template_edit)
        add_row("匹配阈值:", self.threshold_edit)
        add_row("超时(秒):", self.timeout_edit)
        add_row("完成后等待(秒):", self.after_wait_edit)
        add_row("X偏移:", self.offset_x_edit)
        add_row("Y偏移:", self.offset_y_edit)
        add_row("点击X:", self.click_x_edit)
        add_row("点击Y:", self.click_y_edit)
        add_row("识别区域(左上,右下):", self.match_rect_edit)

        next_row = QHBoxLayout()
        next_row.setContentsMargins(0, 0, 0, 0)
        next_row.addWidget(QLabel("下一模板:"), 0)
        next_row.addWidget(self.next_template_edit, 1)
        next_button = QPushButton("选择图片")
        next_button.clicked.connect(self.select_next_template_file)
        next_row.addWidget(next_button)
        next_capture_button = QPushButton("手动框选图片")
        next_capture_button.clicked.connect(self.start_next_template_capture)
        next_row.addWidget(next_capture_button)
        next_region_button = QPushButton("框选出现位置")
        next_region_button.clicked.connect(self.start_next_region_capture)
        next_row.addWidget(next_region_button)
        recognition_layout.addLayout(next_row)

        wait_row = QHBoxLayout()
        wait_row.setContentsMargins(0, 0, 0, 0)
        wait_row.addWidget(QLabel("等待方式:"), 0)
        wait_row.addWidget(self.wait_for_combo, 1)
        recognition_layout.addLayout(wait_row)

        options_row = QHBoxLayout()
        options_row.setContentsMargins(0, 0, 0, 0)
        options_row.addWidget(self.click_checkbox)
        options_row.addWidget(self.match_required_checkbox)
        options_row.addWidget(self.optional_checkbox)
        self.detour_button = QPushButton("迂回")
        self.detour_button.clicked.connect(self.open_detour_editor)
        options_row.addWidget(self.detour_button)
        options_row.addStretch(1)
        recognition_layout.addLayout(options_row)

        self.editor_layout.addWidget(self.recognition_group)

        self.click_until_group = QGroupBox("持续点击设置")
        click_until_layout = QVBoxLayout(self.click_until_group)
        click_until_layout.setContentsMargins(0, 0, 0, 0)
        self.click_until_template_edit = QLineEdit()
        self.click_until_interval_edit = QLineEdit()
        self.click_until_stop_delay_edit = QLineEdit()
        self.click_until_timeout_edit = QLineEdit()
        self.click_until_continue_checkbox = QCheckBox("超时后继续执行")
        self.click_until_stop_on_change_checkbox = QCheckBox("画面变化视为成功")

        def add_click_until_row(label, widget):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(label), 0)
            row.addWidget(widget, 1)
            click_until_layout.addLayout(row)

        add_click_until_row("模板名(逗号分隔):", self.click_until_template_edit)
        add_click_until_row("点击间隔(秒):", self.click_until_interval_edit)
        add_click_until_row("识别后停止延时(秒):", self.click_until_stop_delay_edit)
        add_click_until_row("超时(秒):", self.click_until_timeout_edit)
        click_until_layout.addWidget(self.click_until_continue_checkbox)
        click_until_layout.addWidget(self.click_until_stop_on_change_checkbox)
        self.click_until_group.setVisible(False)
        self.editor_layout.addWidget(self.click_until_group)

        self.special_form = QFormLayout()
        self.special_edits = {}
        self.editor_layout.addLayout(self.special_form)

        # 组设置表单（选中组时显示）
        self.group_form = QGroupBox("组设置")
        group_form_layout = QFormLayout(self.group_form)
        self.group_name_edit = QLineEdit()
        self.group_color_button = QPushButton("选择组颜色")
        self.group_color_button.clicked.connect(self._pick_group_color)
        group_form_layout.addRow("组名称:", self.group_name_edit)
        group_form_layout.addRow("组颜色:", self.group_color_button)
        self.group_apply_button = QPushButton("应用组设置")
        self.group_apply_button.clicked.connect(self._apply_group_editor)
        group_form_layout.addRow(self.group_apply_button)
        self.group_form.setVisible(False)
        self.editor_layout.addWidget(self.group_form)

        self.editor_layout.addStretch(1)

    def _snapshot(self):
        return {
            "tasks": deepcopy(self.tasks),
            "layout": deepcopy(self.layout_data),
            "group_metadata": deepcopy(self.group_metadata),
            "collapsed_groups": set(self.collapsed_groups),
        }

    def _push_history(self):
        self.history.append(self._snapshot())
        self.redo_history.clear()
        if len(self.history) > 50:
            self.history.pop(0)

    def _restore_snapshot(self, snapshot):
        self.tasks[:] = deepcopy(snapshot["tasks"])
        self.layout_data.clear()
        self.layout_data.update(deepcopy(snapshot["layout"]))
        self.group_metadata.clear()
        self.group_metadata.update(deepcopy(snapshot.get("group_metadata", {})))
        self.collapsed_groups = set(snapshot.get("collapsed_groups", set()))
        self.refresh()

    def undo(self):
        if not self.history:
            return
        self.redo_history.append(self._snapshot())
        self._restore_snapshot(self.history.pop())

    def redo(self):
        if not self.redo_history:
            return
        self.history.append(self._snapshot())
        self._restore_snapshot(self.redo_history.pop())

    def _load_collapsed_groups(self):
        expanded = self.group_metadata.get("expanded", {})
        if isinstance(expanded, dict):
            return {str(gid) for gid, value in expanded.items() if value is False}
        return set()

    def _group_children(self):
        value = self.group_metadata.get("children", {})
        return value if isinstance(value, dict) else {}

    def _group_parents(self):
        value = self.group_metadata.get("parents", {})
        return value if isinstance(value, dict) else {}

    def _group_names(self):
        value = self.group_metadata.get("names", {})
        return value if isinstance(value, dict) else {}

    def _group_colors(self):
        value = self.group_metadata.get("colors", {})
        return value if isinstance(value, dict) else {}

    def _group_order(self):
        value = self.group_metadata.get("order", [])
        return value if isinstance(value, list) else []

    def _group_descendants(self, group_id):
        children = self._group_children()
        result = []
        for child in children.get(str(group_id), []):
            result.append(str(child))
            result.extend(self._group_descendants(child))
        return result

    def _group_node_indices(self, group_id):
        return [i for i, task in enumerate(self.tasks) if str(task.get("group_id") or "group_default") == str(group_id)]

    def _group_all_node_indices(self, group_id):
        indices = list(self._group_node_indices(group_id))
        for child in self._group_descendants(group_id):
            indices.extend(self._group_node_indices(child))
        return indices

    def _node_hidden(self, task):
        group_id = str(task.get("group_id") or "group_default")
        parents = self._group_parents()
        current = group_id
        seen = set()
        while current and current != "group_default" and current not in seen:
            seen.add(current)
            if current in self.collapsed_groups:
                return True
            parent = parents.get(current)
            if parent is None or str(parent) == current:
                break
            current = str(parent)
        return False

    def _resolve_number(self, value):
        try:
            target = int(value) - 1
        except (TypeError, ValueError):
            return None
        return target if 0 <= target < len(self.tasks) else None

    def _connections(self):
        id_to_index = {str(task.get("id")): i for i, task in enumerate(self.tasks) if task.get("id") is not None}
        connections = []
        for index, task in enumerate(self.tasks):
            flow = task.get("flow_next")
            target = id_to_index.get(str(flow)) if flow is not None else None
            if target is not None:
                connections.append((index, "output", target, False))
            for key, output in (
                ("condition_true_jump_to", "true"),
                ("condition_false_jump_to", "false"),
                ("detour_success_jump_to", "success"),
                ("detour_jump_to", "failure"),
                ("timeout_jump_to", "timeout"),
                ("event_timeout_target", "event_timeout"),
                ("loop_target", "body"),
                ("loop_exit_target", "exit"),
                ("event_trigger_target", "triggered"),
                ("switch_default_jump_to", "default"),
            ):
                target = self._resolve_number(task.get(key))
                if target is not None:
                    connections.append((index, output, target, False))
            for case, target_number in (task.get("switch_cases") or {}).items():
                target = self._resolve_number(target_number)
                if target is not None:
                    connections.append((index, str(case), target, False))
            if flow is None and not task.get("flow_next_disabled") and index + 1 < len(self.tasks):
                connections.append((index, "output", index + 1, True))
        return connections

    def refresh_flowchart(self):
        self.reset_execution_states()
        self.refresh()

    def refresh(self):
        selected_indices = {
            item.index for item in self.scene.selectedItems() if isinstance(item, BlueprintNodeItem)
        }
        self._refreshing = True
        try:
            self.scene.clear()
            self._node_items = {}
            self._group_items = {}
            self._edges = []
            positions = self.layout_data.setdefault("positions", {})
            node_items = {}
            for index, task in enumerate(self.tasks):
                item = BlueprintNodeItem(index, task)
                position = positions.get(str(index), positions.get(index))
                if isinstance(position, (list, tuple)) and len(position) == 2:
                    item.setPos(float(position[0]), float(position[1]))
                else:
                    item.setPos(40 + (index % 3) * 280, 40 + (index // 3) * 130)
                self.scene.addItem(item)
                item.set_runtime_state(self.execution_states.get(str(task.get("id"))))
                item.setVisible(not self._node_hidden(task))
                item.setSelected(index in selected_indices)
                node_items[index] = item

            self._node_items = node_items
            # 计算每个节点的连接状态（已连接端口高亮，未连接置灰）
            connected_outputs = {index: set() for index in range(len(self.tasks))}
            connected_inputs = set()
            for source_index, output, target_index, _is_default in self._connections():
                connected_outputs[source_index].add(output)
                connected_inputs.add(target_index)
            for index, item in node_items.items():
                item.set_connection_state(connected_outputs.get(index, set()), index in connected_inputs)

            self._render_groups(node_items)
            self._render_connections(node_items)
            for item in node_items.values():
                item.set_move_callback(self._on_node_moved)
            self.scene.setSceneRect(self._visible_scene_rect().adjusted(-80, -80, 80, 80))
        finally:
            self._refreshing = False
        self._refresh_editor_from_selection()

    def update_execution_state(self, task_id, state):
        task_key = str(task_id)
        self.execution_states[task_key] = state
        for item in self._node_items.values():
            if str(item.task.get("id")) == task_key:
                item.set_runtime_state(state)
                break

    def reset_execution_states(self):
        self.execution_states.clear()
        for item in self._node_items.values():
            item.set_runtime_state(None)

    def _visible_scene_rect(self):
        rect = None
        for item in self.scene.items():
            if item.isVisible():
                bounds = item.sceneBoundingRect()
                rect = bounds if rect is None else rect.united(bounds)
        return rect or QRectF(0, 0, 400, 300)

    def _schedule_scene_rect_update(self):
        # 拖动过程中延后更新场景范围，避免在 itemChange 内同步改场景导致闪退/跳动
        if self._scene_rect_pending:
            return
        self._scene_rect_pending = True
        QTimer.singleShot(0, self._update_scene_rect_deferred)

    def _update_scene_rect_deferred(self):
        self._scene_rect_pending = False
        rect = self.scene.sceneRect()
        bounds = self._visible_scene_rect().adjusted(-120, -120, 120, 120)
        if not rect.contains(bounds):
            center = self.view.mapToScene(self.view.viewport().rect().center())
            self.scene.setSceneRect(rect.united(bounds))
            self.view.centerOn(center)

    def _render_groups(self, node_items):
        children = self._group_children()
        names = self._group_names()
        colors = self._group_colors()
        order = self._group_order()
        group_positions = self.layout_data.get("group_positions", {})
        if not isinstance(group_positions, dict):
            group_positions = {}
        rendered = set()
        self._group_items = {}

        def bounds_for(group_id):
            indices = self._group_all_node_indices(group_id)
            if indices:
                left = min(node_items[i].x() for i in indices) - 18
                top = min(node_items[i].y() for i in indices) - 36
                right = max(node_items[i].x() + node_items[i].rect().width() for i in indices) + 18
                bottom = max(node_items[i].y() + node_items[i].rect().height() for i in indices) + 18
                if str(group_id) in self.collapsed_groups:
                    return left, top, left + 220, top + 18
                return left, top, right, bottom
            # 空组：使用存储位置显示组头
            pos = group_positions.get(str(group_id))
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                x, y = float(pos[0]), float(pos[1])
            else:
                x, y = 80.0, 80.0 + 40.0 * len(group_positions)
            return x, y, x + 220, y + 18

        def render(group_id):
            group_id = str(group_id)
            if group_id == "group_default" or group_id in rendered:
                return
            rendered.add(group_id)
            bounds = bounds_for(group_id)
            if bounds is not None:
                left, top, right, bottom = bounds
                group_nodes = [node_items[i] for i in self._group_all_node_indices(group_id) if i in node_items]
                name = names.get(group_id, group_id)
                color = colors.get(group_id, "#38bdf8")
                step_count = len(self._group_all_node_indices(group_id))
                box = BlueprintGroupItem(
                    group_id,
                    (left, top, right - left, bottom - top),
                    group_nodes,
                    name=str(name),
                    color=color,
                    info=f"{step_count} 步骤",
                )
                box.set_release_callback(self.save_layout)
                box.set_move_callback(self._on_empty_group_moved)
                self.scene.addItem(box)
                self._group_items[group_id] = box
            if group_id not in self.collapsed_groups:
                for child in children.get(group_id, []):
                    render(child)

        for group_id in order:
            render(group_id)
        known = {str(task.get("group_id") or "group_default") for task in self.tasks}
        for group_id in known:
            render(group_id)

    def _edge_kind(self, output, is_default, source_type):
        if output == "output":
            return "default" if is_default else "flow"
        if output == "success":
            return "condition_true" if source_type == "condition" else "detour_success"
        if output == "failure":
            return "condition_false" if source_type == "condition" else "detour_failure"
        if output == "timeout":
            return "timeout"
        if output == "true":
            return "condition_true"
        if output == "false":
            return "condition_false"
        return output

    def _collapsed_group_for(self, index):
        task = self.tasks[index]
        group_id = str(task.get("group_id") or "group_default")
        parents = self._group_parents()
        current = group_id
        seen = set()
        while current and current != "group_default" and current not in seen:
            seen.add(current)
            if current in self.collapsed_groups:
                return current
            parent = parents.get(current)
            if parent is None or str(parent) == current:
                break
            current = str(parent)
        return None

    def _node_endpoint(self, index, side, output=None):
        node = self._node_items.get(index)
        if node is None:
            return QPointF(0, 0)
        if node.isVisible():
            if side == "output":
                spec = node.port_for_name(output) if output else None
                sy = node.port_y(spec[2]) if spec else node.rect().height() / 2
                return QPointF(node.x() + node.rect().width(), node.y() + sy)
            return QPointF(node.x(), node.y() + node.rect().height() / 2)
        # 隐藏节点 → 路由到折叠组框
        group_id = self._collapsed_group_for(index)
        box = self._group_items.get(group_id) if group_id else None
        if box is not None:
            rect = box.sceneBoundingRect()
            if side == "output":
                return QPointF(rect.right(), rect.center().y())
            return QPointF(rect.left(), rect.center().y())
        return QPointF(node.x(), node.y() + node.rect().height() / 2)

    def _render_connections(self, node_items):
        self._edges = []
        for source_index, output, target_index, is_default in self._connections():
            if source_index not in node_items or target_index not in node_items:
                continue
            source = node_items[source_index]
            target = node_items[target_index]
            source_collapsed = self._collapsed_group_for(source_index)
            target_collapsed = self._collapsed_group_for(target_index)
            if source_collapsed is not None and source_collapsed == target_collapsed:
                continue
            wire = BlueprintWireItem()
            self.scene.addItem(wire)
            edge_kind = self._edge_kind(output, is_default, self.tasks[source_index].get("type", "normal"))
            bends = list((self.tasks[source_index].get("blueprint_bends") or {}).get(edge_kind, []))
            bends = [bend for bend in bends if isinstance(bend, (list, tuple)) and len(bend) == 2]
            edge = {
                "wire": wire,
                "source": source,
                "target": target,
                "output": output,
                "is_default": is_default,
                "edge_kind": edge_kind,
                "bends": bends,
                "bend_handles": [],
            }
            self._edges.append(edge)
            self._apply_wire_geometry(edge)

    def _on_node_moved(self, node):
        positions = self.layout_data.setdefault("positions", {})
        positions[str(node.index)] = (node.x(), node.y())
        self._update_edges()
        self._sync_group_bounds()
        self._schedule_scene_rect_update()

    def _update_edges(self):
        for edge in self._edges:
            self._apply_wire_geometry(edge)

    def _apply_wire_geometry(self, edge):
        output = edge["output"]
        is_exec = output == "output"
        highlighted = (edge is self.selected_edge) or edge["source"].isSelected() or edge["target"].isSelected()
        color = self._wire_color(output, edge["is_default"])
        start = self._node_endpoint(edge["source"].index, "output", output)
        end = self._node_endpoint(edge["target"].index, "input")
        edge["wire"].update_wire(start, end, color, is_exec, edge.get("bends", []), 2, highlighted)
        # 线本身或相连节点被选中时都置顶，避免被其它步骤盖住
        edge["wire"].setZValue(5 if highlighted else -1)
        self._sync_bend_handles(edge)

    def _on_wire_clicked(self, wire):
        self.selected_edge = None
        for edge in self._edges:
            if edge["wire"] is wire:
                self.selected_edge = edge
                break
        self._update_edges()

    def _sync_bend_handles(self, edge):
        bends = edge.get("bends", [])
        handles = edge.setdefault("bend_handles", [])
        for index, handle in enumerate(handles):
            if index < len(bends):
                handle.setPos(bends[index][0], bends[index][1])
            else:
                handle.setVisible(False)
        for index in range(len(handles), len(bends)):
            handle = self._create_bend_handle(edge, index)
            handles.append(handle)
            handle.setPos(bends[index][0], bends[index][1])

    def _create_bend_handle(self, edge, index):
        handle = BendHandleItem(lambda h, e=edge, i=index: self._on_bend_moved(e, i, h))
        self.scene.addItem(handle)
        return handle

    def _on_bend_moved(self, edge, index, handle):
        if index < len(edge.get("bends", [])):
            edge["bends"][index] = (handle.x(), handle.y())
        self._persist_bends(edge)
        self._apply_wire_geometry(edge)

    def _persist_bends(self, edge):
        source_index = edge["source"].index
        task = self.tasks[source_index]
        bends = edge.get("bends", [])
        bend_map = task.setdefault("blueprint_bends", {})
        if bends:
            bend_map[edge["edge_kind"]] = bends
        else:
            bend_map.pop(edge["edge_kind"], None)

    def _wire_color(self, output, is_default):
        if is_default:
            return "#64748b"
        if output == "output":
            return "#94a3b8"
        return BlueprintNodeItem.PORT_COLORS.get(output, "#94a3b8")

    def _on_empty_group_moved(self, box, dx, dy):
        group_positions = self.layout_data.setdefault("group_positions", {})
        pos = group_positions.get(str(box.group_id))
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            x, y = float(pos[0]) + dx, float(pos[1]) + dy
        else:
            x, y = box.rect().left() + dx, box.rect().top() + dy
        group_positions[str(box.group_id)] = (x, y)

    def _sync_group_bounds(self):
        for group_id, box in self._group_items.items():
            indices = self._group_all_node_indices(group_id)
            nodes = [self._node_items[i] for i in indices if i in self._node_items]
            if not nodes:
                continue
            left = min(node.x() for node in nodes) - 18
            top = min(node.y() for node in nodes) - 36
            if str(group_id) in self.collapsed_groups:
                box.setRect(left, top, 220, 18)
                continue
            right = max(node.x() + node.rect().width() for node in nodes) + 18
            bottom = max(node.y() + node.rect().height() for node in nodes) + 18
            box.setRect(left, top, right - left, bottom - top)

    def auto_arrange(self):
        self._push_history()
        positions = self.layout_data.setdefault("positions", {})
        for index in range(len(self.tasks)):
            positions[str(index)] = (40 + (index % 3) * 280, 40 + (index // 3) * 130)
        self.refresh()

    def save_layout(self):
        self.sync_positions()
        self.save_callback(self.tasks, self.layout_data, self.group_metadata)

    def sync_positions(self):
        positions = self.layout_data.setdefault("positions", {})
        for item in self.scene.items():
            if isinstance(item, BlueprintNodeItem):
                if self.grid_snap:
                    item.setPos(round(item.x() / 20) * 20, round(item.y() / 20) * 20)
                positions[str(item.index)] = (item.x(), item.y())
        self.layout_data["zoom"] = self.view.transform().m11()

    def set_grid_snap(self, enabled):
        self.grid_snap = enabled
        self.sync_positions()
        self.refresh()

    def toggle_group(self, group_id):
        self._push_history()
        if group_id in self.collapsed_groups:
            self.collapsed_groups.remove(group_id)
        else:
            self.collapsed_groups.add(group_id)
        self.refresh()

    def align_selected(self):
        selected = [item for item in self.scene.selectedItems() if isinstance(item, BlueprintNodeItem)]
        if len(selected) < 2:
            return
        self._push_history()
        y = min(item.y() for item in selected)
        for item in selected:
            item.setY(y)
        self.sync_positions()

    def copy_selected(self):
        selected = [item for item in self.scene.selectedItems() if isinstance(item, BlueprintNodeItem)]
        self.clipboard = [deepcopy(self.tasks[item.index]) for item in selected]

    def paste_tasks(self):
        if not self.clipboard:
            return
        self._push_history()
        start_index = len(self.tasks)
        for offset, task in enumerate(self.clipboard):
            copied = deepcopy(task)
            copied["id"] = str(uuid.uuid4())
            copied["description"] = f"{copied.get('description', '步骤')} 副本"
            copied.pop("flow_next", None)
            copied.pop("flow_next_disabled", None)
            self.tasks.append(copied)
            self.layout_data.setdefault("positions", {})[str(start_index + offset)] = (80 + offset * 260, 80)
        self.refresh()

    def delete_selected(self):
        selected = sorted({item.index for item in self.scene.selectedItems() if isinstance(item, BlueprintNodeItem)}, reverse=True)
        if not selected:
            return
        self._push_history()
        removed = set(selected)
        for index in selected:
            self.tasks.pop(index)
        for task in self.tasks:
            flow_target = task.get("flow_next")
            if flow_target is not None and not any(str(candidate.get("id")) == str(flow_target) for candidate in self.tasks):
                task.pop("flow_next", None)
            for field in ("condition_true_jump_to", "condition_false_jump_to", "detour_success_jump_to", "detour_jump_to", "timeout_jump_to", "event_timeout_target", "loop_target", "loop_exit_target", "event_trigger_target", "switch_default_jump_to"):
                value = task.get(field)
                try:
                    target_index = int(value) - 1
                except (TypeError, ValueError):
                    continue
                if target_index in removed:
                    task.pop(field, None)
                else:
                    shift = sum(1 for removed_index in removed if removed_index < target_index)
                    task[field] = target_index - shift + 1
            cases = task.get("switch_cases")
            if isinstance(cases, dict):
                for case, value in list(cases.items()):
                    try:
                        target_index = int(value) - 1
                    except (TypeError, ValueError):
                        continue
                    if target_index in removed:
                        cases.pop(case, None)
                    else:
                        shift = sum(1 for removed_index in removed if removed_index < target_index)
                        cases[case] = target_index - shift + 1
        positions = self.layout_data.setdefault("positions", {})
        self.layout_data["positions"] = {
            str(new_index): positions.get(str(old_index), (40, 40))
            for new_index, old_index in enumerate(index for index in range(len(self.tasks) + len(removed)) if index not in removed)
        }
        self.refresh()

    def show_context_menu(self, position):
        scene_position = self.view.mapToScene(position)
        clicked_items = self.scene.items(scene_position)
        clicked_item = clicked_items[0] if clicked_items else None

        # 转折点手柄
        if isinstance(clicked_item, BendHandleItem):
            edge = self._edge_for_handle(clicked_item)
            if edge is not None:
                bend_index = edge["bend_handles"].index(clicked_item)
                menu = QMenu(self)
                delete_bend_action = menu.addAction("删除此转折点")
                delete_bend_action.triggered.connect(lambda: self._delete_bend(edge, bend_index))
                menu.exec(self.view.mapToGlobal(position))
            return

        # 分组
        if isinstance(clicked_item, BlueprintGroupItem):
            self.show_group_context_menu(clicked_item, position)
            return

        # 连线
        edge = self._edge_for_wire(clicked_item)
        node = self._node_item_from(clicked_item)
        if edge is not None and node is None:
            menu = QMenu(self)
            add_bend_action = menu.addAction("在线上添加转折点")
            delete_conn_action = menu.addAction("删除连接")
            add_bend_action.triggered.connect(lambda: self._add_bend(edge, scene_position))
            delete_conn_action.triggered.connect(lambda: self._delete_connection(edge))
            menu.exec(self.view.mapToGlobal(position))
            return

        if node is not None:
            selected_nodes = [i for i in self.scene.selectedItems() if isinstance(i, BlueprintNodeItem)]
            if node not in selected_nodes:
                self._refreshing = True
                node.setSelected(True)
                self._refreshing = False
            self._select_node(node.index)

        menu = QMenu(self)
        add_task_action = menu.addAction("新建步骤")
        add_task_action.triggered.connect(lambda: self.add_task(None, (scene_position.x(), scene_position.y())))
        add_group_action = menu.addAction("新增组")
        add_group_action.triggered.connect(lambda: self.add_group(None, (scene_position.x(), scene_position.y())))
        menu.addSeparator()
        if node is not None:
            selected_count = len([i for i in self.scene.selectedItems() if isinstance(i, BlueprintNodeItem)])
            if selected_count > 1:
                menu.addAction(f"删除选中的 {selected_count} 个步骤", self.delete_selected)
                menu.addAction(f"复制选中的 {selected_count} 个步骤", self.copy_selected)
            else:
                menu.addAction("删除当前步骤", lambda: self._delete_single(node.index))
                menu.addAction("复制当前步骤", lambda: self._copy_single(node.index))
                menu.addAction("更改当前步骤类型", lambda: self.change_type(node.index))
                menu.addAction("编辑步骤注释", lambda: self.edit_comment(node.index))
                menu.addAction("重命名步骤", lambda: self.rename_node(node.index))
                menu.addAction("更改步骤颜色", lambda: self.color_node(node.index))
                menu.addAction("折叠/展开步骤", lambda: self.toggle_collapse(node.index))
            self._add_group_menu(menu)
        paste_action = menu.addAction("粘贴", self.paste_tasks)
        paste_action.setEnabled(bool(self.clipboard))
        menu.exec(self.view.mapToGlobal(position))

    def show_group_context_menu(self, group_item, position):
        group_id = group_item.group_id
        scene_position = self.view.mapToScene(position)
        menu = QMenu(self)
        add_group_action = menu.addAction("新增组")
        toggle_action = menu.addAction("收起组" if group_id not in self.collapsed_groups else "展开组")
        edit_action = menu.addAction("编辑组设置")
        add_task_action = menu.addAction("新建步骤到此组")
        delete_action = menu.addAction("删除组")
        add_group_action.triggered.connect(lambda: self.add_group(group_id, (scene_position.x(), scene_position.y())))
        toggle_action.triggered.connect(lambda: self.toggle_group(group_id))
        edit_action.triggered.connect(lambda: self._edit_group(group_id))
        add_task_action.triggered.connect(lambda: self.add_task(group_id, (scene_position.x(), scene_position.y())))
        delete_action.triggered.connect(lambda: self._delete_group(group_id))
        menu.exec(self.view.mapToGlobal(position))

    def connect_nodes(self, source_index, target_index, output):
        if not 0 <= source_index < len(self.tasks) or not 0 <= target_index < len(self.tasks):
            return
        self._push_history()
        field_by_output = {
            "output": "flow_next",
            "true": "condition_true_jump_to",
            "false": "condition_false_jump_to",
            "success": "detour_success_jump_to",
            "failure": "detour_jump_to",
            "timeout": "timeout_jump_to",
            "event_timeout": "event_timeout_target",
            "body": "loop_target",
            "exit": "loop_exit_target",
            "triggered": "event_trigger_target",
            "default": "switch_default_jump_to",
        }
        source_task = self.tasks[source_index]
        field = field_by_output.get(output)
        if field == "flow_next":
            source_task[field] = self.tasks[target_index].get("id")
            source_task.pop("flow_next_disabled", None)
        elif field:
            source_task[field] = target_index + 1
            if output in {"success", "failure"}:
                source_task["detour_enabled"] = True
        elif source_task.get("type") == "switch":
            cases = source_task.setdefault("switch_cases", {})
            cases[str(output)] = target_index + 1
        self.refresh()

    # ---------- 编辑器与选择 ----------

    def _on_selection_changed(self):
        if self._refreshing:
            return
        nodes = [i for i in self.scene.selectedItems() if isinstance(i, BlueprintNodeItem)]
        groups = [i for i in self.scene.selectedItems() if isinstance(i, BlueprintGroupItem)]
        if len(nodes) == 1:
            self.selected_edge = None
            self._select_node(nodes[0].index)
        elif len(nodes) == 0 and len(groups) == 1:
            self.selected_edge = None
            self._load_editor_for_group(groups[0].group_id)
        elif len(nodes) == 0:
            self.selected_edge = None
            self._clear_editor()
        self._update_edges()

    def _refresh_editor_from_selection(self):
        nodes = [i for i in self.scene.items() if isinstance(i, BlueprintNodeItem) and i.isSelected()]
        groups = [i for i in self.scene.items() if isinstance(i, BlueprintGroupItem) and i.isSelected()]
        if nodes:
            self._select_node(nodes[0].index)
        elif groups:
            self._load_editor_for_group(groups[0].group_id)
        else:
            self.current_index = -1
            self.current_group_id = None
            self._clear_editor()

    def _select_node(self, index):
        self.current_index = index
        self.current_group_id = None
        self.group_form.setVisible(False)
        if 0 <= index < len(self.tasks):
            self._load_editor_for_task(self.tasks[index])
        else:
            self._clear_editor()

    def _load_editor_for_group(self, group_id):
        self.current_group_id = str(group_id)
        self.current_index = -1
        self.selected_label.setText("组设置")
        self.editor_actions.setVisible(False)
        self.template_preview.setVisible(False)
        self.recognition_group.setVisible(False)
        self._clear_special_form()
        self.click_until_group.setVisible(False)
        self.group_form.setVisible(True)
        self.group_name_edit.setText(self._group_names().get(str(group_id), str(group_id)))
        self._pending_group_color = None
        self._update_group_color_button(str(group_id))

    def _update_group_color_button(self, group_id):
        color = self._pending_group_color or self._group_colors().get(str(group_id), "#38bdf8")
        self.group_color_button.setText(color)
        self.group_color_button.setStyleSheet(f"background: {color}; color: #ffffff;")

    def _pick_group_color(self):
        if self.current_group_id is None:
            return
        current = QColor(self._pending_group_color or self._group_colors().get(self.current_group_id, "#38bdf8"))
        color = QColorDialog.getColor(current, self, "选择组颜色")
        if color.isValid():
            self._pending_group_color = color.name()
            self._update_group_color_button(self.current_group_id)

    def _apply_group_editor(self):
        if self.current_group_id is None:
            return
        self._push_history()
        group_id = self.current_group_id
        name = self.group_name_edit.text().strip() or group_id
        self.group_metadata.setdefault("names", {})[group_id] = name
        if self._pending_group_color is not None:
            self.group_metadata.setdefault("colors", {})[group_id] = self._pending_group_color
        for task in self.tasks:
            if str(task.get("group_id") or "group_default") == group_id:
                task["group_name"] = name
                if self._pending_group_color is not None:
                    task["group_color"] = self._pending_group_color
        self.refresh()
        self.save_layout()
        self._load_editor_for_group(group_id)

    def _load_editor_for_task(self, task):
        self.selected_label.setText(f"第 {self.current_index + 1} 步 · 类型: {task.get('type', 'normal')}")
        is_click_until = task.get("type") == "click_until_gone"
        is_recognition = task.get("type", "normal") in ("normal", "advanced")
        self.recognition_group.setVisible(is_recognition)
        self.click_until_group.setVisible(is_click_until)
        self.editor_actions.setVisible(is_recognition or is_click_until)
        templates = task.get("templates") or task.get("template", "")
        if isinstance(templates, (list, tuple)):
            templates = ", ".join(str(item) for item in templates)
        self.template_edit.setText(str(templates))
        self._update_template_preview(templates)
        self.description_edit.setText(str(task.get("description", "")))
        self.enabled_checkbox.setChecked(bool(task.get("enabled", True)))
        self.threshold_edit.setText(str(task.get("threshold", config.THRESHOLD)))
        self.timeout_edit.setText(str(task.get("timeout", 5)))
        self.after_wait_edit.setText(str(task.get("after_wait", 0.25)))
        self.match_required_checkbox.setChecked(bool(task.get("click_requires_match", True)))
        offset = task.get("offset", (0, 0))
        self.offset_x_edit.setText(str(task.get("offset_x", offset[0] if isinstance(offset, (list, tuple)) else 0)))
        self.offset_y_edit.setText(str(task.get("offset_y", offset[1] if isinstance(offset, (list, tuple)) else 0)))
        click_position = task.get("click_position")
        self.click_x_edit.setText(str(task.get("click_x", click_position[0] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
        self.click_y_edit.setText(str(task.get("click_y", click_position[1] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
        rects = task.get("match_rects")
        rect = (rects[0] if isinstance(rects, list) and rects else None) or task.get("match_rect") or task.get("search_rect")
        self.match_rect_edit.setText(", ".join(str(v) for v in rect[:4]) if isinstance(rect, (list, tuple)) and len(rect) >= 4 else "")
        self.next_template_edit.setText(str(task.get("next_template") or ""))
        wait_for = task.get("wait_for", "time")
        if wait_for == "next_appear":
            self.wait_for_combo.setCurrentIndex(1)
        elif wait_for == "change_then_appear":
            self.wait_for_combo.setCurrentIndex(2)
        else:
            self.wait_for_combo.setCurrentIndex(0)
        self.click_checkbox.setChecked(bool(task.get("click", True)))
        self.optional_checkbox.setChecked(bool(task.get("optional", not bool(task.get("required", True)))))

        click_until_templates = task.get("templates") or task.get("template", "")
        if isinstance(click_until_templates, (list, tuple)):
            click_until_templates = ", ".join(str(item) for item in click_until_templates)
        self.click_until_template_edit.setText(str(click_until_templates))
        self.click_until_interval_edit.setText(str(task.get("click_interval", 0.5)))
        self.click_until_stop_delay_edit.setText(str(task.get("stop_delay", 0.0)))
        self.click_until_timeout_edit.setText(str(task.get("timeout", 30)))
        self.click_until_continue_checkbox.setChecked(bool(task.get("continue_after_timeout", False)))
        self.click_until_stop_on_change_checkbox.setChecked(bool(task.get("stop_on_change", False)))

        self._rebuild_special_form(task)

    @staticmethod
    def _special_field_specs(task):
        task_type = task.get("type", "normal")
        if task_type == "keyboard_move":
            return [
                ("move_steps", "移动步骤(每行: 按键 时长秒)", "move_steps"),
                ("delay_before", "执行前延时(秒)", "float"),
                ("after_wait", "执行后等待(秒)", "float"),
            ]
        if task_type == "key_press":
            return [
                ("key", "按键", "text"),
                ("delay_before", "执行前延时(秒)", "float"),
                ("hold_time", "按住时长(秒)", "float"),
                ("after_wait", "执行后等待(秒)", "float"),
            ]
        if task_type == "drag":
            return [
                ("start_x", "起点 X", "float"),
                ("start_y", "起点 Y", "float"),
                ("end_x", "终点 X", "float"),
                ("end_y", "终点 Y", "float"),
                ("duration", "拖曳时长(秒)", "float"),
                ("after_wait", "执行后等待(秒)", "float"),
            ]
        if task_type == "click_until_gone":
            return []
        if task_type == "delay":
            return [("duration", "延迟时间(秒)", "float")]
        if task_type == "condition":
            return [
                ("condition_templates", "条件模板(逗号分隔)", "templates"),
                ("condition_operator", "条件运算(all/any/not)", "text"),
                ("condition_true_jump_to", "成立跳转步骤号", "int"),
                ("condition_false_jump_to", "不成立跳转步骤号", "int"),
                ("condition_invert", "反转条件结果", "bool"),
                ("threshold", "匹配阈值(0-1)", "float"),
            ]
        if task_type == "switch":
            return [
                ("switch_value", "选择值", "text"),
                ("switch_cases", "分支(值:步骤号,逗号分隔)", "cases"),
                ("switch_default_jump_to", "默认步骤号", "int"),
            ]
        if task_type == "loop":
            return [
                ("loop_count", "循环次数", "int"),
                ("loop_target", "循环体步骤号", "int"),
                ("loop_exit_target", "退出步骤号", "int"),
            ]
        if task_type == "event":
            return [
                ("event_template", "事件模板", "text"),
                ("event_timeout", "等待超时(秒)", "float"),
                ("event_timeout_target", "超时跳转步骤号", "int"),
                ("threshold", "匹配阈值(0-1)", "float"),
            ]
        return []

    def _clear_special_form(self):
        while self.special_form.rowCount():
            self.special_form.removeRow(0)
        self.special_edits = {}

    def _rebuild_special_form(self, task):
        self._clear_special_form()
        for key, label, kind in self._special_field_specs(task):
            if key == "condition_templates":
                value = task.get("condition_templates")
                if isinstance(value, (list, tuple)):
                    value = ", ".join(str(item) for item in value)
                else:
                    value = str(value or task.get("condition_template") or "")
                editor = QLineEdit(value)
            elif key == "switch_cases":
                value = ", ".join(f"{k}:{v}" for k, v in (task.get("switch_cases") or {}).items())
                editor = QLineEdit(value)
            elif key == "move_steps":
                steps = task.get("move_steps") or []
                value = "\n".join(
                    f"{step.get('key', 'W')} {step.get('duration', 1.0)}"
                    for step in steps
                    if isinstance(step, dict)
                )
                editor = QPlainTextEdit()
                editor.setPlaceholderText("每行一个：按键 时长(秒)，例如 W 1.2")
                editor.setMaximumHeight(120)
                editor.setPlainText(value)
            elif kind == "bool":
                editor = QCheckBox()
                editor.setChecked(bool(task.get(key, False)))
            else:
                editor = QLineEdit(str(task.get(key, "")))
            self.special_edits[key] = editor
            self.special_form.addRow(label + ":", editor)

    def _apply_special_fields(self, task):
        specs = {spec[0]: spec[2] for spec in self._special_field_specs(task)}
        for key, editor in self.special_edits.items():
            kind = specs.get(key, "text")
            if kind == "move_steps":
                steps = []
                for line in editor.toPlainText().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    try:
                        duration = float(parts[1].rstrip("sS")) if len(parts) > 1 else 1.0
                    except ValueError:
                        duration = 1.0
                    steps.append({"key": parts[0], "duration": duration})
                task["move_steps"] = steps
                continue
            if kind == "bool":
                task[key] = editor.isChecked()
                continue
            text = editor.text().strip()
            if kind == "templates":
                values = [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]
                task["condition_templates"] = values
                task["condition_template"] = values[0] if values else ""
                if values:
                    task["template"] = values[0]
                continue
            if kind == "cases":
                cases = {}
                for item in text.replace("，", ",").split(","):
                    if ":" not in item:
                        continue
                    case_value, target = item.split(":", 1)
                    if case_value.strip() and target.strip():
                        try:
                            cases[case_value.strip()] = int(target.strip())
                        except ValueError:
                            continue
                task["switch_cases"] = cases
                continue
            if kind == "float":
                if text == "":
                    task.pop(key, None)
                else:
                    task[key] = self._float(text, 0.0)
                continue
            if kind == "int":
                if text == "":
                    task.pop(key, None)
                else:
                    value = self._int(text)
                    if value is not None:
                        task[key] = value
                continue
            if text == "":
                task.pop(key, None)
            else:
                task[key] = text
        if task.get("type") == "event" and task.get("event_template"):
            task["template"] = task["event_template"]
        if task.get("type") == "key_press" and task.get("key"):
            task["template"] = task["key"]

    def _clear_editor(self):
        self.selected_label.setText("未选择步骤")
        for edit in (self.template_edit, self.description_edit, self.threshold_edit, self.timeout_edit, self.after_wait_edit, self.offset_x_edit, self.offset_y_edit, self.click_x_edit, self.click_y_edit, self.match_rect_edit, self.next_template_edit):
            edit.clear()
        self.click_checkbox.setChecked(False)
        self.match_required_checkbox.setChecked(False)
        self.optional_checkbox.setChecked(False)
        self.enabled_checkbox.setChecked(False)
        self.click_until_template_edit.clear()
        self.click_until_interval_edit.clear()
        self.click_until_stop_delay_edit.clear()
        self.click_until_timeout_edit.clear()
        self.click_until_continue_checkbox.setChecked(False)
        self.click_until_stop_on_change_checkbox.setChecked(False)
        self.click_until_group.setVisible(False)
        self.wait_for_combo.setCurrentIndex(0)
        self.template_preview.setPixmap(QPixmap())
        self.template_preview.setText("")
        self.template_preview.setVisible(False)
        self._clear_special_form()
        self.click_until_group.setVisible(False)
        self.recognition_group.setVisible(False)
        self.editor_actions.setVisible(False)
        self.group_form.setVisible(False)
        self.current_group_id = None

    def _update_template_preview(self, templates):
        self.template_preview.setPixmap(QPixmap())
        self.template_preview.setText("")
        self.template_preview.setVisible(False)

    def _capture_rect(self):
        if config.USE_WINDOW_MODE and config.TARGET_WINDOW_TITLE:
            return get_window_rect()
        screen = QApplication.primaryScreen().geometry()
        return {"left": screen.left(), "top": screen.top(), "width": screen.width(), "height": screen.height()}

    def _show_capture_overlay(self, mode):
        if self.capture_overlay is not None:
            self.capture_overlay.close()
        self.capture_overlay = CaptureOverlay(self._capture_rect(), mode)
        self.capture_overlay.clicked.connect(self.finish_click_capture)
        self.capture_overlay.region_selected.connect(self.finish_region_capture)
        self.capture_overlay.image_selected.connect(self.finish_image_capture)
        self.capture_overlay.cancelled.connect(self.cancel_capture)
        self.capture_overlay.too_small.connect(self.finish_too_small)
        self.capture_overlay.destroyed.connect(self.clear_capture_overlay)
        self.capture_overlay.start()
        self.hide()
        parent = self.parent()
        if parent is not None:
            parent.hide()

    def start_click_capture(self):
        if self.current_index >= 0:
            self._capture_callback = None
            self._show_capture_overlay("click")

    def start_region_capture(self):
        if self.current_index >= 0:
            self._capture_callback = None
            self._capture_target = "match"
            self._show_capture_overlay("region")

    def _begin_dialog_capture(self, mode, callback):
        self._capture_callback = callback
        self._capture_target = "match"
        self._show_capture_overlay(mode)

    def finish_click_capture(self, x, y):
        self.clear_capture_overlay()
        self.activateWindow()
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        if callback is not None:
            callback(("click", x, y))
            return
        self.click_x_edit.setText(str(x))
        self.click_y_edit.setText(str(y))
        if not 0 <= self.current_index < len(self.tasks):
            return
        task = self.tasks[self.current_index]
        task["click_x"] = int(x)
        task["click_y"] = int(y)
        task["click_position"] = (int(x), int(y))
        if task.get("type") != "click_until_gone":
            task.pop("match_rect", None)
            task.pop("search_rect", None)
        self._push_history()
        self.refresh()
        self._select_node(self.current_index)
        self.save_layout()

    def finish_region_capture(self, left, top, right, bottom):
        self.clear_capture_overlay()
        self.activateWindow()
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        if callback is not None:
            callback(("region", (left, top, right, bottom)))
            return
        if not 0 <= self.current_index < len(self.tasks):
            return
        task = self.tasks[self.current_index]
        if getattr(self, "_capture_target", "match") == "next":
            task["next_match_rect"] = (left, top, right, bottom)
            task["next_search_rect"] = (left, top, right, bottom)
            self._push_history()
            self.refresh()
            self._select_node(self.current_index)
            self.save_layout()
            return
        match_rects = task.setdefault("match_rects", [])
        if not isinstance(match_rects, list):
            match_rects = []
            task["match_rects"] = match_rects
        match_rects.append((left, top, right, bottom))
        task["match_rect"] = match_rects[0]
        task["search_rect"] = match_rects[0]
        self.match_rect_edit.setText(f"{left}, {top}, {right}, {bottom}")
        self._push_history()
        self.refresh()
        self._select_node(self.current_index)
        self.save_layout()

    def finish_image_capture(self, left, top, right, bottom):
        self.clear_capture_overlay()
        self.activateWindow()
        if right <= left or bottom <= top:
            QMessageBox.warning(self, "框选失败", "框选区域太小，请重新拖曳选择。")
            return
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        QTimer.singleShot(120, lambda: self._save_captured_image(left, top, right, bottom, callback))

    def _save_captured_image(self, left, top, right, bottom, callback=None):
        icons_dir = os.path.join(os.path.dirname(__file__), "icons")
        os.makedirs(icons_dir, exist_ok=True)
        image_name = f"captured_{uuid.uuid4().hex[:10]}"
        image_path = os.path.join(icons_dir, f"{image_name}.png")
        screen = QApplication.primaryScreen()
        pixmap = screen.grabWindow(0, left, top, right - left, bottom - top)
        if pixmap.isNull() or not pixmap.save(image_path, "PNG"):
            QMessageBox.warning(self, "保存失败", "无法保存框选图片。")
            return
        reload_templates()
        if callback is not None:
            callback(("image", image_name))
            return
        if not 0 <= self.current_index < len(self.tasks):
            return
        task = self.tasks[self.current_index]
        task["next_template"] = image_name
        task["next_templates"] = [image_name]
        task["wait_for"] = "next_appear"
        self.next_template_edit.setText(image_name)
        self.wait_for_combo.setCurrentIndex(1)
        self._push_history()
        self.refresh()
        self._select_node(self.current_index)
        self.save_layout()

    def cancel_capture(self):
        self.clear_capture_overlay()
        self.activateWindow()

    def finish_too_small(self):
        self.clear_capture_overlay()
        self.activateWindow()
        QMessageBox.warning(self, "框选失败", "框选区域太小，请重新拖曳选择。")

    def clear_capture_overlay(self):
        if self.capture_overlay is not None:
            overlay = self.capture_overlay
            self.capture_overlay = None
            overlay.close()
        self.show()
        parent = self.parent()
        if parent is not None:
            parent.show()

    def clear_match_region(self):
        self.match_rect_edit.clear()
        if not 0 <= self.current_index < len(self.tasks):
            return
        task = self.tasks[self.current_index]
        for key in ("match_rects", "match_rect", "search_rect"):
            task.pop(key, None)
        self._push_history()
        self.refresh()
        self._select_node(self.current_index)
        self.save_layout()

    def select_next_template_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择等待出现的目标图片", config.ICON_DIR, "PNG 图片 (*.png)")
        if path:
            self.next_template_edit.setText(os.path.splitext(os.path.basename(path))[0])
            self.wait_for_combo.setCurrentIndex(1)

    def start_next_template_capture(self):
        if self.current_index >= 0:
            self._capture_callback = None
            self._capture_target = "next_template"
            self._show_capture_overlay("image")

    def start_next_region_capture(self):
        if self.current_index >= 0:
            if not self.next_template_edit.text().strip():
                QMessageBox.information(self, "提示", "请先选择下一模板图片，再框选它的出现位置。")
                return
            self._capture_callback = None
            self._capture_target = "next"
            self._show_capture_overlay("region")

    def select_template_file(self):
        if not 0 <= self.current_index < len(self.tasks):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("绑定图片")
        dialog.resize(420, 440)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("绑定图片操作"))

        image_list = QListWidget()
        layout.addWidget(image_list, 1)

        current = self.template_edit.text()
        if self.tasks[self.current_index].get("type") == "click_until_gone":
            current = self.click_until_template_edit.text()
        for name in [item.strip() for item in current.replace("，", ",").split(",") if item.strip()]:
            image_list.addItem(name)

        def choose_images():
            paths, _ = QFileDialog.getOpenFileNames(dialog, "选择要绑定的图片", config.ICON_DIR, "PNG 图片 (*.png)")
            if not paths:
                return
            existing = [image_list.item(i).text() for i in range(image_list.count())]
            for path in paths:
                name = os.path.splitext(os.path.basename(path))[0]
                if name not in existing:
                    image_list.addItem(name)
                    existing.append(name)

        def capture_image():
            dialog.hide()

            def on_captured(result):
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                if result[0] == "image":
                    name = result[1]
                    existing = [image_list.item(i).text() for i in range(image_list.count())]
                    if name not in existing:
                        image_list.addItem(name)

            self._begin_dialog_capture("image", on_captured)

        def preview_image():
            row = image_list.currentRow()
            if row < 0:
                return
            name = image_list.item(row).text()
            matches = glob.glob(os.path.join(config.ICON_DIR, f"{name}.*"))
            if matches:
                os.startfile(matches[0])
            else:
                QMessageBox.information(dialog, "预览绑定图片", "当前步骤没有找到可预览的绑定图片。")

        def remove_image():
            row = image_list.currentRow()
            if row >= 0:
                image_list.takeItem(row)

        buttons_row = QHBoxLayout()
        choose_btn = QPushButton("选择图片")
        choose_btn.clicked.connect(choose_images)
        capture_btn = QPushButton("手动框选图片")
        capture_btn.clicked.connect(capture_image)
        preview_btn = QPushButton("预览绑定图片")
        preview_btn.clicked.connect(preview_image)
        remove_btn = QPushButton("删除选中图片")
        remove_btn.clicked.connect(remove_image)
        buttons_row.addWidget(choose_btn)
        buttons_row.addWidget(capture_btn)
        buttons_row.addWidget(preview_btn)
        buttons_row.addWidget(remove_btn)
        layout.addLayout(buttons_row)

        def save():
            values = [image_list.item(i).text() for i in range(image_list.count())]
            template_value = ", ".join(values)
            if self.tasks[self.current_index].get("type") == "click_until_gone":
                self.click_until_template_edit.setText(template_value)
            else:
                self.template_edit.setText(template_value)
                self._update_template_preview(template_value)
            dialog.accept()

        save_btn = QPushButton("保存")
        save_btn.clicked.connect(save)
        layout.addWidget(save_btn)
        dialog.exec()

    def open_detour_editor(self):
        if not 0 <= self.current_index < len(self.tasks):
            return
        task = self.tasks[self.current_index]
        if task.get("type", "normal") not in ("normal", "advanced"):
            return

        detour_steps = task.setdefault("detour_steps", [])
        if not isinstance(detour_steps, list):
            detour_steps = []
            task["detour_steps"] = detour_steps

        dialog = QDialog(self)
        dialog.setWindowTitle("迂回设置")
        dialog.resize(560, 500)
        layout = QVBoxLayout(dialog)

        enabled_checkbox = QCheckBox("启用迂回")
        enabled_checkbox.setChecked(bool(task.get("detour_enabled", False)))
        layout.addWidget(enabled_checkbox)

        jump_options = ["不跳转"]
        jump_option_numbers = {}
        for task_index, main_task in enumerate(self.tasks):
            description = main_task.get("description") or main_task.get("template") or main_task.get("type", "步骤")
            option = f"{task_index + 1}. {description}"
            jump_options.append(option)
            jump_option_numbers[option] = task_index + 1

        def jump_label(target_number):
            if target_number is None:
                return "不跳转"
            for option, option_number in jump_option_numbers.items():
                try:
                    if option_number == int(target_number):
                        return option
                except (TypeError, ValueError):
                    continue
            return "不跳转"

        jump_combo = QComboBox()
        jump_combo.addItems(jump_options)
        jump_combo.setCurrentText(jump_label(task.get("detour_jump_to")))
        success_jump_combo = QComboBox()
        success_jump_combo.addItems(jump_options)
        success_jump_combo.setCurrentText(jump_label(task.get("detour_success_jump_to")))

        jump_row = QHBoxLayout()
        jump_row.addWidget(QLabel("未识别时跳到:"))
        jump_row.addWidget(jump_combo, 1)
        layout.addLayout(jump_row)
        success_row = QHBoxLayout()
        success_row.addWidget(QLabel("识别成功后跳到:"))
        success_row.addWidget(success_jump_combo, 1)
        layout.addLayout(success_row)

        step_list = QListWidget()
        layout.addWidget(step_list, 1)

        def refresh_list():
            step_list.clear()
            for step in detour_steps:
                desc = step.get("description") or step.get("template") or step.get("type", "步骤")
                step_list.addItem(f"{step.get('type', 'normal')} - {desc}")

        refresh_list()

        add_row = QHBoxLayout()
        type_combo = QComboBox()
        type_combo.addItems(["normal", "advanced", "loop", "key_press", "keyboard_move", "drag", "click_until_gone", "delay"])
        add_row.addWidget(type_combo, 1)
        add_button = QPushButton("新增步骤")
        add_button.clicked.connect(lambda: (detour_steps.append({"type": type_combo.currentText() or "normal"}), refresh_list()))
        add_row.addWidget(add_button)
        layout.addLayout(add_row)

        action_row = QHBoxLayout()
        config_button = QPushButton("设置")

        def configure_step():
            row = step_list.currentRow()
            if 0 <= row < len(detour_steps):
                self._configure_detour_step(detour_steps[row], dialog)
                refresh_list()

        config_button.clicked.connect(configure_step)
        action_row.addWidget(config_button)
        delete_button = QPushButton("删除")

        def delete_step():
            row = step_list.currentRow()
            if 0 <= row < len(detour_steps):
                detour_steps.pop(row)
                refresh_list()

        delete_button.clicked.connect(delete_step)
        action_row.addWidget(delete_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        def save():
            task["detour_enabled"] = enabled_checkbox.isChecked()
            task["detour_steps"] = detour_steps
            task["detour_jump_to"] = jump_option_numbers.get(jump_combo.currentText())
            task["detour_success_jump_to"] = jump_option_numbers.get(success_jump_combo.currentText())
            self._push_history()
            self.refresh()
            self._select_node(self.current_index)
            self.save_layout()
            dialog.accept()

        save_button = QPushButton("保存迂回设置")
        save_button.clicked.connect(save)
        layout.addWidget(save_button)
        dialog.exec()

    def _configure_detour_step(self, detour_task, parent=None):
        dialog = QDialog(parent or self)
        dialog.setWindowTitle("迂回步骤设置")
        dialog.resize(460, 440)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()

        type_combo = QComboBox()
        type_combo.addItems(["normal", "advanced", "loop", "key_press", "keyboard_move", "drag", "click_until_gone", "delay"])
        type_combo.setCurrentText(str(detour_task.get("type", "normal")))
        form.addRow("类型:", type_combo)

        description_edit = QLineEdit(str(detour_task.get("description", "")))
        form.addRow("描述:", description_edit)

        def hide_dialogs():
            dialog.hide()
            if isinstance(parent, QDialog):
                parent.hide()

        def restore_dialogs():
            if isinstance(parent, QDialog):
                parent.show()
                parent.raise_()
                parent.activateWindow()
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        template_edit = QLineEdit(str(detour_task.get("template", "")))
        template_widget = QWidget()
        template_row = QHBoxLayout(template_widget)
        template_row.setContentsMargins(0, 0, 0, 0)
        template_row.addWidget(template_edit, 1)
        bind_button = QPushButton("绑定图片")

        def bind_image():
            path, _ = QFileDialog.getOpenFileName(dialog, "选择要绑定的图片", config.ICON_DIR, "PNG 图片 (*.png)")
            if path:
                template_edit.setText(os.path.splitext(os.path.basename(path))[0])

        bind_button.clicked.connect(bind_image)
        template_row.addWidget(bind_button)
        capture_template_button = QPushButton("手动框选")

        def capture_template():
            hide_dialogs()

            def on_captured(result):
                restore_dialogs()
                if result[0] == "image":
                    template_edit.setText(result[1])

            self._begin_dialog_capture("image", on_captured)

        capture_template_button.clicked.connect(capture_template)
        template_row.addWidget(capture_template_button)
        form.addRow("模板:", template_widget)

        click_x_edit = QLineEdit(str(detour_task.get("click_x", "")))
        click_y_edit = QLineEdit(str(detour_task.get("click_y", "")))
        click_widget = QWidget()
        click_row = QHBoxLayout(click_widget)
        click_row.setContentsMargins(0, 0, 0, 0)
        click_row.addWidget(QLabel("X"))
        click_row.addWidget(click_x_edit, 1)
        click_row.addWidget(QLabel("Y"))
        click_row.addWidget(click_y_edit, 1)
        click_capture_button = QPushButton("记录点击点")

        def capture_click():
            hide_dialogs()

            def on_captured(result):
                restore_dialogs()
                if result[0] == "click":
                    click_x_edit.setText(str(result[1]))
                    click_y_edit.setText(str(result[2]))

            self._begin_dialog_capture("click", on_captured)

        click_capture_button.clicked.connect(capture_click)
        click_row.addWidget(click_capture_button)
        form.addRow("点击坐标:", click_widget)

        duration_edit = QLineEdit(str(detour_task.get("duration", detour_task.get("hold_time", ""))))
        form.addRow("时长/按住(秒):", duration_edit)

        key_edit = QLineEdit(str(detour_task.get("key", "")))
        form.addRow("按键:", key_edit)

        match_rect_edit = QLineEdit(str(detour_task.get("match_rect", "") or ""))
        match_rect_widget = QWidget()
        match_rect_row = QHBoxLayout(match_rect_widget)
        match_rect_row.setContentsMargins(0, 0, 0, 0)
        match_rect_row.addWidget(match_rect_edit, 1)
        region_capture_button = QPushButton("框选识别区域")

        def capture_region():
            hide_dialogs()

            def on_captured(result):
                restore_dialogs()
                if result[0] == "region":
                    left, top, right, bottom = result[1]
                    match_rect_edit.setText(f"{left}, {top}, {right}, {bottom}")

            self._begin_dialog_capture("region", on_captured)

        region_capture_button.clicked.connect(capture_region)
        match_rect_row.addWidget(region_capture_button)
        form.addRow("识别区域(左上,右下):", match_rect_widget)

        move_steps_edit = QPlainTextEdit()
        move_steps_edit.setMaximumHeight(90)
        move_steps = detour_task.get("move_steps") or []
        move_steps_edit.setPlainText("\n".join(f"{step.get('key', 'W')} {step.get('duration', 1.0)}" for step in move_steps if isinstance(step, dict)))
        form.addRow("移动步骤(每行: 按键 时长):", move_steps_edit)

        layout.addLayout(form)

        def save():
            detour_task["type"] = type_combo.currentText()
            description = description_edit.text().strip()
            if description:
                detour_task["description"] = description
            template = template_edit.text().strip()
            if template:
                detour_task["template"] = template
            else:
                detour_task.pop("template", None)
            click_x = self._int(click_x_edit.text())
            click_y = self._int(click_y_edit.text())
            if click_x is not None and click_y is not None:
                detour_task["click_x"] = click_x
                detour_task["click_y"] = click_y
                detour_task["click_position"] = (click_x, click_y)
            else:
                detour_task.pop("click_x", None)
                detour_task.pop("click_y", None)
                detour_task.pop("click_position", None)
            duration = self._float(duration_edit.text(), 0.0)
            if duration:
                if detour_task.get("type") == "key_press":
                    detour_task["hold_time"] = duration
                else:
                    detour_task["duration"] = duration
            key = key_edit.text().strip()
            if key:
                detour_task["key"] = key
            rect = self._parse_rect_text(match_rect_edit.text())
            if rect is not None:
                detour_task["match_rect"] = rect
                detour_task["search_rect"] = rect
                detour_task["match_rects"] = [rect]
            else:
                detour_task.pop("match_rect", None)
                detour_task.pop("search_rect", None)
                detour_task.pop("match_rects", None)
            steps = []
            for line in move_steps_edit.toPlainText().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                step_key = parts[0]
                try:
                    step_duration = float(parts[1].rstrip("sS")) if len(parts) > 1 else 1.0
                except ValueError:
                    step_duration = 1.0
                steps.append({"key": step_key, "duration": step_duration})
            if steps:
                detour_task["move_steps"] = steps
            dialog.accept()

        save_button = QPushButton("保存")
        save_button.clicked.connect(save)
        layout.addWidget(save_button)
        dialog.exec()

    def _apply_editor(self):
        if not (0 <= self.current_index < len(self.tasks)):
            return
        task = self.tasks[self.current_index]
        task["description"] = self.description_edit.text().strip()
        task["enabled"] = self.enabled_checkbox.isChecked()
        if task.get("type", "normal") in ("normal", "advanced", "click_until_gone"):
            if task.get("type") == "click_until_gone":
                template_value = self.click_until_template_edit.text().strip()
                templates = [item.strip() for item in template_value.replace("，", ",").split(",") if item.strip()]
                task["templates"] = templates
                task["template"] = templates[0] if templates else ""
                task["click_interval"] = self._float(self.click_until_interval_edit.text(), 0.5)
                task["stop_delay"] = self._float(self.click_until_stop_delay_edit.text(), 0.0)
                task["timeout"] = self._float(self.click_until_timeout_edit.text(), 30.0)
                task["continue_after_timeout"] = self.click_until_continue_checkbox.isChecked()
                task["stop_on_change"] = self.click_until_stop_on_change_checkbox.isChecked()
                task["click"] = True
                task["required"] = True
                task["optional"] = False
                task["enabled"] = self.enabled_checkbox.isChecked()
                self._push_history()
                self.refresh()
                self._select_node(self.current_index)
                self.save_layout()
                return
            template_value = self.template_edit.text().strip() or "new_step"
            if task.get("type") in ("advanced", "click_until_gone"):
                templates = [item.strip() for item in template_value.replace("，", ",").split(",") if item.strip()]
                task["templates"] = templates or ["new_step"]
                task["template"] = task["templates"][0]
            else:
                task["template"] = template_value
            task["threshold"] = self._float(self.threshold_edit.text(), task.get("threshold", config.THRESHOLD))
            task["timeout"] = self._float(self.timeout_edit.text(), task.get("timeout", 5))
            task["after_wait"] = self._float(self.after_wait_edit.text(), task.get("after_wait", 0.25))
            task["click"] = self.click_checkbox.isChecked()
            task["click_requires_match"] = self.match_required_checkbox.isChecked()
            task["optional"] = self.optional_checkbox.isChecked()
            task["required"] = not task["optional"]
            task["offset_x"] = self._float(self.offset_x_edit.text(), 0.0)
            task["offset_y"] = self._float(self.offset_y_edit.text(), 0.0)
            task["offset"] = (task["offset_x"], task["offset_y"])
            click_x = self._int(self.click_x_edit.text())
            click_y = self._int(self.click_y_edit.text())
            if click_x is not None and click_y is not None:
                task["click_x"] = click_x
                task["click_y"] = click_y
                task["click_position"] = (click_x, click_y)
            else:
                task.pop("click_x", None)
                task.pop("click_y", None)
                task.pop("click_position", None)
            rect = self._parse_rect_text(self.match_rect_edit.text())
            if rect is not None:
                task["match_rect"] = rect
                task["search_rect"] = rect
                task["match_rects"] = [rect]
            else:
                task.pop("match_rect", None)
                task.pop("search_rect", None)
                task.pop("match_rects", None)
            next_template = self.next_template_edit.text().strip()
            if next_template:
                task["next_template"] = next_template
                task["next_templates"] = [next_template]
            else:
                task.pop("next_template", None)
                task.pop("next_templates", None)
            wait_mode = self.wait_for_combo.currentText()
            if wait_mode.startswith("2"):
                task["wait_for"] = "next_appear"
            elif wait_mode.startswith("3"):
                task["wait_for"] = "change_then_appear"
            else:
                task["wait_for"] = "time"
        self._apply_special_fields(task)
        self._push_history()
        self.refresh()
        self._select_node(self.current_index)
        self.save_layout()

    @staticmethod
    def _float(value, fallback):
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_rect_text(cls, value):
        parts = [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
        if len(parts) != 4:
            return None
        numbers = [cls._int(item) for item in parts]
        return tuple(numbers) if all(item is not None for item in numbers) else None

    # ---------- 命中测试 ----------

    def _node_item_from(self, item):
        if isinstance(item, BlueprintNodeItem):
            return item
        if isinstance(item, QGraphicsTextItem):
            parent = item.parentItem()
            if isinstance(parent, BlueprintNodeItem):
                return parent
        return None

    def _edge_for_wire(self, item):
        for edge in self._edges:
            if edge["wire"] is item:
                return edge
        return None

    def _edge_for_handle(self, handle):
        for edge in self._edges:
            if handle in edge.get("bend_handles", []):
                return edge
        return None

    # ---------- 连线操作 ----------

    def _add_bend(self, edge, scene_position):
        self._push_history()
        edge.setdefault("bends", []).append((scene_position.x(), scene_position.y()))
        self._persist_bends(edge)
        self.refresh()

    def _delete_bend(self, edge, bend_index):
        if 0 <= bend_index < len(edge.get("bends", [])):
            self._push_history()
            edge["bends"].pop(bend_index)
            self._persist_bends(edge)
            self.refresh()

    def _delete_connection(self, edge):
        source_task = self.tasks[edge["source"].index]
        kind = edge["edge_kind"]
        if kind in ("flow", "default"):
            source_task["flow_next"] = None
            source_task["flow_next_disabled"] = True
        elif kind == "detour_success":
            source_task["detour_success_jump_to"] = None
        elif kind == "detour_failure":
            source_task["detour_jump_to"] = None
        elif kind == "condition_true":
            source_task["condition_true_jump_to"] = None
        elif kind == "condition_false":
            source_task["condition_false_jump_to"] = None
        elif kind == "timeout":
            source_task["timeout_jump_to"] = None
        self._push_history()
        self.refresh()
        self.save_layout()

    def _delete_single(self, index):
        if not (0 <= index < len(self.tasks)):
            return
        self._push_history()
        removed = {index}
        self.tasks.pop(index)
        for task in self.tasks:
            flow_target = task.get("flow_next")
            if flow_target is not None and not any(str(c.get("id")) == str(flow_target) for c in self.tasks):
                task.pop("flow_next", None)
            for field in ("condition_true_jump_to", "condition_false_jump_to", "detour_success_jump_to", "detour_jump_to", "timeout_jump_to", "event_timeout_target", "loop_target", "loop_exit_target", "event_trigger_target", "switch_default_jump_to"):
                value = task.get(field)
                try:
                    target_index = int(value) - 1
                except (TypeError, ValueError):
                    continue
                if target_index in removed:
                    task.pop(field, None)
                else:
                    shift = sum(1 for r in removed if r < target_index)
                    task[field] = target_index - shift + 1
            cases = task.get("switch_cases")
            if isinstance(cases, dict):
                for case, value in list(cases.items()):
                    try:
                        target_index = int(value) - 1
                    except (TypeError, ValueError):
                        continue
                    if target_index in removed:
                        cases.pop(case, None)
                    else:
                        shift = sum(1 for r in removed if r < target_index)
                        cases[case] = target_index - shift + 1
        self.refresh()
        self.save_layout()

    def _copy_single(self, index):
        if not (0 <= index < len(self.tasks)):
            return
        copied = deepcopy(self.tasks[index])
        copied["id"] = str(uuid.uuid4())
        copied["description"] = f"{copied.get('description', '步骤')} 副本"
        copied.pop("flow_next", None)
        copied.pop("flow_next_disabled", None)
        self._push_history()
        self.tasks.insert(index + 1, copied)
        self.refresh()
        self.save_layout()

    def change_type(self, index):
        if not (0 <= index < len(self.tasks)):
            return
        types = ["normal", "advanced", "loop", "keyboard_move", "key_press", "drag", "click_until_gone", "delay", "condition", "switch", "event"]
        current = self.tasks[index].get("type", "normal")
        task_type, ok = QInputDialog.getItem(self, "更改步骤类型", "步骤类型:", types, max(0, types.index(current) if current in types else 0), False)
        if not ok:
            return
        task = self.tasks[index]
        self._push_history()
        task["type"] = task_type
        task["click"] = task_type in ("normal", "advanced", "click_until_gone")
        if task_type == "key_press":
            task["key"] = task.get("key") or "E"
            task["template"] = task["key"]
        elif task_type == "drag":
            task["template"] = "drag"
        self.refresh()
        self._select_node(index)
        self.save_layout()

    def edit_comment(self, index):
        if not (0 <= index < len(self.tasks)):
            return
        text, ok = QInputDialog.getText(self, "编辑步骤注释", "步骤注释:", text=str(self.tasks[index].get("blueprint_comment", "")))
        if not ok:
            return
        self._push_history()
        if text.strip():
            self.tasks[index]["blueprint_comment"] = text.strip()
        else:
            self.tasks[index].pop("blueprint_comment", None)
        self.refresh()
        self.save_layout()

    def rename_node(self, index):
        if not (0 <= index < len(self.tasks)):
            return
        text, ok = QInputDialog.getText(self, "重命名步骤", "步骤名称:", text=str(self.tasks[index].get("description", "")))
        if not ok:
            return
        self._push_history()
        self.tasks[index]["description"] = text.strip() or self.tasks[index].get("template", "未命名步骤")
        self.refresh()
        self._select_node(index)
        self.save_layout()

    def color_node(self, index):
        if not (0 <= index < len(self.tasks)):
            return
        color = QColorDialog.getColor(QColor("#2563eb"), self, "选择步骤颜色")
        if not color.isValid():
            return
        self._push_history()
        self.tasks[index]["blueprint_color"] = color.name()
        self.refresh()
        self.save_layout()

    def toggle_collapse(self, index):
        if not (0 <= index < len(self.tasks)):
            return
        self._push_history()
        self.tasks[index]["blueprint_collapsed"] = not bool(self.tasks[index].get("blueprint_collapsed", False))
        self.refresh()
        self.save_layout()

    # ---------- 分组操作 ----------

    def add_group(self, parent_group_id=None, position=None):
        self._push_history()
        group_id = f"group_{uuid.uuid4().hex[:8]}"
        names = self.group_metadata.setdefault("names", {})
        colors = self.group_metadata.setdefault("colors", {})
        parents = self.group_metadata.setdefault("parents", {})
        children = self.group_metadata.setdefault("children", {})
        order = self.group_metadata.setdefault("order", [])
        names[group_id] = f"组 {len(names) + 1}"
        colors[group_id] = "#eab308"
        parents[group_id] = str(parent_group_id) if parent_group_id else None
        children[group_id] = []
        if parent_group_id:
            children.setdefault(str(parent_group_id), []).append(group_id)
        else:
            order.append(group_id)
        # 记录空组组头位置，便于在右键位置显示
        group_positions = self.layout_data.setdefault("group_positions", {})
        if position is not None:
            group_positions[group_id] = (float(position[0]), float(position[1]))
        elif group_id not in group_positions:
            group_positions[group_id] = (80.0, 80.0 + 40.0 * len(group_positions))
        self.refresh()
        self.save_layout()
        return group_id

    def add_task(self, group_id=None, position=None):
        types = ["normal", "advanced", "loop", "keyboard_move", "key_press", "drag", "click_until_gone", "delay"]
        task_type, ok = QInputDialog.getItem(self, "新增步骤类型", "请选择步骤类型:", types, 0, False)
        if not ok:
            return
        self._push_history()
        new_task = {
            "id": str(uuid.uuid4()),
            "type": task_type,
            "mode": "custom",
            "enabled": True,
            "template": "new_step" if task_type in ("normal", "advanced") else "",
            "description": f"新增{task_type}步骤",
            "click": task_type in ("normal", "advanced", "click_until_gone"),
            "required": True,
        }
        if group_id:
            new_task["group_id"] = str(group_id)
            new_task["group_name"] = self._group_names().get(str(group_id), "默认分组")
        self.tasks.append(new_task)
        index = len(self.tasks) - 1
        if position is not None:
            px, py = float(position[0]), float(position[1])
        else:
            px, py = 80, 80 + (index % 4) * 120
        self.layout_data.setdefault("positions", {})[str(index)] = (px, py)
        self.current_index = index
        self.refresh()
        self._refreshing = True
        if index in self._node_items:
            self._node_items[index].setSelected(True)
        self._refreshing = False
        self._select_node(index)
        self.save_layout()

    def _add_group_menu(self, menu):
        if not any(isinstance(i, BlueprintNodeItem) for i in self.scene.selectedItems()):
            return
        group_menu = menu.addMenu("加入组")
        for group_id in self._group_order():
            self._add_group_menu_item(group_menu, group_id)
        remove_action = menu.addAction("移出当前组（放到根组）")
        remove_action.triggered.connect(self.remove_from_group)

    def _add_group_menu_item(self, menu, group_id, depth=0):
        label = ("  " * depth) + self._group_names().get(group_id, group_id)
        action = menu.addAction(label)
        action.triggered.connect(lambda checked=False, gid=group_id: self.set_selection_group(gid))
        for child in self._group_children().get(group_id, []):
            self._add_group_menu_item(menu, child, depth + 1)

    def set_selection_group(self, group_id):
        indices = sorted({i.index for i in self.scene.selectedItems() if isinstance(i, BlueprintNodeItem)})
        if not indices:
            return
        self._push_history()
        group_id = str(group_id)
        for index in indices:
            self.tasks[index]["group_id"] = group_id
            self.tasks[index]["group_name"] = self._group_names().get(group_id, "默认分组")
        self.refresh()
        self.save_layout()

    def remove_from_group(self):
        indices = sorted({i.index for i in self.scene.selectedItems() if isinstance(i, BlueprintNodeItem)})
        if not indices:
            return
        self._push_history()
        for index in indices:
            self.tasks[index].pop("group_id", None)
            self.tasks[index].pop("group_name", None)
        self.refresh()
        self.save_layout()

    def _edit_group(self, group_id):
        name, ok = QInputDialog.getText(self, "编辑组设置", "组名称:", text=self._group_names().get(group_id, group_id))
        if not ok:
            return
        current_color = QColor(self._group_colors().get(group_id, "#38bdf8"))
        color = QColorDialog.getColor(current_color, self, "选择组颜色")
        self._push_history()
        self.group_metadata.setdefault("names", {})[group_id] = name.strip() or group_id
        if color.isValid():
            self.group_metadata.setdefault("colors", {})[group_id] = color.name()
        for task in self.tasks:
            if str(task.get("group_id") or "group_default") == str(group_id):
                task["group_name"] = name.strip() or group_id
                if color.isValid():
                    task["group_color"] = color.name()
        self.refresh()
        self.save_layout()

    def _delete_group(self, group_id):
        if QMessageBox.question(self, "确认删除", "确定删除该组及其包含的步骤吗？") != QMessageBox.Yes:
            return
        self._push_history()
        remove_ids = {str(group_id)}
        remove_ids.update(str(g) for g in self._group_descendants(group_id))
        self.tasks[:] = [t for t in self.tasks if str(t.get("group_id") or "group_default") not in remove_ids]
        for gid in remove_ids:
            self.group_metadata.setdefault("names", {}).pop(gid, None)
            self.group_metadata.setdefault("colors", {}).pop(gid, None)
            self.group_metadata.setdefault("parents", {}).pop(gid, None)
            self.group_metadata.setdefault("children", {}).pop(gid, None)
            order = self.group_metadata.setdefault("order", [])
            if gid in order:
                order.remove(gid)
        self.refresh()
        self.save_layout()

    # ---------- 应用与校验 ----------

    def apply_blueprint(self):
        errors = self._validate_blueprint_connections()
        if errors:
            QMessageBox.critical(self, "蓝图连接无效", "\n".join(errors))
            return
        self._apply_blueprint_order()
        self.save_layout()
        self.refresh()
        self._refresh_editor_from_selection()

    def validate_blueprint(self):
        errors = NodeGraph(self.tasks).validate()
        errors.extend(self._validate_blueprint_connections())
        errors = list(dict.fromkeys(errors))
        if self.tasks:
            id_to_index = {str(t.get("id")): i for i, t in enumerate(self.tasks) if t.get("id") is not None}
            incoming = {
                id_to_index[str(t["flow_next"])]
                for t in self.tasks
                if t.get("flow_next") is not None and str(t["flow_next"]) in id_to_index
            }
            entry = next((i for i in range(len(self.tasks)) if i not in incoming), 0)
            reachable = set()
            pending = [entry]
            while pending:
                index = pending.pop()
                if index in reachable or not (0 <= index < len(self.tasks)):
                    continue
                reachable.add(index)
                task = self.tasks[index]
                targets = []
                flow_target = id_to_index.get(str(task.get("flow_next"))) if task.get("flow_next") is not None else None
                if flow_target is not None:
                    targets.append(flow_target)
                for key in ("detour_jump_to", "detour_success_jump_to", "condition_true_jump_to", "condition_false_jump_to", "switch_default_jump_to", "loop_target", "loop_exit_target", "event_timeout_target", "timeout_jump_to"):
                    value = task.get(key)
                    if value is not None:
                        target = self._resolve_number(value)
                        if target is not None:
                            targets.append(target)
                for value in (task.get("switch_cases") or {}).values():
                    target = self._resolve_number(value)
                    if target is not None:
                        targets.append(target)
                if flow_target is None and not task.get("flow_next_disabled") and index + 1 < len(self.tasks):
                    targets.append(index + 1)
                pending.extend(targets)
            unreachable = [str(i + 1) for i in range(len(self.tasks)) if i not in reachable]
            if unreachable:
                errors.append(f"不可达步骤: {', '.join(unreachable)}")
        if errors:
            QMessageBox.warning(self, "蓝图检查结果", "\n".join(errors))
        else:
            QMessageBox.information(self, "蓝图检查结果", "蓝图连接完整，未发现不可达步骤。")

    def _validate_blueprint_connections(self):
        errors = []
        task_ids = []
        for task in self.tasks:
            task.setdefault("id", str(uuid.uuid4()))
            task_ids.append(str(task.get("id")))
        duplicate_ids = sorted({tid for tid in task_ids if task_ids.count(tid) > 1})
        if duplicate_ids:
            errors.append(f"存在重复节点 ID: {', '.join(duplicate_ids)}")
        id_to_index = {str(t.get("id")): i for i, t in enumerate(self.tasks) if t.get("id") is not None}
        for index, task in enumerate(self.tasks):
            flow_target = task.get("flow_next")
            if flow_target is not None:
                target_index = id_to_index.get(str(flow_target))
                if target_index is None:
                    errors.append(f"步骤 {index + 1} 的普通连接目标不存在。")
                elif target_index == index:
                    errors.append(f"步骤 {index + 1} 不能连接到自身。")
            for key, label in (
                ("detour_jump_to", "未识别"), ("detour_success_jump_to", "识别成功"),
                ("timeout_jump_to", "超时"), ("condition_true_jump_to", "条件成立"),
                ("condition_false_jump_to", "条件不成立"), ("loop_target", "循环体"),
                ("loop_exit_target", "循环退出"),
            ):
                target_number = task.get(key)
                if target_number is None:
                    continue
                try:
                    target_index = int(target_number) - 1
                except (TypeError, ValueError):
                    errors.append(f"步骤 {index + 1} 的“{label}”目标不是有效编号。")
                    continue
                if not (0 <= target_index < len(self.tasks)):
                    errors.append(f"步骤 {index + 1} 的“{label}”目标不存在。")
                elif target_index == index:
                    errors.append(f"步骤 {index + 1} 的“{label}”不能跳转到自身。")
            switch_cases = task.get("switch_cases") or {}
            switch_targets = list(switch_cases.items()) if isinstance(switch_cases, dict) else []
            for case_value, target_number in switch_targets + [("默认", task.get("switch_default_jump_to"))]:
                if target_number is None:
                    continue
                try:
                    target_index = int(target_number) - 1
                except (TypeError, ValueError):
                    errors.append(f"步骤 {index + 1} 的 Switch「{case_value}」目标不是有效编号。")
                    continue
                if not (0 <= target_index < len(self.tasks)):
                    errors.append(f"步骤 {index + 1} 的 Switch「{case_value}」目标不存在。")
                elif target_index == index:
                    errors.append(f"步骤 {index + 1} 的 Switch「{case_value}」不能连接到自身。")

        flow_state = {}

        def visit(idx):
            state = flow_state.get(idx, 0)
            if state == 1:
                return True
            if state == 2:
                return False
            flow_state[idx] = 1
            target_id = self.tasks[idx].get("flow_next")
            target_idx = id_to_index.get(str(target_id)) if target_id is not None else None
            has_cycle = target_idx is not None and visit(target_idx)
            flow_state[idx] = 2
            return has_cycle

        for index in range(len(self.tasks)):
            if visit(index):
                errors.append("蓝图普通连接形成循环，请拆开循环或改用迂回跳转。")
                break
        return errors

    def _apply_blueprint_order(self):
        if not self.tasks or not any(t.get("flow_next") for t in self.tasks):
            return
        old_tasks = list(self.tasks)
        id_to_index = {str(t.get("id")): i for i, t in enumerate(old_tasks) if t.get("id") is not None}
        incoming = {
            id_to_index[str(t["flow_next"])]
            for t in old_tasks
            if t.get("flow_next") is not None and str(t["flow_next"]) in id_to_index
        }
        starts = [i for i in range(len(old_tasks)) if i not in incoming]
        ordered = []
        visited = set()
        for start in starts + list(range(len(old_tasks))):
            index = start
            while index not in visited:
                visited.add(index)
                ordered.append(index)
                next_id = old_tasks[index].get("flow_next")
                next_index = id_to_index.get(str(next_id)) if next_id is not None else None
                if next_index is None:
                    break
                index = next_index
        if ordered == list(range(len(old_tasks))):
            return
        old_number_by_id = {str(t.get("id")): i + 1 for i, t in enumerate(old_tasks) if t.get("id") is not None}
        new_number_by_id = {
            str(old_tasks[old_index].get("id")): new_index + 1
            for new_index, old_index in enumerate(ordered)
            if old_tasks[old_index].get("id") is not None
        }
        old_positions = self.layout_data.get("positions", {})
        new_positions = {
            str(new_index): old_positions.get(str(old_index), old_positions.get(old_index, (40 + (new_index % 3) * 280, 40 + (new_index // 3) * 130)))
            for new_index, old_index in enumerate(ordered)
        }
        reordered = [old_tasks[i] for i in ordered]
        for task in reordered:
            for key in ("detour_jump_to", "detour_success_jump_to"):
                old_target = task.get(key)
                if old_target is None:
                    continue
                target_id = next((tid for tid, num in old_number_by_id.items() if num == int(old_target)), None)
                task[key] = new_number_by_id.get(target_id, old_target)
        self.tasks[:] = reordered
        self.layout_data["positions"] = new_positions

    def closeEvent(self, event):
        self.save_layout()
        super().closeEvent(event)


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
            self.log.emit(f"脚本异常: {exc}")
            self.completed.emit("failed")
        finally:
            self.finished.emit()


class PySide6ScriptWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("视觉识别自动脚本 - PySide6")
        self.resize(1180, 860)
        self.setMinimumSize(980, 720)

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.single_step_event = threading.Event()
        self.worker_thread = None
        self.worker = None
        self.completion_notified = False
        self.capture_overlay = None
        self.blueprint_window = None
        self.execution_states = {}
        self.current_task_index = -1
        self.current_group_id = None
        self._pending_group_color = None
        self.status_text = "待机"
        self.deleted_preset_names = set(DELETED_PRESET_NAMES)
        self.mode_group_metadata = deepcopy(PRESET_METADATA)
        self.blueprint_layouts = load_blueprint_layouts()
        self.blueprint_graphs = load_blueprint_graphs()
        self.mode_tasks = {"custom": deepcopy(TASKS)}
        self.mode_tasks.update({name: deepcopy(value) for name, value in USER_PRESETS.items()})

        self._build_ui()
        self._load_modes()
        self.refresh_window_list()
        self.refresh_task_list()

    def _build_ui(self):
        root = QWidget(self)
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(14, 12, 14, 14)
        root_layout.setSpacing(8)

        title = QLabel("脚本编辑器")
        title.setObjectName("titleLabel")
        root_layout.addWidget(title)

        preset_bar = QHBoxLayout()
        self.loop_checkbox = QCheckBox("循环执行")
        preset_bar.addWidget(self.loop_checkbox)
        preset_bar.addWidget(QLabel("执行功能:"))
        self.mode_combo = QComboBox()
        self.mode_combo.currentTextChanged.connect(self.on_mode_selected)
        self.mode_combo.setMinimumWidth(118)
        preset_bar.addWidget(self.mode_combo)
        self.preset_buttons = []
        for label, handler in (
            ("新建预设", self.create_preset),
            ("重命名预设", self.rename_current_preset),
            ("复制到预设", self.copy_current_preset),
            ("导出预设", self.export_current_preset),
            ("导入预设", self.import_preset),
            ("删除预设", self.delete_current_preset),
            ("保存当前任务", self.save_current_tasks),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            preset_bar.addWidget(button)
            self.preset_buttons.append(button)
        root_layout.addLayout(preset_bar)

        control_bar = QHBoxLayout()
        self.start_button = QPushButton("开始执行")
        self.start_button.clicked.connect(self.start_script)
        control_bar.addWidget(self.start_button)
        self.start_current_button = QPushButton("从当前步骤执行")
        self.start_current_button.clicked.connect(self.start_from_current)
        control_bar.addWidget(self.start_current_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self.stop_script)
        self.stop_button.setEnabled(False)
        control_bar.addWidget(self.stop_button)
        self.pause_button = QPushButton("暂停")
        self.pause_button.clicked.connect(self.toggle_pause)
        self.pause_button.setEnabled(False)
        control_bar.addWidget(self.pause_button)
        self.step_button = QPushButton("单步")
        self.step_button.clicked.connect(self.step_script)
        self.step_button.setEnabled(False)
        control_bar.addWidget(self.step_button)
        control_bar.addSpacing(8)
        control_bar.addWidget(QLabel("目标窗口:"))
        self.window_combo = QComboBox()
        self.window_combo.setEditable(True)
        control_bar.addWidget(self.window_combo, 1)
        refresh_button = QPushButton("刷新窗口")
        refresh_button.clicked.connect(self.refresh_window_list)
        control_bar.addWidget(refresh_button)
        self.status_label = QLabel("状态: 待机")
        self.status_label.setMinimumWidth(100)
        control_bar.addWidget(self.status_label)
        root_layout.addLayout(control_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        task_panel = QGroupBox("项目列表")
        task_layout = QVBoxLayout(task_panel)
        self.task_list = TaskListWidget()
        self.task_list.setColumnCount(1)
        self.task_list.setHeaderHidden(True)
        self.task_list.setItemDelegate(TaskItemDelegate(self.task_list))
        self.task_list.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.task_list.setDragDropMode(QTreeWidget.InternalMove)
        self.task_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.task_list.currentItemChanged.connect(self.load_selected_task)
        self.task_list.itemDoubleClicked.connect(self.toggle_item)
        self.task_list.itemChanged.connect(self.on_item_changed)
        self.task_list.toggle_requested.connect(self.toggle_selected_tasks)
        self.task_list.order_changed.connect(self.persist_task_order)
        self.task_list.task_drop_requested.connect(self.reorder_task_from_tree)
        self.task_list.task_drop_to_end_requested.connect(self.move_task_to_end)
        self.task_buttons = []
        button_specs = [
            ("全选", self.select_all_tasks),
            ("清空", self.clear_tasks),
            ("上移", lambda: self.move_selected_item(-1)),
            ("下移", lambda: self.move_selected_item(1)),
            ("新增组", self.add_group),
            ("新增步骤", self.add_task),
            ("复制", self.copy_selected_item),
            ("删除", self.delete_selected_item),
            ("打开蓝图流程", self.open_blueprint),
        ]
        button_grid = QGridLayout()
        button_grid.setContentsMargins(0, 0, 0, 0)
        button_grid.setHorizontalSpacing(8)
        button_grid.setVerticalSpacing(4)
        for index, (label, handler) in enumerate(button_specs):
            button = QPushButton(label)
            button.clicked.connect(handler)
            button_grid.addWidget(button, index // 4, index % 4)
            self.task_buttons.append(button)
        task_layout.addLayout(button_grid)
        task_layout.addWidget(self.task_list)
        splitter.addWidget(task_panel)

        editor_panel = QGroupBox("当前步骤")
        editor_panel_layout = QVBoxLayout(editor_panel)
        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor_content = QWidget()
        editor_layout = QVBoxLayout(editor_content)
        editor_scroll.setWidget(editor_content)
        editor_panel_layout.addWidget(editor_scroll)
        self.selected_label = QLabel("未选择步骤")
        self.selected_label.setWordWrap(True)
        editor_layout.addWidget(self.selected_label)
        self.template_preview = QLabel("无模板预览")
        self.template_preview.setAlignment(Qt.AlignCenter)
        self.template_preview.setFixedSize(180, 110)
        self.template_preview.setStyleSheet("border: 1px solid #cbd5e1; background: #f8fafc;")
        self.template_preview.setVisible(False)

        self.editor_actions = QWidget()
        action_layout = QHBoxLayout(self.editor_actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        bind_button = QPushButton("绑定图片")
        bind_button.clicked.connect(self.select_template_file)
        action_layout.addWidget(bind_button)
        click_capture_button = QPushButton("记录点击点")
        click_capture_button.clicked.connect(self.start_click_capture)
        action_layout.addWidget(click_capture_button)
        region_capture_button = QPushButton("框选识别区域")
        region_capture_button.clicked.connect(self.start_region_capture)
        action_layout.addWidget(region_capture_button)
        clear_region_button = QPushButton("清空识别区域")
        clear_region_button.clicked.connect(self.clear_match_region)
        action_layout.addWidget(clear_region_button)
        self.apply_button = QPushButton("应用修改")
        self.apply_button.clicked.connect(self.apply_selected_task)
        action_layout.addWidget(self.apply_button)
        editor_layout.addWidget(self.editor_actions)

        # 步骤名称与启用状态对所有步骤类型可见（与蓝图保持一致）
        self.description_edit = QLineEdit()
        self.enabled_checkbox = QCheckBox("启用步骤")
        name_form = QFormLayout()
        name_form.addRow("步骤名称:", self.description_edit)
        name_form.addRow("启用状态:", self.enabled_checkbox)
        editor_layout.addLayout(name_form)

        self.recognition_group = QWidget()
        recognition_layout = QVBoxLayout(self.recognition_group)
        recognition_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addWidget(self.recognition_group)

        self.click_until_group = QGroupBox("持续点击设置")
        click_until_layout = QVBoxLayout(self.click_until_group)
        click_until_layout.setContentsMargins(0, 0, 0, 0)
        self.click_until_template_edit = QLineEdit()
        self.click_until_interval_edit = QLineEdit()
        self.click_until_stop_delay_edit = QLineEdit()
        self.click_until_timeout_edit = QLineEdit()
        self.click_until_continue_checkbox = QCheckBox("超时后继续执行")
        self.click_until_stop_on_change_checkbox = QCheckBox("画面变化视为成功")

        def add_click_until_row(label, widget):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(label), 0)
            row.addWidget(widget, 1)
            click_until_layout.addLayout(row)

        add_click_until_row("模板名(逗号分隔):", self.click_until_template_edit)
        add_click_until_row("点击间隔(秒):", self.click_until_interval_edit)
        add_click_until_row("识别后停止延时(秒):", self.click_until_stop_delay_edit)
        add_click_until_row("超时(秒):", self.click_until_timeout_edit)
        click_until_layout.addWidget(self.click_until_continue_checkbox)
        click_until_layout.addWidget(self.click_until_stop_on_change_checkbox)
        self.click_until_group.setVisible(False)
        editor_layout.addWidget(self.click_until_group)

        self.template_edit = QLineEdit()
        self.threshold_edit = QLineEdit()
        self.timeout_edit = QLineEdit()
        self.after_wait_edit = QLineEdit()
        self.click_checkbox = QCheckBox("执行点击")
        self.match_required_checkbox = QCheckBox("必须识别到图片再点击")
        self.optional_checkbox = QCheckBox("可选步骤（跳过）")
        self.offset_x_edit = QLineEdit()
        self.offset_y_edit = QLineEdit()
        self.click_x_edit = QLineEdit()
        self.click_y_edit = QLineEdit()
        self.region_left_edit = QLineEdit()
        self.region_top_edit = QLineEdit()
        self.region_right_edit = QLineEdit()
        self.region_bottom_edit = QLineEdit()
        self.region_center_x_edit = QLineEdit()
        self.region_center_y_edit = QLineEdit()
        self.match_rect_edit = QLineEdit()
        self.next_template_edit = QLineEdit()
        self.wait_for_combo = QComboBox()
        self.wait_for_combo.addItems(["1. 画面结果变化", "2. 等待目标模板出现", "3. 画面变化后目标结果出现"])

        def add_row(label, widget):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(label), 0)
            row.addWidget(widget, 1)
            recognition_layout.addLayout(row)

        add_row("模板名:", self.template_edit)
        add_row("X偏移:", self.offset_x_edit)
        add_row("Y偏移:", self.offset_y_edit)
        add_row("点击X:", self.click_x_edit)
        add_row("点击Y:", self.click_y_edit)
        add_row("匹配阈值(0-1):", self.threshold_edit)

        def add_coordinate_row(label, x_edit, y_edit):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(label), 0)
            row.addWidget(QLabel("X"), 0)
            row.addWidget(x_edit, 1)
            row.addWidget(QLabel("Y"), 0)
            row.addWidget(y_edit, 1)
            recognition_layout.addLayout(row)

        add_coordinate_row("左上:", self.region_left_edit, self.region_top_edit)
        add_coordinate_row("右下:", self.region_right_edit, self.region_bottom_edit)
        add_coordinate_row("中心:", self.region_center_x_edit, self.region_center_y_edit)

        next_row = QHBoxLayout()
        next_row.setContentsMargins(0, 0, 0, 0)
        next_row.addWidget(QLabel("下一模板:"), 0)
        next_row.addWidget(self.next_template_edit, 1)
        next_button = QPushButton("选择图片")
        next_button.clicked.connect(self.select_next_template_file)
        next_row.addWidget(next_button)
        next_capture_button = QPushButton("手动框选图片")
        next_capture_button.clicked.connect(self.start_next_template_capture)
        next_row.addWidget(next_capture_button)
        next_region_button = QPushButton("框选出现位置")
        next_region_button.clicked.connect(self.start_next_region_capture)
        next_row.addWidget(next_region_button)
        recognition_layout.addLayout(next_row)

        timeout_row = QHBoxLayout()
        timeout_row.setContentsMargins(0, 0, 0, 0)
        timeout_row.addWidget(QLabel("超时(秒，0为不限制):"), 0)
        timeout_row.addWidget(self.timeout_edit, 1)
        recognition_layout.addLayout(timeout_row)

        wait_row = QHBoxLayout()
        wait_row.setContentsMargins(0, 0, 0, 0)
        wait_row.addWidget(QLabel("等待方式:"), 0)
        wait_row.addWidget(self.wait_for_combo, 1)
        wait_row.addWidget(QLabel("完成后等待(秒):"), 0)
        wait_row.addWidget(self.after_wait_edit, 1)
        recognition_layout.addLayout(wait_row)

        options_row = QHBoxLayout()
        options_row.setContentsMargins(0, 0, 0, 0)
        options_row.addWidget(self.click_checkbox)
        options_row.addWidget(self.match_required_checkbox)
        options_row.addWidget(self.optional_checkbox)
        self.detour_button = QPushButton("迂回")
        self.detour_button.clicked.connect(self.open_detour_editor)
        options_row.addWidget(self.detour_button)
        options_row.addStretch(1)
        recognition_layout.addLayout(options_row)

        self.special_group = QGroupBox("类型专用字段")
        self.special_form = QFormLayout(self.special_group)
        self.special_edits = {}
        editor_layout.addWidget(self.special_group)

        # 组设置（选中组时显示）
        self.group_form = QGroupBox("组设置")
        group_form_layout = QFormLayout(self.group_form)
        self.group_name_edit = QLineEdit()
        self.group_color_button = QPushButton("选择组颜色")
        self.group_color_button.clicked.connect(self._pick_group_color)
        group_form_layout.addRow("组名称:", self.group_name_edit)
        group_form_layout.addRow("组颜色:", self.group_color_button)
        self.group_apply_button = QPushButton("应用组设置")
        self.group_apply_button.clicked.connect(self._apply_group_editor)
        group_form_layout.addRow(self.group_apply_button)
        self.group_form.setVisible(False)
        editor_layout.addWidget(self.group_form)

        editor_layout.addStretch(1)
        splitter.addWidget(editor_panel)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([320, 500])

        log_panel = QGroupBox("日志输出")
        log_layout = QVBoxLayout(log_panel)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        log_layout.addWidget(self.log_box)
        content_splitter = QSplitter(Qt.Orientation.Vertical)
        content_splitter.addWidget(splitter)
        content_splitter.addWidget(log_panel)
        content_splitter.setStretchFactor(0, 5)
        content_splitter.setStretchFactor(1, 1)
        content_splitter.setSizes([650, 170])
        root_layout.addWidget(content_splitter, 1)

        checked_indicator = os.path.join(os.path.dirname(__file__), "icons", "checkbox_checked.svg").replace("\\", "/")
        indeterminate_indicator = os.path.join(os.path.dirname(__file__), "icons", "checkbox_indeterminate.svg").replace("\\", "/")
        self.setStyleSheet(
            "QMainWindow { background: #f3f5f8; color: #1f2937; }"
            "QGroupBox { border: 1px solid #cbd5e1; border-radius: 6px; margin-top: 10px; padding: 10px; font-weight: bold; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #334155; }"
            "#titleLabel { color: #0f172a; font-size: 18px; font-weight: bold; }"
            "QPushButton { min-height: 26px; padding: 3px 10px; border: 1px solid #94a3b8; border-radius: 4px; background: #ffffff; color: #1e293b; }"
            "QPushButton:hover { background: #e0f2fe; border-color: #0284c7; }"
            "QPushButton:pressed { background: #bae6fd; }"
            "QPushButton:disabled { color: #94a3b8; background: #e2e8f0; }"
            "QLineEdit, QPlainTextEdit, QComboBox, QTreeWidget { border: 1px solid #cbd5e1; border-radius: 4px; background: #ffffff; padding: 4px; }"
            "QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QTreeWidget:focus { border-color: #0284c7; }"
            "QTreeWidget { padding: 6px; }"
            "QTreeWidget::item { padding: 4px; }"
            "QTreeWidget::item:selected { background: #dbeafe; color: #0f172a; }"
            "QTreeWidget::indicator { width: 16px; height: 16px; border: 2px solid #94a3b8; border-radius: 4px; background: #ffffff; }"
            "QTreeWidget::indicator:hover { border-color: #0284c7; background: #f0f9ff; }"
            f'QTreeWidget::indicator:checked {{ border-color: #0284c7; background: #0284c7; image: url("{checked_indicator}"); }}'
            "QTreeWidget::indicator:checked:hover { border-color: #0369a1; background: #0369a1; }"
            f'QTreeWidget::indicator:indeterminate {{ border-color: #0284c7; background: #bae6fd; image: url("{indeterminate_indicator}"); }}'
            "QSplitter::handle { background: #cbd5e1; }"
        )

    def _load_modes(self):
        modes = ["custom"] + sorted(USER_PRESETS)
        self.mode_combo.blockSignals(True)
        self.mode_combo.addItems(modes)
        self.mode_combo.setCurrentText("custom")
        self.mode_combo.blockSignals(False)

    def _save_presets(self):
        payload = {
            name: deepcopy(tasks)
            for name, tasks in self.mode_tasks.items()
            if name != "custom"
        }
        payload["__deleted__"] = sorted(self.deleted_preset_names)
        payload["__group_metadata__"] = deepcopy(self.mode_group_metadata)
        save_presets(payload)

    def _refresh_mode_combo(self, selected=None):
        selected = selected or self.mode_combo.currentText() or "custom"
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        self.mode_combo.addItems(["custom"] + sorted(name for name in self.mode_tasks if name != "custom"))
        self.mode_combo.setCurrentText(selected if selected in self.mode_tasks else "custom")
        self.mode_combo.blockSignals(False)

    @Slot()
    def create_preset(self):
        name, accepted = QInputDialog.getText(self, "新建预设", "预设名称:")
        name = name.strip()
        if not accepted:
            return
        if not name or name == "custom":
            QMessageBox.warning(self, "名称无效", "请输入非 custom 的预设名称。")
            return
        if name in self.mode_tasks:
            QMessageBox.warning(self, "名称重复", "该预设已经存在。")
            return
        self.mode_tasks[name] = []
        self._save_presets()
        self._refresh_mode_combo(name)
        self.on_mode_selected(name)

    @Slot()
    def rename_current_preset(self):
        old_name = self.mode_combo.currentText() or "custom"
        if old_name == "custom":
            QMessageBox.warning(self, "无法重命名", "custom 是自定义任务，不能重命名。")
            return
        new_name, accepted = QInputDialog.getText(self, "重命名预设", "新名称:", text=old_name)
        new_name = new_name.strip()
        if not accepted:
            return
        if not new_name or new_name == "custom":
            QMessageBox.warning(self, "名称无效", "请输入非 custom 的预设名称。")
            return
        if new_name != old_name and new_name in self.mode_tasks:
            QMessageBox.warning(self, "名称重复", "该预设已经存在。")
            return
        self.mode_tasks[new_name] = self.mode_tasks.pop(old_name)
        self.deleted_preset_names.discard(new_name)
        self.deleted_preset_names.add(old_name)
        self._save_presets()
        self._refresh_mode_combo(new_name)
        self.on_mode_selected(new_name)

    @Slot()
    def copy_current_preset(self):
        source_name = self.mode_combo.currentText() or "custom"
        target_name, accepted = QInputDialog.getText(self, "复制预设", "新预设名称:")
        target_name = target_name.strip()
        if not accepted:
            return
        if not target_name or target_name == "custom":
            QMessageBox.warning(self, "名称无效", "请输入非 custom 的预设名称。")
            return
        if target_name in self.mode_tasks:
            QMessageBox.warning(self, "名称重复", "该预设已经存在。")
            return
        self.mode_tasks[target_name] = deepcopy(self.mode_tasks.get(source_name, TASKS))
        self._save_presets()
        self._refresh_mode_combo(target_name)
        self.on_mode_selected(target_name)

    @Slot()
    def delete_current_preset(self):
        name = self.mode_combo.currentText() or "custom"
        if name == "custom":
            QMessageBox.warning(self, "无法删除", "custom 是自定义任务，不能删除。")
            return
        answer = QMessageBox.question(self, "确认删除", f"确定删除预设“{name}”吗？")
        if answer != QMessageBox.Yes:
            return
        self.mode_tasks.pop(name, None)
        self.deleted_preset_names.add(name)
        self._save_presets()
        self._refresh_mode_combo("custom")
        self.on_mode_selected("custom")

    def _collect_bound_image_names(self, value):
        names = set()
        image_keys = {
            "template", "templates", "condition_template", "condition_templates",
            "event_template", "next_template", "next_templates", "stage_templates",
        }
        if isinstance(value, dict):
            for key, item in value.items():
                if key in image_keys or isinstance(item, (dict, list, tuple)):
                    names.update(self._collect_bound_image_names(item))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                names.update(self._collect_bound_image_names(item))
        elif isinstance(value, str):
            for item in value.replace("，", ",").split(","):
                item = item.strip()
                if item:
                    names.add(os.path.splitext(os.path.basename(item))[0])
        return names

    @Slot()
    def export_current_preset(self):
        preset_name = self.mode_combo.currentText() or "custom"
        self.save_current_tasks()
        output_path, _ = QFileDialog.getSaveFileName(
            self, "导出预设", f"{preset_name}.zip", "预设压缩包 (*.zip)"
        )
        if not output_path:
            return
        tasks = deepcopy(self.mode_tasks.get(preset_name, TASKS))
        image_names = sorted(self._collect_bound_image_names(tasks))
        payload = {
            "format": "visionflow-preset",
            "version": 1,
            "preset_name": preset_name,
            "tasks": tasks,
            "group_metadata": deepcopy(self.mode_group_metadata.get(preset_name, {})),
            "blueprint_layout": deepcopy(self.blueprint_layouts.get(preset_name, {})),
            "blueprint_graph": deepcopy(self.blueprint_graphs.get(preset_name, {})),
            "images": image_names,
        }
        try:
            with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("preset.json", json.dumps(payload, ensure_ascii=False, indent=2))
                for image_name in image_names:
                    image_path = os.path.join(config.ICON_DIR, f"{image_name}.png")
                    if os.path.isfile(image_path):
                        archive.write(image_path, f"icons/{image_name}.png")
        except (OSError, zipfile.BadZipFile) as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.append_log(f"已导出预设“{preset_name}”：{len(tasks)} 个步骤。")
        QMessageBox.information(self, "导出完成", f"预设已导出到：\n{output_path}")

    @Slot()
    def import_preset(self):
        input_path, _ = QFileDialog.getOpenFileName(self, "导入预设", "", "预设压缩包 (*.zip)")
        if not input_path:
            return
        try:
            with zipfile.ZipFile(input_path, "r") as archive:
                if "preset.json" not in archive.namelist():
                    raise ValueError("压缩包中缺少 preset.json。")
                payload = json.loads(archive.read("preset.json").decode("utf-8"))
                if payload.get("format") != "visionflow-preset":
                    raise ValueError("不是有效的脚本编辑器预设文件。")
                imported_tasks = payload.get("tasks")
                if not isinstance(imported_tasks, list):
                    raise ValueError("预设步骤数据无效。")
                suggested_name = str(payload.get("preset_name") or "导入预设").strip() or "导入预设"
                target_name, accepted = QInputDialog.getText(self, "导入预设名称", "保存为预设名称:", text=suggested_name)
                target_name = target_name.strip()
                if not accepted:
                    return
                if not target_name or target_name.lower() == "custom":
                    raise ValueError("请输入非 custom 的预设名称。")
                if target_name in self.mode_tasks:
                    answer = QMessageBox.question(self, "覆盖预设", f"预设“{target_name}”已存在，是否覆盖？")
                    if answer != QMessageBox.Yes:
                        return
                for member in archive.namelist():
                    if not member.startswith("icons/") or not member.lower().endswith(".png"):
                        continue
                    filename = os.path.basename(member)
                    if not filename:
                        continue
                    os.makedirs(config.ICON_DIR, exist_ok=True)
                    with open(os.path.join(config.ICON_DIR, filename), "wb") as image_file:
                        image_file.write(archive.read(member))
        except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return

        self.mode_tasks[target_name] = deepcopy(imported_tasks)
        self.mode_group_metadata[target_name] = deepcopy(payload.get("group_metadata") or {})
        self.blueprint_layouts[target_name] = deepcopy(payload.get("blueprint_layout") or {})
        self.blueprint_graphs[target_name] = deepcopy(payload.get("blueprint_graph") or {})
        self.deleted_preset_names.discard(target_name)
        self._save_presets()
        save_blueprint_layouts(self.blueprint_layouts)
        save_blueprint_graphs(self.blueprint_graphs)
        self._refresh_mode_combo(target_name)
        self.on_mode_selected(target_name)
        reload_templates()
        self.append_log(f"已导入预设“{target_name}”：{len(imported_tasks)} 个步骤。")

    def append_log(self, message):
        self.log_box.appendPlainText(str(message))

    @Slot(str)
    def on_mode_selected(self, mode):
        mode = mode or "custom"
        self.mode_tasks.setdefault(mode, deepcopy(get_tasks_for_mode(mode)))
        TASKS[:] = deepcopy(self.mode_tasks[mode])
        self.current_task_index = -1
        self.refresh_task_list()
        self.append_log(f"已切换到预设: {mode}")

    @Slot()
    def refresh_task_list(self):
        self.task_list.blockSignals(True)
        self.task_list.clear()
        groups = {}
        group_metadata = self.mode_group_metadata.get(self.mode_combo.currentText() or "custom", {})
        group_names = group_metadata.get("names", {}) if isinstance(group_metadata, dict) else {}
        group_colors = group_metadata.get("colors", {}) if isinstance(group_metadata, dict) else {}

        def make_group(group_id):
            group_label = group_names.get(group_id) or ("默认分组" if group_id == "group_default" else group_id)
            group = QTreeWidgetItem(["分组: " + str(group_label)])
            group.setBackground(0, QColor(group_colors.get(group_id, "#e0f2fe")))
            group.setData(0, Qt.UserRole, "group")
            group.setData(0, Qt.UserRole + 1, group_id)
            group.setFlags(group.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            self.task_list.addTopLevelItem(group)
            groups[group_id] = group
            return group

        for index, task in enumerate(TASKS):
            task.setdefault("id", str(uuid.uuid4()))
            description = task.get("description", task.get("template", "未命名步骤"))
            task_type = task.get("type", "normal")
            group_id = str(task.get("group_id") or "group_default")
            if group_id not in groups:
                make_group(group_id)
            detour_status = " · 已启用迂回" if task.get("detour_enabled") else ""
            item = QTreeWidgetItem([f"{index + 1:02d}. [{task_type}] {description}{detour_status}"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setData(0, Qt.UserRole, str(task.get("id", index)))
            item.setData(0, Qt.UserRole + 1, index)
            item.setCheckState(0, Qt.CheckState.Checked if task.get("enabled", True) else Qt.CheckState.Unchecked)
            groups[group_id].addChild(item)
            groups[group_id].setExpanded(True)

        # 显示元数据中存在但暂无步骤的组（保证“新增组”后立即可见）
        if isinstance(group_metadata, dict):
            group_order = group_metadata.get("order", []) if isinstance(group_metadata.get("order"), list) else []
            for group_id in group_order:
                gid = str(group_id)
                if gid in groups or gid not in group_names:
                    continue
                make_group(gid)

        # 依据子步骤启用情况回填组勾选状态
        for group in groups.values():
            states = [group.child(i).checkState(0) == Qt.CheckState.Checked for i in range(group.childCount())]
            if not states:
                group.setCheckState(0, Qt.CheckState.Checked)
            elif all(states):
                group.setCheckState(0, Qt.CheckState.Checked)
            elif any(states):
                group.setCheckState(0, Qt.CheckState.PartiallyChecked)
            else:
                group.setCheckState(0, Qt.CheckState.Unchecked)

        self.task_list.blockSignals(False)
        if TASKS:
            target_index = min(max(self.current_task_index, 0), len(TASKS) - 1)
            self.select_task_index(target_index)
        else:
            self.clear_editor()

    def select_task_index(self, index):
        for group_index in range(self.task_list.topLevelItemCount()):
            group = self.task_list.topLevelItem(group_index)
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                if child.data(0, Qt.UserRole + 1) == index:
                    self.task_list.setCurrentItem(child)
                    return

    def _task_index_from_item(self, item):
        if item is None or item.data(0, Qt.UserRole) == "group":
            return None
        index = item.data(0, Qt.UserRole + 1)
        return int(index) if index is not None else None

    @Slot(QTreeWidgetItem, QTreeWidgetItem)
    def load_selected_task(self, item, _previous_item):
        if item is not None and item.data(0, Qt.UserRole) == "group":
            self._load_editor_for_group(str(item.data(0, Qt.UserRole + 1)))
            return
        index = self._task_index_from_item(item)
        if index is None or not 0 <= index < len(TASKS):
            self.current_task_index = -1
            self.clear_editor()
            return
        self.current_task_index = index
        self.current_group_id = None
        self.group_form.setVisible(False)
        task = TASKS[index]
        self.selected_label.setText(f"类型: {task.get('type', 'normal')} | ID: {task.get('id', '')}")
        is_click_until = task.get("type") == "click_until_gone"
        is_recognition = task.get("type", "normal") in ("normal", "advanced")
        self.editor_actions.setVisible(is_recognition or is_click_until)
        self.recognition_group.setVisible(is_recognition)
        self.click_until_group.setVisible(is_click_until)
        self.special_group.setTitle("持续点击设置" if is_click_until else "类型专用字段" if is_recognition else "步骤设置")
        self.description_edit.setText(str(task.get("description", "")))
        self.enabled_checkbox.setChecked(bool(task.get("enabled", True)))
        templates = task.get("templates") or task.get("template", "")
        if isinstance(templates, (list, tuple)):
            templates = ", ".join(str(item) for item in templates)
        self.template_edit.setText(str(templates))
        self._update_template_preview(templates)
        click_until_templates = task.get("templates") or task.get("template", "")
        if isinstance(click_until_templates, (list, tuple)):
            click_until_templates = ", ".join(str(item) for item in click_until_templates)
        self.click_until_template_edit.setText(str(click_until_templates))
        self.click_until_interval_edit.setText(str(task.get("click_interval", 0.5)))
        self.click_until_stop_delay_edit.setText(str(task.get("stop_delay", 0.0)))
        self.click_until_timeout_edit.setText(str(task.get("timeout", 30)))
        self.click_until_continue_checkbox.setChecked(bool(task.get("continue_after_timeout", False)))
        self.click_until_stop_on_change_checkbox.setChecked(bool(task.get("stop_on_change", False)))
        self.threshold_edit.setText(str(task.get("threshold", config.THRESHOLD)))
        self.timeout_edit.setText(str(task.get("timeout", task.get("wait_timeout", 5))))
        self.after_wait_edit.setText(str(task.get("after_wait", 0.25)))
        self.click_checkbox.setChecked(bool(task.get("click", True)))
        self.match_required_checkbox.setChecked(bool(task.get("click_requires_match", True)))
        self.offset_x_edit.setText(str(task.get("offset_x", task.get("offset", (0, 0))[0] if isinstance(task.get("offset"), (list, tuple)) else 0)))
        self.offset_y_edit.setText(str(task.get("offset_y", task.get("offset", (0, 0))[1] if isinstance(task.get("offset"), (list, tuple)) else 0)))
        click_position = task.get("click_position")
        self.click_x_edit.setText(str(task.get("click_x", click_position[0] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
        self.click_y_edit.setText(str(task.get("click_y", click_position[1] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
        rects = task.get("match_rects")
        rect = (rects[0] if isinstance(rects, list) and rects else None) or task.get("match_rect") or task.get("search_rect")
        self.match_rect_edit.setText(", ".join(str(value) for value in rect[:4]) if isinstance(rect, (list, tuple)) and len(rect) >= 4 else "")
        region_values = list(rect[:4]) if isinstance(rect, (list, tuple)) and len(rect) >= 4 else ["", "", "", ""]
        self.region_left_edit.setText(str(region_values[0]))
        self.region_top_edit.setText(str(region_values[1]))
        self.region_right_edit.setText(str(region_values[2]))
        self.region_bottom_edit.setText(str(region_values[3]))
        self.region_center_x_edit.setText(str((region_values[0] + region_values[2]) // 2) if all(isinstance(value, int) for value in region_values) else "")
        self.region_center_y_edit.setText(str((region_values[1] + region_values[3]) // 2) if all(isinstance(value, int) for value in region_values) else "")
        self._rebuild_special_form(task)

    def _load_editor_for_group(self, group_id):
        self.current_group_id = str(group_id)
        self.current_task_index = -1
        self.selected_label.setText("组设置")
        self.template_preview.setVisible(False)
        self.editor_actions.setVisible(False)
        self.recognition_group.setVisible(False)
        self.special_group.setVisible(False)
        self.group_form.setVisible(True)
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        names = metadata.get("names", {}) if isinstance(metadata, dict) else {}
        self.group_name_edit.setText(names.get(str(group_id), str(group_id)))
        self._pending_group_color = None
        self._update_group_color_button(str(group_id))

    def _update_group_color_button(self, group_id):
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        colors = metadata.get("colors", {}) if isinstance(metadata, dict) else {}
        color = self._pending_group_color or colors.get(str(group_id), "#e0f2fe")
        self.group_color_button.setText(color)
        self.group_color_button.setStyleSheet(f"background: {color}; color: #1f2937;")

    def _pick_group_color(self):
        if self.current_group_id is None:
            return
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        colors = metadata.get("colors", {}) if isinstance(metadata, dict) else {}
        current = QColor(self._pending_group_color or colors.get(self.current_group_id, "#e0f2fe"))
        color = QColorDialog.getColor(current, self, "选择分组颜色")
        if color.isValid():
            self._pending_group_color = color.name()
            self._update_group_color_button(self.current_group_id)

    def _apply_group_editor(self):
        if self.current_group_id is None:
            return
        group_id = self.current_group_id
        name = self.group_name_edit.text().strip() or group_id
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        metadata.setdefault("names", {})[group_id] = name
        if self._pending_group_color is not None:
            metadata.setdefault("colors", {})[group_id] = self._pending_group_color
        for task in TASKS:
            if str(task.get("group_id") or "group_default") == group_id:
                task["group_name"] = name
                if self._pending_group_color is not None:
                    task["group_color"] = self._pending_group_color
        self._save_presets()
        self.refresh_task_list()
        self._load_editor_for_group(group_id)
        self.append_log(f"已应用组设置: {name}")

    def _rebuild_special_form(self, task):
        while self.special_form.rowCount():
            self.special_form.removeRow(0)
        self.special_edits = {}
        # 主窗口通用表单已包含 template/threshold/timeout/after_wait，这里只放各类型的专属字段
        field_specs = {
            "normal": (),
            "advanced": (),
            "keyboard_move": (("move_steps", "移动步骤(每行: 按键 时长秒)", "move_steps"), ("delay_before", "执行前延时", "float"), ("after_wait", "执行后等待(秒)", "float")),
            "key_press": (("key", "按键", "text"), ("delay_before", "执行前延时", "float"), ("hold_time", "持续时间", "float"), ("after_wait", "执行后等待(秒)", "float")),
            "drag": (("start_x", "起点 X", "float"), ("start_y", "起点 Y", "float"), ("end_x", "终点 X", "float"), ("end_y", "终点 Y", "float"), ("duration", "拖曳时间", "float"), ("after_wait", "执行后等待(秒)", "float")),
            "delay": (("duration", "延迟时间", "float"),),
            "condition": (("condition_templates", "条件模板(逗号分隔)", "templates"), ("condition_operator", "条件运算", "text"), ("condition_true_jump_to", "成立跳转步骤号", "int"), ("condition_false_jump_to", "不成立跳转步骤号", "int"), ("condition_invert", "反转结果", "bool"), ("threshold", "匹配阈值(0-1)", "float")),
            "switch": (("switch_value", "选择值", "text"), ("switch_cases", "分支(值:步骤号)", "cases"), ("switch_default_jump_to", "默认步骤号", "int")),
            "loop": (("loop_count", "循环次数", "int"), ("loop_target", "循环体步骤号", "int"), ("loop_exit_target", "退出步骤号", "int")),
            "event": (("event_template", "事件模板", "text"), ("event_timeout", "等待超时(秒)", "float"), ("event_timeout_target", "超时跳转步骤号", "int"), ("threshold", "匹配阈值(0-1)", "float")),
        }
        for key, label, kind in field_specs.get(str(task.get("type", "normal")), ()):
            if key == "condition_templates":
                value = task.get("condition_templates")
                if isinstance(value, (list, tuple)):
                    value = ", ".join(str(item) for item in value)
                else:
                    value = str(value or task.get("condition_template") or "")
                editor = QLineEdit(value)
            elif key == "switch_cases":
                value = ", ".join(f"{k}:{v}" for k, v in (task.get("switch_cases") or {}).items())
                editor = QLineEdit(value)
            elif key == "move_steps":
                steps = task.get("move_steps") or []
                value = "\n".join(f"{step.get('key', 'W')} {step.get('duration', 1.0)}" for step in steps if isinstance(step, dict))
                editor = QPlainTextEdit()
                editor.setPlaceholderText("每行一个：按键 时长(秒)，例如 W 1.2")
                editor.setMaximumHeight(120)
                editor.setPlainText(value)
            elif kind == "bool":
                editor = QCheckBox()
                editor.setChecked(bool(task.get(key, False)))
            else:
                editor = QLineEdit(str(task.get(key, "")))
            self.special_edits[key] = editor
            self.special_form.addRow(label + ":", editor)
        self.special_group.setVisible(bool(self.special_edits) and str(task.get("type", "normal")) != "click_until_gone")

    def clear_editor(self):
        self.selected_label.setText("未选择步骤")
        self.template_preview.setPixmap(QPixmap())
        self.template_preview.setText("无模板预览")
        self.template_preview.setVisible(False)
        self.editor_actions.setVisible(False)
        for editor in (self.description_edit, self.template_edit, self.threshold_edit, self.timeout_edit, self.after_wait_edit):
            editor.clear()
        self.click_until_template_edit.clear()
        self.click_until_interval_edit.clear()
        self.click_until_stop_delay_edit.clear()
        self.click_until_timeout_edit.clear()
        self.click_until_continue_checkbox.setChecked(False)
        self.click_until_stop_on_change_checkbox.setChecked(False)
        self.click_checkbox.setChecked(False)
        self.match_required_checkbox.setChecked(False)
        self.enabled_checkbox.setChecked(False)
        self.offset_x_edit.clear()
        self.offset_y_edit.clear()
        self.click_x_edit.clear()
        self.click_y_edit.clear()
        self.match_rect_edit.clear()
        for editor in (self.region_left_edit, self.region_top_edit, self.region_right_edit, self.region_bottom_edit, self.region_center_x_edit, self.region_center_y_edit):
            editor.clear()
        self.next_template_edit.clear()
        self.wait_for_combo.setCurrentIndex(0)
        self._rebuild_special_form({})
        self.recognition_group.setVisible(False)
        self.group_form.setVisible(False)
        self.current_group_id = None

    def _update_template_preview(self, templates):
        self.template_preview.setPixmap(QPixmap())
        self.template_preview.setText("")
        self.template_preview.setVisible(False)

    @Slot()
    def select_template_file(self):
        if not 0 <= self.current_task_index < len(TASKS):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("绑定图片")
        dialog.resize(420, 440)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("绑定图片操作"))

        image_list = QListWidget()
        layout.addWidget(image_list, 1)

        current = self.template_edit.text()
        if TASKS[self.current_task_index].get("type") == "click_until_gone":
            current = self.click_until_template_edit.text()
        for name in [item.strip() for item in current.replace("，", ",").split(",") if item.strip()]:
            image_list.addItem(name)

        def choose_images():
            paths, _ = QFileDialog.getOpenFileNames(dialog, "选择要绑定的图片", config.ICON_DIR, "PNG 图片 (*.png)")
            if not paths:
                return
            existing = [image_list.item(i).text() for i in range(image_list.count())]
            for path in paths:
                name = os.path.splitext(os.path.basename(path))[0]
                if name not in existing:
                    image_list.addItem(name)
                    existing.append(name)

        def capture_image():
            dialog.hide()

            def on_captured(result):
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                if result[0] == "image":
                    name = result[1]
                    existing = [image_list.item(i).text() for i in range(image_list.count())]
                    if name not in existing:
                        image_list.addItem(name)

            self._begin_dialog_capture("image", on_captured)

        def preview_image():
            row = image_list.currentRow()
            if row < 0:
                return
            name = image_list.item(row).text()
            matches = glob.glob(os.path.join(config.ICON_DIR, f"{name}.*"))
            if matches:
                os.startfile(matches[0])
            else:
                QMessageBox.information(dialog, "预览绑定图片", "当前步骤没有找到可预览的绑定图片。")

        def remove_image():
            row = image_list.currentRow()
            if row >= 0:
                image_list.takeItem(row)

        buttons_row = QHBoxLayout()
        choose_btn = QPushButton("选择图片")
        choose_btn.clicked.connect(choose_images)
        capture_btn = QPushButton("手动框选图片")
        capture_btn.clicked.connect(capture_image)
        preview_btn = QPushButton("预览绑定图片")
        preview_btn.clicked.connect(preview_image)
        remove_btn = QPushButton("删除选中图片")
        remove_btn.clicked.connect(remove_image)
        buttons_row.addWidget(choose_btn)
        buttons_row.addWidget(capture_btn)
        buttons_row.addWidget(preview_btn)
        buttons_row.addWidget(remove_btn)
        layout.addLayout(buttons_row)

        def save():
            values = [image_list.item(i).text() for i in range(image_list.count())]
            template_value = ", ".join(values)
            if TASKS[self.current_task_index].get("type") == "click_until_gone":
                self.click_until_template_edit.setText(template_value)
            else:
                self.template_edit.setText(template_value)
                self._update_template_preview(template_value)
            dialog.accept()

        save_btn = QPushButton("保存")
        save_btn.clicked.connect(save)
        layout.addWidget(save_btn)
        dialog.exec()

    @Slot()
    def select_next_template_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择等待出现的目标图片", config.ICON_DIR, "PNG 图片 (*.png)")
        if path:
            self.next_template_edit.setText(os.path.splitext(os.path.basename(path))[0])
            self.wait_for_combo.setCurrentText("2. 等待目标模板出现")

    @Slot()
    def start_next_template_capture(self):
        if self.current_task_index >= 0:
            self._capture_target = "next_template"
            self._show_capture_overlay("image")

    @Slot()
    def start_next_region_capture(self):
        if self.current_task_index >= 0:
            if not self.next_template_edit.text().strip():
                self.append_log("请先选择下一模板图片，再框选它的出现位置。")
                return
            self._capture_target = "next"
            self._show_capture_overlay("region")

    @Slot()
    def clear_match_region(self):
        self.match_rect_edit.clear()
        for editor in (self.region_left_edit, self.region_top_edit, self.region_right_edit, self.region_bottom_edit, self.region_center_x_edit, self.region_center_y_edit):
            editor.clear()
        if not 0 <= self.current_task_index < len(TASKS):
            return
        task = TASKS[self.current_task_index]
        for key in ("match_rects", "match_rect", "search_rect"):
            task.pop(key, None)
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)
        self.append_log("已清空当前步骤的全部识别区域")

    def _current_match_rect(self):
        values = [self._int_value(editor.text()) for editor in (self.region_left_edit, self.region_top_edit, self.region_right_edit, self.region_bottom_edit)]
        if all(value is not None for value in values) and values[2] > values[0] and values[3] > values[1]:
            return tuple(values)
        return self._parse_rect(self.match_rect_edit.text())

    @Slot()
    def choose_group_color(self):
        selected_group_ids = self._selected_group_ids()
        if selected_group_ids:
            group_id = selected_group_ids[0]
        elif 0 <= self.current_task_index < len(TASKS):
            group_id = str(TASKS[self.current_task_index].get("group_id") or "group_default")
        else:
            return
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        colors = metadata.setdefault("colors", {})
        current = QColor(colors.get(group_id, "#e0f2fe"))
        color = QColorDialog.getColor(current, self, "选择分组颜色")
        if not color.isValid():
            return
        colors[group_id] = color.name()
        for task in TASKS:
            if str(task.get("group_id") or "group_default") == group_id:
                task["group_color"] = color.name()
        self._save_presets()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)

    def _capture_rect(self):
        if config.USE_WINDOW_MODE and config.TARGET_WINDOW_TITLE:
            return get_window_rect()
        screen = QApplication.primaryScreen().geometry()
        return {"left": screen.left(), "top": screen.top(), "width": screen.width(), "height": screen.height()}

    def _show_capture_overlay(self, mode):
        if self.capture_overlay is not None:
            self.capture_overlay.close()
        self.capture_overlay = CaptureOverlay(self._capture_rect(), mode)
        self.capture_overlay.clicked.connect(self.finish_click_capture)
        self.capture_overlay.region_selected.connect(self.finish_region_capture)
        self.capture_overlay.image_selected.connect(self.finish_image_capture)
        self.capture_overlay.cancelled.connect(self.cancel_capture)
        self.capture_overlay.too_small.connect(self.finish_too_small)
        self.capture_overlay.destroyed.connect(self.clear_capture_overlay)
        self.append_log("请在覆盖层中选择位置，按 Esc 取消。")
        self.capture_overlay.start()
        self.hide()

    @Slot()
    def start_click_capture(self):
        if self.current_task_index >= 0:
            self._capture_callback = None
            self._show_capture_overlay("click")

    @Slot()
    def start_region_capture(self):
        if self.current_task_index >= 0:
            self._capture_callback = None
            self._capture_target = "match"
            self._show_capture_overlay("region")

    def _begin_dialog_capture(self, mode, callback):
        self._capture_callback = callback
        self._capture_target = "match"
        self._show_capture_overlay(mode)

    @Slot(int, int)
    def finish_click_capture(self, x, y):
        self.clear_capture_overlay()
        self.activateWindow()
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        if callback is not None:
            callback(("click", x, y))
            return
        self.click_x_edit.setText(str(x))
        self.click_y_edit.setText(str(y))
        if not 0 <= self.current_task_index < len(TASKS):
            return
        task = TASKS[self.current_task_index]
        task["click_x"] = int(x)
        task["click_y"] = int(y)
        task["click_position"] = (int(x), int(y))
        if task.get("type") != "click_until_gone":
            task.pop("match_rect", None)
            task.pop("search_rect", None)
        self.save_current_tasks()
        self.append_log(f"已记录备用点击坐标: ({x}, {y})")

    @Slot(int, int, int, int)
    def finish_region_capture(self, left, top, right, bottom):
        self.clear_capture_overlay()
        self.activateWindow()
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        if callback is not None:
            callback(("region", (left, top, right, bottom)))
            return
        if not 0 <= self.current_task_index < len(TASKS):
            return
        task = TASKS[self.current_task_index]
        if getattr(self, "_capture_target", "match") == "next":
            task["next_match_rect"] = (left, top, right, bottom)
            task["next_search_rect"] = (left, top, right, bottom)
            self.save_current_tasks()
            self.append_log(f"已记录下一模板识别区域: 左上=({left}, {top}) 右下=({right}, {bottom})")
            return
        match_rects = task.setdefault("match_rects", [])
        if not isinstance(match_rects, list):
            match_rects = []
            task["match_rects"] = match_rects
        match_rects.append((left, top, right, bottom))
        task["match_rect"] = match_rects[0]
        task["search_rect"] = match_rects[0]
        self.match_rect_edit.setText(f"{left}, {top}, {right}, {bottom}")
        self.region_left_edit.setText(str(left))
        self.region_top_edit.setText(str(top))
        self.region_right_edit.setText(str(right))
        self.region_bottom_edit.setText(str(bottom))
        self.region_center_x_edit.setText(str((left + right) // 2))
        self.region_center_y_edit.setText(str((top + bottom) // 2))
        self.save_current_tasks()
        self.append_log(f"已追加第 {len(match_rects)} 段识别区域")

    @Slot(int, int, int, int)
    def finish_image_capture(self, left, top, right, bottom):
        self.clear_capture_overlay()
        self.activateWindow()
        if right <= left or bottom <= top:
            QMessageBox.warning(self, "框选失败", "框选区域太小，请重新拖曳选择。")
            return
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        QTimer.singleShot(120, lambda: self._save_captured_image(left, top, right, bottom, callback))

    def _save_captured_image(self, left, top, right, bottom, callback=None):
        icons_dir = os.path.join(os.path.dirname(__file__), "icons")
        os.makedirs(icons_dir, exist_ok=True)
        image_name = f"captured_{uuid.uuid4().hex[:10]}"
        image_path = os.path.join(icons_dir, f"{image_name}.png")
        screen = QApplication.primaryScreen()
        pixmap = screen.grabWindow(0, left, top, right - left, bottom - top)
        if pixmap.isNull() or not pixmap.save(image_path, "PNG"):
            QMessageBox.warning(self, "保存失败", "无法保存框选图片。")
            return
        reload_templates()
        if callback is not None:
            callback(("image", image_name))
            return
        if not 0 <= self.current_task_index < len(TASKS):
            return
        task = TASKS[self.current_task_index]
        task["next_template"] = image_name
        task["next_templates"] = [image_name]
        task["wait_for"] = "next_appear"
        self.next_template_edit.setText(image_name)
        self.wait_for_combo.setCurrentText("2. 等待目标模板出现")
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)
        self.append_log(f"已将框选图片保存并绑定: {image_path}")

    @Slot()
    def cancel_capture(self):
        self.clear_capture_overlay()
        self.activateWindow()
        self.append_log("已取消屏幕采集。")

    @Slot()
    def finish_too_small(self):
        self.clear_capture_overlay()
        self.activateWindow()
        QMessageBox.warning(self, "框选失败", "框选区域太小，请重新拖曳选择。")

    @Slot()
    def clear_capture_overlay(self):
        if self.capture_overlay is not None:
            overlay = self.capture_overlay
            self.capture_overlay = None
            overlay.close()
        self.show()

    @Slot(QTreeWidgetItem, int)
    def toggle_item(self, item, _column):
        index = self._task_index_from_item(item)
        if index is None:
            return
        if 0 <= index < len(TASKS):
            TASKS[index]["enabled"] = not bool(TASKS[index].get("enabled", True))
            self.refresh_task_list()
            self.save_current_tasks()

    @Slot()
    def toggle_selected_tasks(self):
        rows = sorted({index for item in self.task_list.selectedItems() if (index := self._task_index_from_item(item)) is not None})
        if not rows:
            return
        should_enable = not all(bool(TASKS[row].get("enabled", True)) for row in rows)
        for row in rows:
            TASKS[row]["enabled"] = should_enable
        self.refresh_task_list()
        self.save_current_tasks()
        self.append_log(f"已切换 {len(rows)} 个选中步骤的启用状态。")

    @Slot(QTreeWidgetItem, int)
    def on_item_changed(self, item, _column):
        index = self._task_index_from_item(item)
        if index is not None and 0 <= index < len(TASKS):
            TASKS[index]["enabled"] = item.checkState(0) == Qt.CheckState.Checked
            parent = item.parent()
            if parent is not None:
                self.task_list.blockSignals(True)
                states = [parent.child(i).checkState(0) == Qt.CheckState.Checked for i in range(parent.childCount())]
                if all(states):
                    parent.setCheckState(0, Qt.CheckState.Checked)
                elif any(states):
                    parent.setCheckState(0, Qt.CheckState.PartiallyChecked)
                else:
                    parent.setCheckState(0, Qt.CheckState.Unchecked)
                self.task_list.blockSignals(False)
            return
        if item.data(0, Qt.UserRole) == "group":
            group_id = str(item.data(0, Qt.UserRole + 1))
            checked = item.checkState(0) == Qt.CheckState.Checked
            for task in TASKS:
                if str(task.get("group_id") or "group_default") == group_id:
                    task["enabled"] = checked
            self.save_current_tasks()
            self.refresh_task_list()

    @Slot()
    def persist_task_order(self):
        ordered_ids = []
        for group_index in range(self.task_list.topLevelItemCount()):
            group = self.task_list.topLevelItem(group_index)
            for child_index in range(group.childCount()):
                ordered_ids.append(str(group.child(child_index).data(0, Qt.UserRole)))
        task_by_id = {str(task.get("id", index)): task for index, task in enumerate(TASKS)}
        TASKS[:] = [task_by_id[task_id] for task_id in ordered_ids if task_id in task_by_id]
        self.current_task_index = -1
        self.save_current_tasks()
        self.refresh_task_list()

    @Slot(int, int, bool)
    def reorder_task_from_tree(self, source_index, target_index, insert_after):
        if source_index == target_index or not (0 <= source_index < len(TASKS)) or not (0 <= target_index < len(TASKS)):
            return
        task = TASKS.pop(source_index)
        if source_index < target_index:
            target_index -= 1
        insert_index = target_index + (1 if insert_after else 0)
        insert_index = max(0, min(insert_index, len(TASKS)))
        TASKS.insert(insert_index, task)
        self.current_task_index = insert_index
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(insert_index)

    @Slot(int)
    def move_task_to_end(self, source_index):
        if not (0 <= source_index < len(TASKS)):
            return
        task = TASKS.pop(source_index)
        TASKS.append(task)
        self.current_task_index = len(TASKS) - 1
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)

    def _selected_task_indices(self):
        return sorted({index for item in self.task_list.selectedItems() if (index := self._task_index_from_item(item)) is not None})

    def _selected_group_ids(self):
        ids = []
        for item in self.task_list.selectedItems():
            if item.data(0, Qt.UserRole) == "group":
                group_id = item.data(0, Qt.UserRole + 1)
                if group_id:
                    ids.append(str(group_id))
        return ids

    def _group_descendants(self, metadata, group_id):
        children = metadata.get("children", {}) if isinstance(metadata, dict) else {}
        out = [str(group_id)]
        for child in children.get(str(group_id), []):
            out.extend(self._group_descendants(metadata, str(child)))
        return out

    @Slot()
    def select_all_tasks(self):
        for task in TASKS:
            task["enabled"] = True
        self.save_current_tasks()
        self.refresh_task_list()
        self.append_log("已启用全部步骤。")

    @Slot()
    def clear_tasks(self):
        for task in TASKS:
            task["enabled"] = False
        self.save_current_tasks()
        self.refresh_task_list()
        self.append_log("已停用全部步骤。")

    @Slot()
    def move_selected_item(self, direction):
        indices = self._selected_task_indices()
        if not indices:
            return
        index = indices[0]
        target = index + direction
        if not (0 <= target < len(TASKS)):
            return
        task = TASKS.pop(index)
        TASKS.insert(target, task)
        self.current_task_index = target
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(target)

    @Slot()
    def add_group(self):
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        group_id = f"group_{uuid.uuid4().hex[:8]}"
        metadata.setdefault("names", {})[group_id] = f"组 {len(metadata.get('names', {})) + 1}"
        metadata.setdefault("colors", {})[group_id] = "#eab308"
        metadata.setdefault("parents", {})[group_id] = None
        metadata.setdefault("children", {})[group_id] = []
        metadata.setdefault("order", []).append(group_id)
        self._save_presets()
        self.refresh_task_list()
        self.append_log(f"已新增组: {metadata['names'][group_id]}")

    @Slot()
    def add_task(self):
        types = ["normal", "advanced", "loop", "keyboard_move", "key_press", "drag", "click_until_gone", "delay"]
        task_type, ok = QInputDialog.getItem(self, "新增步骤类型", "请选择步骤类型:", types, 0, False)
        if not ok:
            return
        group_ids = self._selected_group_ids()
        group_id = group_ids[0] if group_ids else None
        mode = self.mode_combo.currentText() or "custom"
        new_task = {
            "id": str(uuid.uuid4()),
            "type": task_type,
            "mode": mode,
            "enabled": True,
            "template": "new_step" if task_type in ("normal", "advanced") else "",
            "description": f"新增{task_type}步骤",
            "click": task_type in ("normal", "advanced", "click_until_gone"),
            "required": True,
        }
        if group_id:
            new_task["group_id"] = str(group_id)
            new_task["group_name"] = self.mode_group_metadata.get(mode, {}).get("names", {}).get(str(group_id), "默认分组")
        TASKS.append(new_task)
        self.current_task_index = len(TASKS) - 1
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)
        self.append_log(f"已新增步骤: {new_task['description']}")

    @Slot()
    def copy_selected_item(self):
        indices = self._selected_task_indices()
        if not indices:
            return
        index = indices[-1]
        cloned = deepcopy(TASKS[index])
        cloned["id"] = str(uuid.uuid4())
        cloned["description"] = f"{cloned.get('description', '步骤')} 副本"
        cloned.pop("flow_next", None)
        cloned.pop("flow_next_disabled", None)
        TASKS.insert(index + 1, cloned)
        self.current_task_index = index + 1
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)
        self.append_log(f"已复制步骤: {cloned['description']}")

    @Slot()
    def delete_selected_item(self):
        indices = set(self._selected_task_indices())
        selected_group_ids = self._selected_group_ids()
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        all_group_ids = set()
        for gid in selected_group_ids:
            all_group_ids.update(self._group_descendants(metadata, gid))
        remove_indices = set(indices)
        if all_group_ids:
            remove_indices.update(i for i, task in enumerate(TASKS) if str(task.get("group_id") or "group_default") in all_group_ids)
        if not remove_indices and not selected_group_ids:
            return
        group_count = len(all_group_ids)
        if group_count:
            if remove_indices:
                prompt = f"确定删除选中的 {len(remove_indices)} 个步骤和 {group_count} 个组吗？"
            else:
                prompt = f"确定删除选中的 {group_count} 个组吗？"
        else:
            prompt = f"确定删除选中的 {len(remove_indices)} 个步骤吗？"
        if QMessageBox.question(self, "确认删除", prompt) != QMessageBox.Yes:
            return
        old_tasks = list(TASKS)
        removed = set(remove_indices)
        TASKS[:] = [task for i, task in enumerate(old_tasks) if i not in removed]
        for task in TASKS:
            flow_target = task.get("flow_next")
            if flow_target is not None and not any(str(candidate.get("id")) == str(flow_target) for candidate in TASKS):
                task.pop("flow_next", None)
            for field in ("condition_true_jump_to", "condition_false_jump_to", "detour_success_jump_to", "detour_jump_to", "timeout_jump_to", "event_timeout_target", "loop_target", "loop_exit_target", "event_trigger_target", "switch_default_jump_to"):
                value = task.get(field)
                try:
                    target_index = int(value) - 1
                except (TypeError, ValueError):
                    continue
                if target_index in removed:
                    task.pop(field, None)
                else:
                    shift = sum(1 for r in removed if r < target_index)
                    task[field] = target_index - shift + 1
            cases = task.get("switch_cases")
            if isinstance(cases, dict):
                for case, value in list(cases.items()):
                    try:
                        target_index = int(value) - 1
                    except (TypeError, ValueError):
                        continue
                    if target_index in removed:
                        cases.pop(case, None)
                    else:
                        shift = sum(1 for r in removed if r < target_index)
                        cases[case] = target_index - shift + 1
        for gid in all_group_ids:
            metadata.get("names", {}).pop(gid, None)
            metadata.get("colors", {}).pop(gid, None)
            metadata.get("parents", {}).pop(gid, None)
            metadata.get("children", {}).pop(gid, None)
            order = metadata.get("order", [])
            if gid in order:
                order.remove(gid)
        self.current_task_index = -1
        self.save_current_tasks()
        self._save_presets()
        self.refresh_task_list()
        if group_count:
            self.append_log(f"已删除 {len(remove_indices)} 个步骤和 {group_count} 个组。")
        else:
            self.append_log(f"已删除 {len(remove_indices)} 个步骤。")

    @Slot()
    def apply_selected_task(self):
        if not 0 <= self.current_task_index < len(TASKS):
            return
        task = TASKS[self.current_task_index]
        task["description"] = self.description_edit.text().strip()
        task["enabled"] = self.enabled_checkbox.isChecked()
        if task.get("type", "normal") in ("normal", "advanced", "click_until_gone"):
            if task.get("type") == "click_until_gone":
                template_value = self.click_until_template_edit.text().strip() or "new_step"
                templates = [item.strip() for item in template_value.replace("，", ",").split(",") if item.strip()]
                task["templates"] = templates or ["new_step"]
                task["template"] = task["templates"][0]
                task["click_interval"] = self._float_value(self.click_until_interval_edit.text(), 0.5)
                task["stop_delay"] = self._float_value(self.click_until_stop_delay_edit.text(), 0.0)
                task["timeout"] = self._float_value(self.click_until_timeout_edit.text(), 30.0)
                task["continue_after_timeout"] = self.click_until_continue_checkbox.isChecked()
                task["stop_on_change"] = self.click_until_stop_on_change_checkbox.isChecked()
                task["click"] = True
                task["required"] = True
            else:
                template_value = self.template_edit.text().strip() or "new_step"
                if task.get("type") == "advanced":
                    templates = [item.strip() for item in template_value.replace("，", ",").split(",") if item.strip()]
                    task["templates"] = templates or ["new_step"]
                    task["template"] = task["templates"][0]
                else:
                    task["template"] = template_value
                task["threshold"] = self._float_value(self.threshold_edit.text(), config.THRESHOLD)
                task["timeout"] = self._float_value(self.timeout_edit.text(), 5.0)
                task["after_wait"] = self._float_value(self.after_wait_edit.text(), 0.25)
                task["click"] = self.click_checkbox.isChecked()
                task["click_requires_match"] = self.match_required_checkbox.isChecked()
                task["offset_x"] = self._float_value(self.offset_x_edit.text(), 0.0)
                task["offset_y"] = self._float_value(self.offset_y_edit.text(), 0.0)
                task["offset"] = (task["offset_x"], task["offset_y"])
                click_x = self._int_value(self.click_x_edit.text())
                click_y = self._int_value(self.click_y_edit.text())
                if click_x is not None and click_y is not None:
                    task["click_x"] = click_x
                    task["click_y"] = click_y
                    task["click_position"] = (click_x, click_y)
                else:
                    task.pop("click_x", None)
                    task.pop("click_y", None)
                    task.pop("click_position", None)
                match_rect = self._current_match_rect()
                if match_rect is not None:
                    task["match_rect"] = match_rect
                    task["search_rect"] = match_rect
                    task["match_rects"] = [match_rect]
                else:
                    task.pop("match_rect", None)
                    task.pop("search_rect", None)
                    task.pop("match_rects", None)
                self.match_rect_edit.setText(", ".join(str(value) for value in match_rect) if match_rect else "")
                next_template = self.next_template_edit.text().strip()
                if next_template:
                    task["next_template"] = next_template
                    task["next_templates"] = [next_template]
                else:
                    task.pop("next_template", None)
                    task.pop("next_templates", None)
                wait_mode = self.wait_for_combo.currentText()
                task["wait_for"] = "next_appear" if wait_mode.startswith("2") else "change_then_appear" if wait_mode.startswith("3") else "time"
        task["enabled"] = self.enabled_checkbox.isChecked()
        for key, editor in self.special_edits.items():
            if key == "move_steps":
                steps = []
                for line in editor.toPlainText().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    step_key = parts[0]
                    try:
                        duration = float(parts[1].rstrip("sS")) if len(parts) > 1 else 1.0
                    except ValueError:
                        duration = 1.0
                    steps.append({"key": step_key, "duration": duration})
                task["move_steps"] = steps
                continue
            if key in {"condition_invert", "continue_after_timeout", "stop_on_change"}:
                task[key] = editor.isChecked()
                continue
            value = editor.text().strip()
            if key == "condition_templates":
                values = [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
                task["condition_templates"] = values
                task["condition_template"] = values[0] if values else ""
                if values:
                    task["template"] = values[0]
                continue
            if key == "switch_cases":
                cases = {}
                for item in value.replace("，", ",").split(","):
                    if ":" not in item:
                        continue
                    case_value, target = item.split(":", 1)
                    if case_value.strip() and target.strip():
                        try:
                            cases[case_value.strip()] = int(target.strip())
                        except ValueError:
                            continue
                task["switch_cases"] = cases
                continue
            if key in {"condition_true_jump_to", "condition_false_jump_to", "switch_default_jump_to", "loop_count", "loop_target", "loop_exit_target", "event_timeout_target"}:
                if value == "":
                    task.pop(key, None)
                else:
                    parsed = self._int_value(value)
                    if parsed is not None:
                        task[key] = parsed
            elif key in {"wait_timeout", "offset_x", "offset_y", "delay_before", "hold_time", "start_x", "start_y", "end_x", "end_y", "duration", "click_interval", "stop_delay", "event_timeout", "after_wait", "threshold"}:
                fallback = config.THRESHOLD if key == "threshold" else task.get(key, 0.0)
                task[key] = self._float_value(value, fallback)
            else:
                task[key] = value
        if task.get("type") == "event" and task.get("event_template"):
            task["template"] = task["event_template"]
        if task.get("type") == "key_press" and task.get("key"):
            task["template"] = task["key"]
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)
        self.append_log(f"已应用第 {self.current_task_index + 1} 步修改。")

    @Slot()
    def open_detour_editor(self):
        if not 0 <= self.current_task_index < len(TASKS):
            return
        task = TASKS[self.current_task_index]
        if task.get("type", "normal") not in ("normal", "advanced"):
            return

        detour_steps = task.setdefault("detour_steps", [])
        if not isinstance(detour_steps, list):
            detour_steps = []
            task["detour_steps"] = detour_steps

        dialog = QDialog(self)
        dialog.setWindowTitle("迂回设置")
        dialog.resize(560, 500)
        layout = QVBoxLayout(dialog)

        enabled_checkbox = QCheckBox("启用迂回")
        enabled_checkbox.setChecked(bool(task.get("detour_enabled", False)))
        layout.addWidget(enabled_checkbox)

        jump_options = ["不跳转"]
        jump_option_numbers = {}
        for task_index, main_task in enumerate(TASKS):
            description = main_task.get("description") or main_task.get("template") or main_task.get("type", "步骤")
            option = f"{task_index + 1}. {description}"
            jump_options.append(option)
            jump_option_numbers[option] = task_index + 1

        def jump_label(target_number):
            if target_number is None:
                return "不跳转"
            for option, option_number in jump_option_numbers.items():
                try:
                    if option_number == int(target_number):
                        return option
                except (TypeError, ValueError):
                    continue
            return "不跳转"

        jump_combo = QComboBox()
        jump_combo.addItems(jump_options)
        jump_combo.setCurrentText(jump_label(task.get("detour_jump_to")))
        success_jump_combo = QComboBox()
        success_jump_combo.addItems(jump_options)
        success_jump_combo.setCurrentText(jump_label(task.get("detour_success_jump_to")))

        jump_row = QHBoxLayout()
        jump_row.addWidget(QLabel("未识别时跳到:"))
        jump_row.addWidget(jump_combo, 1)
        layout.addLayout(jump_row)
        success_row = QHBoxLayout()
        success_row.addWidget(QLabel("识别成功后跳到:"))
        success_row.addWidget(success_jump_combo, 1)
        layout.addLayout(success_row)

        step_list = QListWidget()
        layout.addWidget(step_list, 1)

        def refresh_list():
            step_list.clear()
            for step in detour_steps:
                desc = step.get("description") or step.get("template") or step.get("type", "步骤")
                step_list.addItem(f"{step.get('type', 'normal')} - {desc}")

        refresh_list()

        add_row = QHBoxLayout()
        type_combo = QComboBox()
        type_combo.addItems(["normal", "advanced", "loop", "key_press", "keyboard_move", "drag", "click_until_gone", "delay"])
        add_row.addWidget(type_combo, 1)
        add_button = QPushButton("新增步骤")
        add_button.clicked.connect(lambda: (detour_steps.append({"type": type_combo.currentText() or "normal"}), refresh_list()))
        add_row.addWidget(add_button)
        layout.addLayout(add_row)

        action_row = QHBoxLayout()
        config_button = QPushButton("设置")

        def configure_step():
            row = step_list.currentRow()
            if 0 <= row < len(detour_steps):
                self._configure_detour_step(detour_steps[row], dialog)
                refresh_list()

        config_button.clicked.connect(configure_step)
        action_row.addWidget(config_button)
        delete_button = QPushButton("删除")

        def delete_step():
            row = step_list.currentRow()
            if 0 <= row < len(detour_steps):
                detour_steps.pop(row)
                refresh_list()

        delete_button.clicked.connect(delete_step)
        action_row.addWidget(delete_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        def save():
            task["detour_enabled"] = enabled_checkbox.isChecked()
            task["detour_steps"] = detour_steps
            task["detour_jump_to"] = jump_option_numbers.get(jump_combo.currentText())
            task["detour_success_jump_to"] = jump_option_numbers.get(success_jump_combo.currentText())
            self.save_current_tasks()
            self.refresh_task_list()
            self.select_task_index(self.current_task_index)
            self.append_log("已保存迂回设置。")
            dialog.accept()

        save_button = QPushButton("保存迂回设置")
        save_button.clicked.connect(save)
        layout.addWidget(save_button)
        dialog.exec()

    def _configure_detour_step(self, detour_task, parent=None):
        dialog = QDialog(parent or self)
        dialog.setWindowTitle("迂回步骤设置")
        dialog.resize(460, 440)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()

        type_combo = QComboBox()
        type_combo.addItems(["normal", "advanced", "loop", "key_press", "keyboard_move", "drag", "click_until_gone", "delay"])
        type_combo.setCurrentText(str(detour_task.get("type", "normal")))
        form.addRow("类型:", type_combo)

        description_edit = QLineEdit(str(detour_task.get("description", "")))
        form.addRow("描述:", description_edit)

        def hide_dialogs():
            dialog.hide()
            if isinstance(parent, QDialog):
                parent.hide()

        def restore_dialogs():
            if isinstance(parent, QDialog):
                parent.show()
                parent.raise_()
                parent.activateWindow()
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        template_edit = QLineEdit(str(detour_task.get("template", "")))
        template_widget = QWidget()
        template_row = QHBoxLayout(template_widget)
        template_row.setContentsMargins(0, 0, 0, 0)
        template_row.addWidget(template_edit, 1)
        bind_button = QPushButton("绑定图片")

        def bind_image():
            path, _ = QFileDialog.getOpenFileName(dialog, "选择要绑定的图片", config.ICON_DIR, "PNG 图片 (*.png)")
            if path:
                template_edit.setText(os.path.splitext(os.path.basename(path))[0])

        bind_button.clicked.connect(bind_image)
        template_row.addWidget(bind_button)
        capture_template_button = QPushButton("手动框选")

        def capture_template():
            hide_dialogs()

            def on_captured(result):
                restore_dialogs()
                if result[0] == "image":
                    template_edit.setText(result[1])

            self._begin_dialog_capture("image", on_captured)

        capture_template_button.clicked.connect(capture_template)
        template_row.addWidget(capture_template_button)
        form.addRow("模板:", template_widget)

        click_x_edit = QLineEdit(str(detour_task.get("click_x", "")))
        click_y_edit = QLineEdit(str(detour_task.get("click_y", "")))
        click_widget = QWidget()
        click_row = QHBoxLayout(click_widget)
        click_row.setContentsMargins(0, 0, 0, 0)
        click_row.addWidget(QLabel("X"))
        click_row.addWidget(click_x_edit, 1)
        click_row.addWidget(QLabel("Y"))
        click_row.addWidget(click_y_edit, 1)
        click_capture_button = QPushButton("记录点击点")

        def capture_click():
            hide_dialogs()

            def on_captured(result):
                restore_dialogs()
                if result[0] == "click":
                    click_x_edit.setText(str(result[1]))
                    click_y_edit.setText(str(result[2]))

            self._begin_dialog_capture("click", on_captured)

        click_capture_button.clicked.connect(capture_click)
        click_row.addWidget(click_capture_button)
        form.addRow("点击坐标:", click_widget)

        duration_edit = QLineEdit(str(detour_task.get("duration", detour_task.get("hold_time", ""))))
        form.addRow("时长/按住(秒):", duration_edit)

        key_edit = QLineEdit(str(detour_task.get("key", "")))
        form.addRow("按键:", key_edit)

        match_rect_edit = QLineEdit(str(detour_task.get("match_rect", "") or ""))
        match_rect_widget = QWidget()
        match_rect_row = QHBoxLayout(match_rect_widget)
        match_rect_row.setContentsMargins(0, 0, 0, 0)
        match_rect_row.addWidget(match_rect_edit, 1)
        region_capture_button = QPushButton("框选识别区域")

        def capture_region():
            hide_dialogs()

            def on_captured(result):
                restore_dialogs()
                if result[0] == "region":
                    left, top, right, bottom = result[1]
                    match_rect_edit.setText(f"{left}, {top}, {right}, {bottom}")

            self._begin_dialog_capture("region", on_captured)

        region_capture_button.clicked.connect(capture_region)
        match_rect_row.addWidget(region_capture_button)
        form.addRow("识别区域(左上,右下):", match_rect_widget)

        move_steps_edit = QPlainTextEdit()
        move_steps_edit.setMaximumHeight(90)
        move_steps = detour_task.get("move_steps") or []
        move_steps_edit.setPlainText("\n".join(f"{step.get('key', 'W')} {step.get('duration', 1.0)}" for step in move_steps if isinstance(step, dict)))
        form.addRow("移动步骤(每行: 按键 时长):", move_steps_edit)

        layout.addLayout(form)

        def save():
            detour_task["type"] = type_combo.currentText()
            description = description_edit.text().strip()
            if description:
                detour_task["description"] = description
            template = template_edit.text().strip()
            if template:
                detour_task["template"] = template
            else:
                detour_task.pop("template", None)
            click_x = self._int_value(click_x_edit.text())
            click_y = self._int_value(click_y_edit.text())
            if click_x is not None and click_y is not None:
                detour_task["click_x"] = click_x
                detour_task["click_y"] = click_y
                detour_task["click_position"] = (click_x, click_y)
            else:
                detour_task.pop("click_x", None)
                detour_task.pop("click_y", None)
                detour_task.pop("click_position", None)
            duration = self._float_value(duration_edit.text(), 0.0)
            if duration:
                if detour_task.get("type") == "key_press":
                    detour_task["hold_time"] = duration
                else:
                    detour_task["duration"] = duration
            key = key_edit.text().strip()
            if key:
                detour_task["key"] = key
            rect = self._parse_rect(match_rect_edit.text())
            if rect is not None:
                detour_task["match_rect"] = rect
                detour_task["search_rect"] = rect
                detour_task["match_rects"] = [rect]
            else:
                detour_task.pop("match_rect", None)
                detour_task.pop("search_rect", None)
                detour_task.pop("match_rects", None)
            steps = []
            for line in move_steps_edit.toPlainText().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                step_key = parts[0]
                try:
                    step_duration = float(parts[1].rstrip("sS")) if len(parts) > 1 else 1.0
                except ValueError:
                    step_duration = 1.0
                steps.append({"key": step_key, "duration": step_duration})
            if steps:
                detour_task["move_steps"] = steps
            dialog.accept()

        save_button = QPushButton("保存")
        save_button.clicked.connect(save)
        layout.addWidget(save_button)
        dialog.exec()

    @staticmethod
    def _float_value(value, fallback):
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _int_value(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_rect(cls, value):
        parts = [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
        if len(parts) != 4:
            return None
        numbers = [cls._int_value(item) for item in parts]
        return tuple(numbers) if all(item is not None for item in numbers) and numbers[2] > numbers[0] and numbers[3] > numbers[1] else None

    @Slot()
    def save_current_tasks(self):
        mode_name = self.mode_combo.currentText() or "custom"
        self.mode_tasks[mode_name] = deepcopy(TASKS)
        if mode_name == "custom":
            save_tasks(TASKS)
        else:
            self._save_presets()
        self.append_log(f"任务已保存到预设“{mode_name}”。")

    @Slot()
    def refresh_window_list(self):
        titles = sorted({window.title.strip() for window in gw.getAllWindows() if window.title.strip()})
        current = self.window_combo.currentText()
        self.window_combo.clear()
        self.window_combo.addItems(titles)
        if current and current in titles:
            self.window_combo.setCurrentText(current)
        elif titles:
            self.window_combo.setCurrentIndex(0)
        self.append_log(f"检测到 {len(titles)} 个可见窗口。")

    @Slot()
    def open_blueprint(self):
        if self.blueprint_window is not None and self.blueprint_window.isVisible():
            self.blueprint_window.raise_()
            self.blueprint_window.activateWindow()
            return
        mode = self.mode_combo.currentText() or "custom"
        layout_data = deepcopy(self.blueprint_layouts.get(mode, {}))
        group_metadata = deepcopy(self.mode_group_metadata.get(mode, {}))
        self.blueprint_window = BlueprintWindow(
            deepcopy(TASKS),
            layout_data,
            lambda tasks, data, meta=None: self.save_blueprint_state(mode, tasks, data, meta),
            group_metadata,
            self,
            self.execution_states,
        )
        self.blueprint_window.show()

    def save_blueprint_state(self, mode, tasks, layout_data, group_metadata=None):
        self.mode_tasks[mode] = deepcopy(tasks)
        if group_metadata is not None:
            self.mode_group_metadata[mode] = deepcopy(group_metadata)
        if mode == (self.mode_combo.currentText() or "custom"):
            TASKS[:] = deepcopy(tasks)
        if mode == "custom":
            save_tasks(tasks)
        self.blueprint_layouts[mode] = deepcopy(layout_data)
        self.blueprint_graphs[mode] = NodeGraph(tasks).to_payload()
        save_blueprint_layouts(self.blueprint_layouts)
        save_blueprint_graphs(self.blueprint_graphs)
        self._save_presets()
        if mode == (self.mode_combo.currentText() or "custom"):
            self.refresh_task_list()
        self.append_log(f"已保存预设“{mode}”的蓝图布局。")

    def selected_tasks(self):
        return [deepcopy(task) for task in TASKS if task.get("enabled", True)]

    def _set_running_state(self, running):
        self.mode_combo.setEnabled(not running)
        self.task_list.setEnabled(not running)
        self.apply_button.setEnabled(not running)
        for button in self.preset_buttons:
            button.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.start_current_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.pause_button.setEnabled(running)
        self.step_button.setEnabled(running)

    @Slot()
    def start_script(self):
        self._start_worker(None)

    @Slot()
    def start_from_current(self):
        if 0 <= self.current_task_index < len(TASKS):
            self._start_worker(TASKS[self.current_task_index].get("id"))

    def _start_worker(self, start_node_id):
        if self.worker_thread and self.worker_thread.isRunning():
            return
        tasks = self.selected_tasks()
        if not tasks:
            QMessageBox.warning(self, "无法执行", "没有启用的任务步骤。")
            return
        self.execution_states.clear()
        if self.blueprint_window is not None:
            self.blueprint_window.reset_execution_states()
        window_title = self.window_combo.currentText().strip()
        config.TARGET_WINDOW_TITLE = window_title or None
        config.USE_WINDOW_MODE = bool(window_title)
        self.stop_event.clear()
        self.pause_event.clear()
        self.single_step_event.clear()
        self.completion_notified = False
        self._set_running_state(True)
        self.status_text = "运行中"
        self.status_label.setText("状态: 运行中")
        self.append_log(f"脚本启动，共 {len(tasks)} 个启用步骤。")

        self.worker_thread = QThread(self)
        self.worker = TaskWorker(tasks, self.loop_checkbox.isChecked(), self.stop_event, self.pause_event, self.single_step_event, start_node_id)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.execution_started.connect(self.on_execution_started)
        self.worker.execution_result.connect(self.on_execution_result)
        self.worker.completed.connect(self.on_execution_completed)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.on_worker_finished)
        self.worker_thread.start()

    @Slot()
    def stop_script(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.stop_event.set()
            self.pause_event.clear()
            self.single_step_event.set()
            self.status_label.setText("状态: 停止中")
            self.stop_button.setEnabled(False)
            self.append_log("正在停止脚本...")

    @Slot()
    def toggle_pause(self):
        if not self.worker_thread or not self.worker_thread.isRunning():
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_button.setText("暂停")
            self.status_label.setText("状态: 运行中")
        else:
            self.pause_event.set()
            self.pause_button.setText("继续")
            self.status_label.setText("状态: 已暂停")

    @Slot()
    def step_script(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.pause_event.set()
            self.single_step_event.set()
            self.pause_button.setText("继续")
            self.status_label.setText("状态: 单步执行")

    @Slot(dict)
    def on_execution_started(self, task):
        task_id = str(task.get("id"))
        self.execution_states[task_id] = "running"
        if self.blueprint_window is not None:
            self.blueprint_window.update_execution_state(task_id, "running")
        for index, item in enumerate(TASKS):
            if str(item.get("id")) == task_id:
                self.current_task_index = index
                self.select_task_index(index)
                break

    @Slot(dict, str)
    def on_execution_result(self, task, state):
        self.execution_states[str(task.get("id"))] = state
        if self.blueprint_window is not None:
            self.blueprint_window.update_execution_state(str(task.get("id")), state)
        self.append_log(f"步骤结果: {task.get('description', task.get('template', '未命名'))} -> {state}")

    @Slot(str)
    def on_execution_completed(self, state):
        if self.completion_notified:
            return
        self.completion_notified = True
        if state == "failed":
            self.status_text = "异常"
            self.status_label.setText("状态: 异常")
            QMessageBox.warning(self, "脚本异常", "脚本执行过程中发生错误，请查看日志。")
        elif self.stop_event.is_set():
            self.status_text = "已停止"
            self.status_label.setText("状态: 已停止")
        else:
            self.status_text = "已完成"
            self.status_label.setText("状态: 已完成")
            QMessageBox.information(self, "脚本执行完成", "所有步骤已完成，脚本已停止运行。")

    @Slot()
    def on_worker_finished(self):
        self._set_running_state(False)
        self.pause_button.setText("暂停")
        self.append_log("脚本执行结束。")
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker_thread = None
        self.worker = None

    def closeEvent(self, event):
        if self.worker_thread and self.worker_thread.isRunning():
            self.stop_event.set()
            self.pause_event.clear()
            self.single_step_event.set()
            if not self.worker_thread.wait(3000):
                QMessageBox.warning(self, "正在执行", "脚本线程尚未结束，请先停止脚本后再关闭窗口。")
                event.ignore()
                return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("VisionFlow Automator")
    window = PySide6ScriptWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
