# -*- coding: utf-8 -*-
"""蓝图画布的 QGraphics 组件（节点项、连线、弯折手柄、场景、分组、视图）。

从 gui_pyside6.py 外移出来：这 6 个类只依赖 Qt，不依赖任何窗口类，
是外移风险最低的一块。节点颜色等常量原本就是类级属性，随类一起搬走。

名字在 gui_pyside6 里重新导出，`gui_pyside6.BlueprintNodeItem` 等用法不变。
"""
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
)

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
        """每种步骤类型一种颜色，便于在蓝图里一眼区分（节点头部同时显示类型名）。

        此前 `click_until_gone` 与 `delay` 没登记，会和"未知类型"一起落到灰色，
        而它们的头部文字又要仔细看才认得出——所有者 2026-09-28 要求按类型区分颜色。
        这里保证 6 种实际用到的类型（normal / advanced / click_until_gone /
        keyboard_move / key_press / drag）色相互相拉开。
        """
        return {
            "loop": "#ef4444",              # 0°   红
            "advanced": "#d97706",          # 32°  琥珀
            "delay": "#65a30d",             # 85°  黄绿
            "keyboard_move": "#16a34a",     # 142° 绿
            "switch": "#14b8a6",            # 173° 青绿
            "click_until_gone": "#0e7490",  # 192° 青
            "normal": "#2563eb",            # 221° 蓝
            "condition": "#4f46e5",         # 243° 靛
            "key_press": "#7c3aed",         # 262° 紫罗兰
            "event": "#c026d3",             # 292° 品红紫
            "drag": "#db2777",              # 333° 品红
        }.get(task_type, "#475569")         # 未知类型保持灰

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
