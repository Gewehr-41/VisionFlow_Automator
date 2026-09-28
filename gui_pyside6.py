# PySide6 主界面：新版任务编辑器、蓝图编辑器和线程化执行控制。
import os
import sys
import threading
import uuid
from copy import deepcopy

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")

import pygetwindow as gw
from PySide6.QtCore import QThread, QTimer, Signal, Slot, Qt
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPixmap, QBrush
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QColorDialog,
    # QDialog 在本模块内已无人直接使用，但 tests/ 用
    # `mock.patch.object(g.QDialog, "exec", ...)` 拦对话框，必须保留（见下面那条守卫测试）
    QDialog,
    QFormLayout,
    QGroupBox,
    QGridLayout,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import config
import paths
from main import reload_templates
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
from nodes import JUMP_FIELD_LABELS, NodeGraph, fill_missing_connections, remap_jump_targets
# ---- 已外移的模块（这里重新导出，保证 gui_pyside6.<名字> 的既有用法不变）----
# 说明：_load_template_pixmap 在 gui_pyside6 内部已无人使用，但为保持
# `gui_pyside6.<名字>` 这一层兼容面完整，仍然一并导出。
# ---- 已外移的模块（这里重新导出，保证 gui_pyside6.<名字> 的既有用法不变）----
from capture_overlay import CaptureOverlay, CaptureOverlayMixin
from blueprint_groups import BlueprintGroupMixin
from blueprint_interaction import BlueprintInteractionMixin
from blueprint_canvas import (
    BendHandleItem,
    BlueprintGroupItem,
    BlueprintNodeItem,
    BlueprintScene,
    BlueprintView,
    BlueprintWireItem,
)
from task_widgets import TaskItemDelegate, TaskListWidget, TaskWorker
from preset_manager import PresetManagerMixin
from execution_control import ExecutionControlMixin
from task_list_manager import TaskListMixin
from ui_common import (
    _add_click_until_row,
    _add_coordinate_row,
    _add_form_row,
    _add_region_selector_row,
    _capture_rect,
    _first_available_template_pixmap,
    _jump_index_map,
    _load_template_pixmap,
    _to_float,
    _to_int,
)











class BlueprintWindow(BlueprintGroupMixin, BlueprintInteractionMixin,
                     CaptureOverlayMixin, QMainWindow):
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
        # 正在编辑的识别区域下标（一个步骤可以有多条区域）与它所属步骤的标识
        self._region_index = 0
        self._region_task_key = None
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
        # zoom_changed 此前从未 connect；接上它才能在滚轮缩放后立即持久化
        self.view.zoom_changed.connect(self._on_zoom_changed)
        # 恢复上次的缩放比例。此前 zoom 只写不读（只在 sync_positions 里写入
        # layout_data["zoom"]，打开时从不应用），导致每次重开蓝图都回到 100%。
        self._restore_saved_zoom()

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
        """面板控件全部由 mixin 的共享构造器建（见 capture_overlay.py）。"""
        self._build_shared_editor_panel(self.editor_layout)


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

    # ---------- CaptureOverlayMixin 的钩子实现 ----------
    # 采集管线的共享实现在 CaptureOverlayMixin 中，这里只提供蓝图窗口特有的两点差异。

    def _capture_index(self):
        return getattr(self, "current_index", -1)

    def _capture_task_list(self):
        return self.tasks

    def _capture_after_task_changed(self):
        self._push_history()
        # 必须在 refresh() **之前**记住当前步骤：refresh() 会重建整个场景、丢掉
        # 选中项，进而触发 _refresh_editor_from_selection() 把 current_index 置为
        # -1。此前这里是在 refresh() 之后才读 self.current_index，于是拿到 -1，
        # `_select_node(-1)` 直接把编辑面板清空 —— 表现为"在蓝图窗口框选图片/
        # 记录点击点/框选识别区域之后，右侧面板整个空掉"。
        index = self.current_index
        self.refresh()
        self._select_node(index)
        self.save_layout()

    def _capture_show_parent(self):
        parent = self.parent()
        if parent is not None:
            parent.show()

    # ---------- 分组编辑钩子（组编辑的共享实现在 CaptureOverlayMixin 中）----------





    def _capture_set_no_current_task(self):
        self.current_index = -1



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


    def _update_edges(self):
        for edge in self._edges:
            self._apply_wire_geometry(edge)










    def auto_arrange(self):
        self._push_history()
        positions = self.layout_data.setdefault("positions", {})
        for index in range(len(self.tasks)):
            positions[str(index)] = (40 + (index % 3) * 280, 40 + (index // 3) * 130)
        self.refresh()


    def sync_positions(self):
        positions = self.layout_data.setdefault("positions", {})
        for item in self.scene.items():
            if isinstance(item, BlueprintNodeItem):
                if self.grid_snap:
                    item.setPos(round(item.x() / 20) * 20, round(item.y() / 20) * 20)
                positions[str(item.index)] = (item.x(), item.y())
        self.layout_data["zoom"] = self.view.transform().m11()





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
        # 先按 id 记录旧下标，删除后 id 顺序即等于新顺序。
        old_index_by_id = {str(t.get("id")): i for i, t in enumerate(self.tasks)}
        for index in selected:
            self.tasks.pop(index)
        for task in self.tasks:
            flow_target = task.get("flow_next")
            if flow_target is not None and not any(str(candidate.get("id")) == str(flow_target) for candidate in self.tasks):
                task.pop("flow_next", None)
        # 统一重编号：被删下标不在序列中，指向它们的跳转会被清除。
        remap_jump_targets(self.tasks, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in self.tasks]))
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





    def _load_editor_for_task(self, task):
        self.selected_label.setText(f"第 {self.current_index + 1} 步 · 类型: {task.get('type', 'normal')}")
        self._update_editor_visibility(task)
        self._load_common_editor_fields(task)

    def _editor_float_fallback(self, key, task, default):
        """蓝图窗口：输入框解析失败时保留任务里的原值（历史差异，见 mixin 里的说明）。"""
        return task.get(key, default)


    def _clear_editor(self):
        self.selected_label.setText("未选择步骤")
        for edit in (self.template_edit, self.description_edit, self.threshold_edit, self.timeout_edit, self.after_wait_edit, self.click_x_edit, self.click_y_edit, self.next_template_edit):
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
        self._set_region_group_visible(False)
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
            self._apply_common_editor_fields(task)
            self._editor_apply_optional(task)
        self._apply_special_fields(task)
        self._push_history()
        self.refresh()
        self._select_node(self.current_index)
        self.save_layout()

    # _int / _float / _normalize_region 已上移到 CaptureOverlayMixin：
    # 迂回子步骤对话框由两个窗口共用，以前这三个只挂在蓝图窗口上，主窗口点
    # 「保存」就抛 AttributeError。

    # ---------- 命中测试 ----------



    def _edge_for_handle(self, handle):
        for edge in self._edges:
            if handle in edge.get("bend_handles", []):
                return edge
        return None

    # ---------- 连线操作 ----------



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
        # 先按 id 记录旧下标，删除后 id 顺序即等于新顺序。
        old_index_by_id = {str(t.get("id")): i for i, t in enumerate(self.tasks)}
        self.tasks.pop(index)
        for task in self.tasks:
            flow_target = task.get("flow_next")
            if flow_target is not None and not any(str(c.get("id")) == str(flow_target) for c in self.tasks):
                task.pop("flow_next", None)
        # 统一重编号：被删下标不在序列中，指向它的跳转会被清除。
        remap_jump_targets(self.tasks, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in self.tasks]))
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
        types = ["normal", "loop", "keyboard_move", "key_press", "drag", "click_until_gone", "delay", "condition", "switch", "event"]
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


    def add_task(self, group_id=None, position=None):
        types = ["normal", "loop", "keyboard_move", "key_press", "drag", "click_until_gone", "delay"]
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
            # 复用 nodes.NodeGraph 的可达性计算。此前这里把 entry_index +
            # reachable_indices 的逻辑内联重写了一遍，且列表只列了 9 个跳转字段
            # （漏 event_trigger_target 与 wait_timeout_jump_to），于是仅靠这两类
            # 跳转才可达的步骤会被**误报为"不可达步骤"**。
            reachable = NodeGraph(self.tasks).reachable_indices()
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
            for key, label in JUMP_FIELD_LABELS.items():
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
            for case_value, target_number in switch_targets:
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
        old_positions = self.layout_data.get("positions", {})
        new_positions = {
            str(new_index): old_positions.get(str(old_index), old_positions.get(old_index, (40 + (new_index % 3) * 280, 40 + (new_index // 3) * 130)))
            for new_index, old_index in enumerate(ordered)
        }
        reordered = [old_tasks[i] for i in ordered]
        # 使用统一的跳转重编号函数。此前这里只重映射 detour_jump_to /
        # detour_success_jump_to 两个字段，会漏掉 timeout_jump_to 等。
        index_map = {old_index: new_index for new_index, old_index in enumerate(ordered)}
        remap_jump_targets(reordered, index_map)
        self.tasks[:] = reordered
        self.layout_data["positions"] = new_positions

    def closeEvent(self, event):
        self.save_layout()
        super().closeEvent(event)




class PySide6ScriptWindow(ExecutionControlMixin, TaskListMixin, PresetManagerMixin,
                          CaptureOverlayMixin, QMainWindow):
    stop_requested = Signal()

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
        # 运行前 config 的快照，收尾时用它还原（见 _restore_config_after_run）
        self._saved_config = None
        # 正在编辑的识别区域下标（一个步骤可以有多条区域）与它所属步骤的标识
        self._region_index = 0
        self._region_task_key = None
        self.capture_overlay = None
        self.blueprint_window = None
        self._hotkey = None
        self.execution_states = {}
        self.current_task_index = -1
        self.current_group_id = None
        self._pending_group_color = None
        # 构建期标志：期间不显示模板预览（详见 __init__ 末尾的说明）
        self._initializing = True
        self._task_list_commit_scheduled = False
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
        self.stop_requested.connect(self.stop_script)
        # 构建期结束：此后才允许显示模板预览。
        # refresh_task_list() 会自动选中第一个步骤并触发 _update_template_preview，
        # 若不设此标志，启动瞬间编辑面板就会显示第一张模板缩略图。
        self._initializing = False
        # 诊断信息（数据文件读取失败、全局热键注册失败、模板文件读不了…）必须落到
        # 日志框。启动脚本用隐藏窗口运行 python，stdout/stderr 用户看不到；
        # 不落日志就等于"问题静默消失"。
        self._warning_cursor = 0
        # 启动时先把"数据放在哪、资源在哪"记一行：打包成 exe 之后这是排查
        # "预设不见了 / 读不到模板"的第一手信息（用户可以直接看日志框）。
        self.append_log(paths.describe())
        self._log_pending_warnings()
        # 运行期也会产生诊断信息，其中热键那条来自后台线程、模板那条来自 worker 线程。
        # 只在 GUI 线程里搬运，所以用一个定时器轮询（而不是让 worker 直接碰控件）。
        self._warning_timer = QTimer(self)
        self._warning_timer.timeout.connect(self._log_pending_warnings)
        self._warning_timer.start(500)


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
        # 组折叠/展开状态需要持久化：此前只读取元数据里的 expanded，从不写入，
        # 导致在界面上折叠组后重启即恢复展开（gui.py 的对应实现是完整的）。
        self.task_list.itemExpanded.connect(self._on_group_expanded_changed)
        self.task_list.itemCollapsed.connect(self._on_group_expanded_changed)
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
        self.editor_layout = editor_layout          # 与蓝图窗口同名，便于共用构造器
        # 面板控件全部由 mixin 的共享构造器建（见 capture_overlay.py）
        self._build_shared_editor_panel(editor_layout)
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

        # 这两个 SVG 是**程序自带的界面资源**（复选框指示器），不是用户模板。
        # 它们有意固定在 <脚本目录>/icons，不跟随 config.ICON_DIR —— 用户把
        # ICON_DIR 指向自己的模板目录时，界面样式仍应正常。
        checked_indicator = os.path.join(paths.ICON_DIR, "checkbox_checked.svg").replace("\\", "/")
        indeterminate_indicator = os.path.join(paths.ICON_DIR, "checkbox_indeterminate.svg").replace("\\", "/")
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




    @Slot(str)
    def on_mode_selected(self, mode):
        mode = mode or "custom"
        self.mode_tasks.setdefault(mode, deepcopy(get_tasks_for_mode(mode)))
        TASKS[:] = deepcopy(self.mode_tasks[mode])
        self.current_task_index = -1
        self.refresh_task_list()
        self.append_log(f"已切换到预设: {mode}")




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
        self._update_editor_visibility(task)
        self.special_group.setTitle("持续点击设置" if is_click_until else "类型专用字段" if is_recognition else "步骤设置")
        self._load_common_editor_fields(task)

    def _editor_timeout_text(self, task):
        """主窗口：「超时(秒)」初值会回退到旧的 wait_timeout 字段（历史差异）。"""
        return str(task.get("timeout", task.get("wait_timeout", 5)))

    # 「可选步骤（跳过）」的回写：以前主窗口这里有一个空实现，导致勾选框点了等于没勾
    # （§8.2 #7）。2026-09-28 按所有者要求对齐蓝图窗口，直接沿用 mixin 的实现。

    # ---- 面板构造的 3 处显式差异（见 capture_overlay.py 里的共享构造器）----

    def _apply_editor(self):
        """与蓝图窗口同名的「应用修改」槽：共享构造器直接接这个名字。

        主窗口的实际逻辑仍在 apply_selected_task（保存、刷新、写日志），这里转调。
        """
        return self.apply_selected_task()

    def _editor_field_labels(self):
        """主窗口这两个标签多了括号说明，历史上与蓝图窗口不同。"""
        return {"threshold": "匹配阈值(0-1):", "timeout": "超时(秒，0为不限制):", "status": "启用状态:"}

    def _editor_special_form_container(self):
        """主窗口把「类型专用字段」套在 GroupBox 里（蓝图窗口是裸表单）。"""
        group = QGroupBox("类型专用字段")
        return group, QFormLayout(group)

    def _editor_preview_placeholder(self):
        """主窗口的预览占位文字（蓝图窗口是空字符串）。"""
        return "无模板预览"


    def _on_group_expanded_changed(self, item):
        """把组的展开/折叠状态写入分组元数据并落盘。

        只处理组项；步骤项的展开状态无意义，直接忽略。
        写入内容进入 __group_metadata__[预设]["expanded"]，重启后由
        refresh_task_list 读回恢复。
        """
        if item is None or item.data(0, Qt.UserRole) != "group":
            return
        group_id = str(item.data(0, Qt.UserRole + 1))
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        expanded = metadata.get("expanded")
        if not isinstance(expanded, dict):
            expanded = {}
            metadata["expanded"] = expanded
        state = bool(item.isExpanded())
        if expanded.get(group_id) == state:
            return
        expanded[group_id] = state
        self._save_presets()

    def _rebuild_special_form(self, task, form=None):
        """主窗口多一步：整组「类型专用字段」的显隐（蓝图窗口没有这个 GroupBox）。

        `form` 非空表示调用方只是借用同一份规格在别处摆字段（例如迂回子步骤对话框），
        此时不去动本窗口的 GroupBox。
        """
        super()._rebuild_special_form(task, form)
        if form is None:
            self.special_group.setVisible(bool(self.special_edits)
                                          and str(task.get("type", "normal")) != "click_until_gone")

    def _special_field_specs(self, task):
        """主窗口的类型专用字段表。

        与蓝图窗口（mixin 默认实现）的**唯一差异是标签文字**：主窗口这边历史上更简略
        （「执行前延时」而非「执行前延时(秒)」、「持续时间」而非「按住时长(秒)」…）。
        各自保留文案，只共享渲染/回写逻辑。
        """
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
        return list(field_specs.get(str(task.get("type", "normal")), ()))

    def _special_empty_input_pops(self):
        """主窗口历史上「输入为空」是写入回退值（浮点）或空串（文本），而不是删键。"""
        return False

    def _special_float_fallback(self, key, task):
        """主窗口：threshold 回退到 config.THRESHOLD，其余回退到任务里的原值。"""
        return config.THRESHOLD if key == "threshold" else task.get(key, 0.0)


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
        self.optional_checkbox.setChecked(False)
        self.enabled_checkbox.setChecked(False)
        self.click_x_edit.clear()
        self.click_y_edit.clear()
        self.next_template_edit.clear()
        self.wait_for_combo.setCurrentIndex(0)
        self._rebuild_special_form({})
        self.recognition_group.setVisible(False)
        self._set_region_group_visible(False)
        self.group_form.setVisible(False)
        self.current_group_id = None



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
            self._schedule_task_list_commit()
            return
        if item.data(0, Qt.UserRole) == "group":
            group_id = str(item.data(0, Qt.UserRole + 1))
            state = item.checkState(0)
            # PartiallyChecked 只是子项混合状态的自动显示（点击任意子项勾选框时 Qt 会自动
            # 把父项联动为 PartiallyChecked 并再次触发 itemChanged），此时各子项的 itemChanged
            # 已分别更新 TASKS，不能再按“未勾选”批量覆盖整组，否则会把整组全部禁用。
            if state == Qt.CheckState.PartiallyChecked:
                return
            checked = state == Qt.CheckState.Checked
            for task in TASKS:
                if str(task.get("group_id") or "group_default") == group_id:
                    task["enabled"] = checked
            self._schedule_task_list_commit()



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
        # 先按 id 记录每个步骤的旧下标——列表重排后 id 顺序即等于新顺序。
        old_index_by_id = {str(t.get("id")): i for i, t in enumerate(TASKS)}
        task = TASKS.pop(source_index)
        if source_index < target_index:
            target_index -= 1
        insert_index = target_index + (1 if insert_after else 0)
        insert_index = max(0, min(insert_index, len(TASKS)))
        TASKS.insert(insert_index, task)
        # 步骤下标已改变，必须同步重编号跳转目标，否则迂回/超时跳转会指向错误的步骤。
        remap_jump_targets(TASKS, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in TASKS]))
        self.current_task_index = insert_index
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(insert_index)


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

    # ---------- CaptureOverlayMixin 的钩子实现 ----------
    # 采集管线的共享实现在 CaptureOverlayMixin 中，这里只提供主窗口特有的差异。

    def _capture_index(self):
        return self.current_task_index


    def _capture_after_task_changed(self):
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)


    def _capture_notify_next_template_widgets(self, image_name):
        self.next_template_edit.setText(image_name)
        self._capture_set_wait_for_next()

    # ---------- 分组编辑钩子（组编辑的共享实现在 CaptureOverlayMixin 中）----------

    def _group_metadata_for_current_mode(self):
        mode = self.mode_combo.currentText() or "custom"
        return self.mode_group_metadata.setdefault(mode, {})

    def _group_default_color(self):
        return "#e0f2fe"

    def _group_color_button_text_color(self):
        return "#1f2937"

    def _capture_set_no_current_task(self):
        self.current_task_index = -1

    def _group_hide_editor_groups(self):
        self.special_group.setVisible(False)
        self.click_until_group.setVisible(False)

    def _group_after_editor_applied(self, group_id, name):
        self._save_presets()
        self.refresh_task_list()
        self.append_log(f"已应用组设置: {name}")

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
        types = ["normal", "loop", "keyboard_move", "key_press", "drag", "click_until_gone", "delay"]
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
                task["click_interval"] = self._float(self.click_until_interval_edit.text(), 0.5)
                task["stop_delay"] = self._float(self.click_until_stop_delay_edit.text(), 0.0)
                task["timeout"] = self._float(self.click_until_timeout_edit.text(), 30.0)
                task["continue_after_timeout"] = self.click_until_continue_checkbox.isChecked()
                task["stop_on_change"] = self.click_until_stop_on_change_checkbox.isChecked()
                task["click"] = True
                task["required"] = True
            else:
                self._apply_common_editor_fields(task)
                # 「可选步骤（跳过）」：与蓝图窗口一致地回写（§8.2 #7 已在 2026-09-28 修复）
                self._editor_apply_optional(task)
        task["enabled"] = self.enabled_checkbox.isChecked()
        # 类型专用字段的回写：与蓝图窗口共用同一实现（逻辑一份，规格表各自提供）
        self._apply_special_fields(task)
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)
        self.append_log(f"已应用第 {self.current_task_index + 1} 步修改。")

    # _float / _int 已在 CaptureOverlayMixin 里统一实现（两窗口共用一套名字）。
    # 此前主窗口另有 _float_value / _int_value 两个逐字相同的别名，已删除。

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
        # 图数据里是「按节点 id」记录的连接，比 1-based 序号抗重排。这里只借它
        # 补齐任务侧**为空**的跳转字段，绝不覆盖已有值（详见 nodes.fill_missing_connections）。
        blueprint_tasks = deepcopy(TASKS)
        repaired = fill_missing_connections(blueprint_tasks, self.blueprint_graphs.get(mode, {}))
        self.blueprint_window = BlueprintWindow(
            blueprint_tasks,
            layout_data,
            lambda tasks, data, meta=None: self.save_blueprint_state(mode, tasks, data, meta),
            group_metadata,
            self,
            self.execution_states,
        )
        self.blueprint_window.show()
        if repaired:
            self.append_log(f"已用蓝图图数据补齐 {repaired} 条缺失连线。")

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
        """返回启用的步骤副本，并按完整列表位置记录 _outer_step_number，
        使迂回/条件/循环/选择等按编号跳转与蓝图中的步骤编号一致（即使中间存在禁用步骤）。"""
        runtime_tasks = []
        for task_index, task in enumerate(TASKS):
            if not task.get("enabled", True):
                continue
            runtime_task = deepcopy(task)
            runtime_task["_outer_step_number"] = task_index + 1
            runtime_tasks.append(runtime_task)
        return runtime_tasks

    def _set_running_state(self, running):
        self.mode_combo.setEnabled(not running)
        self.task_list.setEnabled(not running)
        self.apply_button.setEnabled(not running)
        for button in self.preset_buttons:
            button.setEnabled(not running)
        # 任务操作按钮（全选/清空/上移/下移/新增组/新增步骤/复制/删除/打开蓝图）
        # 此前在运行期间仍可点击，会造成界面与正在执行的内容漂移、并在运行中落盘。
        for button in self.task_buttons:
            button.setEnabled(not running)
        # 目标窗口在启动时已写入 config，运行中修改不会生效，禁用以免误导。
        self.window_combo.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.start_current_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.pause_button.setEnabled(running)
        self.step_button.setEnabled(running)


    @Slot()
    def start_from_current(self):
        if 0 <= self.current_task_index < len(TASKS):
            self._start_worker(TASKS[self.current_task_index].get("id"))









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



    def closeEvent(self, event):
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None
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
