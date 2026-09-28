# -*- coding: utf-8 -*-
"""蓝图窗口的画布交互：命中测试、选择、弯折点、连线几何、缩放、网格吸附、布局保存。

从 `gui_pyside6.py` 的 `BlueprintWindow` 整体外移出来的内聚职责块（24 个方法）。
窗口类通过 `BlueprintInteractionMixin` 混入，`self._select_node(i)` 之类的调用方式不变。

这些方法只依赖 `blueprint_canvas` 的三个图元类与 Qt 本身，不依赖任何项目模块，
因此外移风险低；行为等价性由画布操作基线（选择/命中/弯折/缩放/存档回调）保证。
"""
from PySide6.QtCore import QPointF, QRectF, QTimer, Slot
from PySide6.QtWidgets import QGraphicsTextItem

from blueprint_canvas import BendHandleItem, BlueprintGroupItem, BlueprintNodeItem


class BlueprintInteractionMixin:
    """画布交互相关方法；依赖宿主窗口提供 scene / view / tasks / layout_data 等。"""

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

    def _on_node_moved(self, node):
        positions = self.layout_data.setdefault("positions", {})
        positions[str(node.index)] = (node.x(), node.y())
        self._update_edges()
        self._sync_group_bounds()
        self._schedule_scene_rect_update()

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

    def save_layout(self):
        self.sync_positions()
        self.save_callback(self.tasks, self.layout_data, self.group_metadata)

    def _restore_saved_zoom(self):
        """把 layout_data["zoom"] 记录的比例应用到画布；非法值一律忽略。"""
        try:
            zoom = float(self.layout_data.get("zoom", 1.0))
        except (TypeError, ValueError):
            return
        # 与滚轮缩放一致，限制在合理区间，避免存档里的异常值让画布不可用
        if not 0.2 <= zoom <= 5.0 or abs(zoom - 1.0) < 1e-6:
            return
        self.view.scale(zoom, zoom)
        self.view.centerOn(0, 0)

    @Slot(float)
    def _on_zoom_changed(self, zoom):
        """滚轮缩放后立即记录比例，避免直接关窗时丢失。"""
        try:
            value = float(zoom)
        except (TypeError, ValueError):
            return
        if not 0.2 <= value <= 5.0:
            return
        if self.layout_data.get("zoom") == value:
            return
        self.layout_data["zoom"] = value
        self.save_layout()

    def set_grid_snap(self, enabled):
        self.grid_snap = enabled
        self.sync_positions()
        self.refresh()

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
