# PySide6 主界面：新版任务编辑器、蓝图编辑器和线程化执行控制。
import os
import time
import sys
import tempfile
import threading
import time
import uuid
from copy import deepcopy

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")

import pygetwindow as gw
from PySide6.QtCore import QThread, QTimer, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPixmap, QBrush
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
    QToolButton,
    QSizePolicy,
    QProgressBar,
    QStackedWidget,
    QLineEdit,
)

import config
import paths
from main import reload_templates
from tasks import (
    DELETED_PRESET_NAMES,
    FALLBACK_STEPS,
    SNIPPETS,
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
from capture_overlay import CaptureOverlay, CaptureOverlayMixin, _build_step_list_editor
from blueprint_groups import BlueprintGroupMixin
from blueprint_interaction import BlueprintInteractionMixin
from blueprint_snippets import BlueprintSnippetMixin
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
from qt_translations import install_qt_translations
from recognition_debug_window import RecognitionDebugWindow
from update_ui import (UpdateCheckRunner, UpdateDownloadRunner, ask_update_choice,
                       build_update_helper, can_auto_update, cleanup_update_leftovers,
                       extract_update_zip, launch_update_helper, notify_update,
                       report_check_failure)
import snippets as snippet_store
import settings_store
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
                     BlueprintSnippetMixin, CaptureOverlayMixin, QMainWindow):
    _BASIC_FIELDS = {
        "id", "type", "mode", "enabled", "template", "templates", "description",
        "threshold", "timeout", "wait_timeout", "after_wait", "click", "optional", "required",
        "click_x", "click_y", "click_position", "match_rect", "search_rect", "match_rects",
        "next_template", "next_templates", "wait_for", "group_id", "group_name", "group_color",
        "flow_next", "flow_next_disabled", "blueprint_bends", "blueprint_collapsed",
        "blueprint_color", "blueprint_comment", "detour_enabled", "detour_steps",
        "detour_jump_to", "detour_success_jump_to",
    }

    def __init__(self, tasks, layout_data, save_callback, group_metadata=None, parent=None,
                 execution_states=None, snippets_provider=None, snippets_save=None):
        super().__init__(parent)
        self.tasks = tasks
        self.save_callback = save_callback
        # 片段库由主窗口注入（同一份 __snippets__ 数据，蓝图不另存一份）
        self.snippets_provider = snippets_provider
        self.snippets_save = snippets_save
        self.layout_data = layout_data if isinstance(layout_data, dict) else {}
        self.group_metadata = group_metadata if isinstance(group_metadata, dict) else {}
        self.history = []
        self.redo_history = []
        self.clipboard = []
        self.grid_snap = False
        self.collapsed_groups = self._load_collapsed_groups()
        # 子图聚焦（F14）：None = 根层；否则为正在查看的组 id（仅视图状态，不写入数据）
        self.focus_group = None
        self.current_index = -1
        self.current_group_id = None
        self._pending_group_color = None
        # 正在编辑的识别区域下标（一个步骤可以有多条区域）与它所属步骤的标识
        self._region_index = 0
        self._region_task_key = None
        self.selected_edge = None
        self._refreshing = False
        self._scene_rect_pending = False
        # 编辑器正在程序化加载字段：期间控件变化不算用户改动
        self._editor_loading = False
        self.capture_overlay = None
        self._capture_callback = None
        self.execution_states = dict(execution_states or {})
        self.setWindowTitle(f"蓝图流程 - PySide6（v{config.APP_VERSION}）")
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
        self.view.group_enter_requested.connect(self.enter_group)
        self.view.leave_focus_requested.connect(self.leave_group)
        self.view.node_toggle_requested.connect(self.toggle_collapse)
        self.view.wire_clicked.connect(self._on_wire_clicked)
        self.view.wires_cut.connect(self._on_wires_cut)
        # zoom_changed 此前从未 connect；接上它才能在滚轮缩放后立即持久化
        self.view.zoom_changed.connect(self._on_zoom_changed)
        # Esc 退出子图：绑定在画布控件上（WidgetShortcut），焦点在画布时才生效
        self._leave_focus_action = QAction(self.view)
        self._leave_focus_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        self._leave_focus_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self._leave_focus_action.triggered.connect(self.leave_group)
        self.view.addAction(self._leave_focus_action)
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

        # 子图导航条（F14）：进入组内部后显示路径与返回按钮
        self.breadcrumb_bar = QWidget()
        breadcrumb_layout = QHBoxLayout(self.breadcrumb_bar)
        breadcrumb_layout.setContentsMargins(0, 0, 0, 0)
        self.back_button = QPushButton("← 返回上级")
        self.back_button.clicked.connect(self.leave_group)
        self.root_button = QPushButton("根层")
        self.root_button.clicked.connect(self.focus_root)
        self.breadcrumb_label = QLabel("视图：根层（所有步骤）")
        breadcrumb_layout.addWidget(self.back_button)
        breadcrumb_layout.addWidget(self.root_button)
        breadcrumb_layout.addWidget(self.breadcrumb_label, 1)
        self.back_button.setEnabled(False)
        self.root_button.setEnabled(False)

        canvas_panel = QGroupBox("蓝图流程")
        canvas_layout = QVBoxLayout(canvas_panel)
        canvas_layout.addWidget(toolbar)
        canvas_layout.addWidget(self.breadcrumb_bar)
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
            focus_visible = self._focus_visible_indices()
            for index, task in enumerate(self.tasks):
                item = BlueprintNodeItem(index, task)
                position = positions.get(str(index), positions.get(index))
                if isinstance(position, (list, tuple)) and len(position) == 2:
                    item.setPos(float(position[0]), float(position[1]))
                else:
                    item.setPos(40 + (index % 3) * 280, 40 + (index // 3) * 130)
                self.scene.addItem(item)
                item.set_runtime_state(self.execution_states.get(str(task.get("id"))))
                in_focus = focus_visible is None or index in focus_visible
                item.setVisible(in_focus and not self._node_hidden(task))
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
            # 子图视图里只显示聚焦组及其后代的组框（父组框会框住整个画面，隐藏）
            if focus_visible is not None:
                # 聚焦组自己的框不再画（已经进来了；画出来会压住内部节点），只留子组框
                allowed_groups = {str(gid) for gid in self._group_descendants(self.focus_group)}
                for group_id, box in self._group_items.items():
                    box.setVisible(str(group_id) in allowed_groups)
            self._render_connections(node_items)
            for item in node_items.values():
                item.set_move_callback(self._on_node_moved)
            self.scene.setSceneRect(self._visible_scene_rect().adjusted(-80, -80, 80, 80))
        finally:
            self._refreshing = False
        self._update_breadcrumb()
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





    def _canvas_default_group(self):
        """画布空白处新建内容时默认放进哪个组：子图视图内=当前聚焦组，否则根层(None)。"""
        focus = getattr(self, "focus_group", None)
        return str(focus) if focus else None

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
        focus_visible = self._focus_visible_indices()
        for source_index, output, target_index, is_default in self._connections():
            if source_index not in node_items or target_index not in node_items:
                continue
            if focus_visible is not None and (source_index not in focus_visible
                                              or target_index not in focus_visible):
                continue
            source = node_items[source_index]
            target = node_items[target_index]
            source_box = self._collapsed_box(source_index)
            target_box = self._collapsed_box(target_index)
            # _collapsed_box 会尊重聚焦状态：进入折叠组内部后，组内可见节点的端点返回 None，
            # 所以内部连线不会被误判成"同框"而跳过（否则进入后整组断线）。
            if source_box is not None and source_box is target_box:
                # 两端都被折叠进同一个组框（含嵌套折叠）：这条线在框内不可见，跳过
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
            # 只选中了连线：删除这些连接（框选连线后按 Delete 走的就是这里）
            wires = [item for item in self.scene.selectedItems() if isinstance(item, BlueprintWireItem)]
            if wires:
                self._delete_wires(wires)
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
        # 刚用 Ctrl+右键划过线：这次右键已经用于划断，不要再弹菜单
        if self.view.consume_menu_suppression():
            return
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
            selected_wires = [i for i in self.scene.selectedItems() if isinstance(i, BlueprintWireItem)]
            menu = QMenu(self)
            add_bend_action = menu.addAction("在线上添加转折点")
            if len(selected_wires) > 1 and clicked_item in selected_wires:
                # 框选了多条线后右键其中一条 → 一起删（等价于"批量断线"）
                delete_conn_action = menu.addAction(f"删除选中的 {len(selected_wires)} 条连接")
                delete_conn_action.triggered.connect(lambda: self._delete_wires(selected_wires))
            else:
                delete_conn_action = menu.addAction("删除连接")
                delete_conn_action.triggered.connect(lambda: self._delete_connection(edge))
            add_bend_action.triggered.connect(lambda: self._add_bend(edge, scene_position))
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
        add_task_action.triggered.connect(
            lambda: self.add_task(self._canvas_default_group(), (scene_position.x(), scene_position.y())))
        add_group_action = menu.addAction("新增组")
        add_group_action.triggered.connect(
            lambda: self.add_group(self._canvas_default_group(), (scene_position.x(), scene_position.y())))
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
        menu.addSeparator()
        self._add_snippet_menu(menu, (scene_position.x(), scene_position.y()))
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
        # 没选步骤时不该显示"步骤名称 / 启用状态 / 多模板时"
        self.name_block.setVisible(False)
        for edit in (self.template_edit, self.description_edit, self.threshold_edit, self.timeout_edit, self.retry_count_edit, self.retry_interval_edit, self.ocr_text_edit, self.ocr_confidence_edit, self.after_wait_edit, self.click_x_edit, self.click_y_edit, self.next_template_edit):
            edit.clear()
        self.click_checkbox.setChecked(False)
        # 取消 OCR 勾选（toggled 会把「目标文字」等选项一起隐藏）
        self.ocr_checkbox.setChecked(False)
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


        self._editor_loading = False
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
        self._delete_connections([edge])

    def _delete_wires(self, wires):
        """按连线图元删除连接（框选 / 右键多选时用）。"""
        wire_set = set(wires)
        self._delete_connections([edge for edge in self._edges if edge["wire"] in wire_set])

    def _on_wires_cut(self, wires):
        """Ctrl+右键划断：一次性切掉划过的那几条连接。"""
        self._delete_wires(list(wires or []))

    def _clear_connection_fields(self, source_task, kind):
        """把一条连接对应的跳转字段清掉（含迂回开关的对称处理）。"""
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
        # 迂回的两条跳转线就是「这条步骤用不用迂回」在画布上的表示（连上时
        # connect_nodes 会置 detour_enabled=True），所以切断后必须对称地关掉开关：
        # 引擎的判据是 `detour_enabled or detour_jump_to is not None`（main.py），
        # 只清跳转而留着开关，会让这一步在识别失败时**照样**走迂回流程、
        # 而且因为没有跳转目标，失败会被静默吞掉（不再按超时报错）。
        # 只在两条线都不剩时才关：只切成一条说明迂回仍然接在图上；已配置的
        # detour_steps 一律保留，不做破坏性清空。
        if kind in ("detour_success", "detour_failure") and not (
                source_task.get("detour_jump_to") is not None
                or source_task.get("detour_success_jump_to") is not None):
            source_task["detour_enabled"] = False

    def _delete_connections(self, edges):
        """一次删除多条连接：只推一次历史、重建一次场景、落盘一次。"""
        edges = [edge for edge in edges if edge is not None]
        if not edges:
            return
        self._push_history()
        for edge in edges:
            self._clear_connection_fields(self.tasks[edge["source"].index], edge["edge_kind"])
        self.selected_edge = None
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
        self.setWindowTitle(f"视觉识别自动脚本 - PySide6（v{config.APP_VERSION}）")
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
        # 全局兜底处理步骤（F7）：按模式保存，结构与分组元数据同源
        self.mode_fallback_steps = deepcopy(FALLBACK_STEPS)
        # 步骤片段（F13）：全局共享，存在预设文件的 __snippets__ 里
        self.snippets = deepcopy(SNIPPETS)
        # 主窗口的"复制/粘贴"剪贴板（Ctrl+C 复制、Ctrl+V 粘贴；蓝图窗口另有自己的 self.clipboard）
        self.task_clipboard = None
        # 主窗口撤销/重做（快照式：TASKS + 分组元数据 + 当前选中下标）
        self.history = []
        self.redo_history = []
        self._restoring_history = False
        # 编辑器脏标记（M2：决定「应用修改」要不要压撤销栈）
        self._editor_dirty = False
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
        self.mode_combo.setMinimumWidth(160)
        self.mode_combo.setMaximumWidth(240)
        self.mode_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        preset_bar.addWidget(self.mode_combo)
        # 预设管理收进「预设 ▾」下拉：这 6 个操作都是低频管理动作，
        # 常驻按钮会把主界面挤成三行按钮（对比 MaaEnd/MXU：主操作常驻、其余收进菜单）。
        preset_menu = QMenu(self)
        for label, handler in (
            ("新建预设", self.create_preset),
            ("重命名预设", self.rename_current_preset),
            ("复制到预设", self.copy_current_preset),
            ("导出预设", self.export_current_preset),
            ("导入预设", self.import_preset),
            ("删除预设", self.delete_current_preset),
        ):
            action = preset_menu.addAction(label)
            action.triggered.connect(handler)
        self.preset_menu = preset_menu
        self.preset_menu_button = QToolButton()
        self.preset_menu_button.setText("预设 ▾")
        self.preset_menu_button.setMenu(preset_menu)
        self.preset_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.preset_menu_button.setToolTip("新建 / 重命名 / 复制 / 导入导出 / 删除预设")
        self.preset_menu_button.setMinimumWidth(78)
        self.preset_menu_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        preset_bar.addWidget(self.preset_menu_button)
        self.save_button = QPushButton("保存")
        self.save_button.clicked.connect(self.save_current_tasks)
        self.save_button.setMinimumWidth(64)
        self.save_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.save_button.setToolTip(
            "把当前预设的步骤与分组立即保存到文件（Ctrl+S）；增删/拖动/分组等操作会自动保存")
        preset_bar.addWidget(self.save_button)
        preset_bar.addStretch(1)
        self.save_shortcut = QAction(self)
        self.save_shortcut.setShortcut(QKeySequence("Ctrl+S"))
        self.save_shortcut.triggered.connect(self.save_current_tasks)
        self.addAction(self.save_shortcut)
        # 运行中需要禁用这些入口：保留列表给 _set_running_state 统一处理
        self.preset_buttons = [self.preset_menu_button, self.save_button]
        root_layout.addLayout(preset_bar)

        control_bar = QHBoxLayout()
        self.start_button = QPushButton("开始执行")
        self.start_button.clicked.connect(self.start_script)
        self.start_current_button = QPushButton("从当前步骤执行")
        self.start_current_button.clicked.connect(self.start_from_current)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self.stop_script)
        self.stop_button.setEnabled(False)
        self.pause_button = QPushButton("暂停")
        self.pause_button.clicked.connect(self.toggle_pause)
        self.pause_button.setEnabled(False)
        self.step_button = QPushButton("单步")
        self.step_button.setObjectName("secondaryButton")
        self.step_button.clicked.connect(self.step_script)
        self.step_button.setEnabled(False)
        self.step_button.setToolTip("暂停状态下点一次执行一个步骤")
        # 左侧 5 个控制按钮**等比拉伸填满整行剩余宽度**（窗口变宽/变窄都自动适应）
        self.control_buttons = [self.start_button, self.start_current_button,
                                self.stop_button, self.pause_button, self.step_button]
        for button in self.control_buttons:
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            control_bar.addWidget(button, 1)
        control_bar.addSpacing(8)
        control_bar.addWidget(QLabel("目标窗口:"))
        self.window_combo = QComboBox()
        self.window_combo.setEditable(True)
        self.window_combo.setMinimumWidth(240)
        self.window_combo.setMaximumWidth(420)
        control_bar.addWidget(self.window_combo)
        self.refresh_window_button = QPushButton("刷新窗口")
        self.refresh_window_button.setObjectName("secondaryButton")
        self.refresh_window_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.refresh_window_button.clicked.connect(self.refresh_window_list)
        control_bar.addWidget(self.refresh_window_button)
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
        self.task_list.task_into_group_requested.connect(self.move_task_into_group)
        self.task_list.task_before_group_requested.connect(self.move_task_before_group)
        self.task_list.task_to_root_requested.connect(self.move_task_to_root)
        self.task_list.group_reparent_requested.connect(self.reparent_group)
        self.task_list.group_reorder_requested.connect(self.move_group_block)
        # 组折叠/展开状态需要持久化：此前只读取元数据里的 expanded，从不写入，
        # 导致在界面上折叠组后重启即恢复展开（gui.py 的对应实现是完整的）。
        self.task_list.itemDoubleClicked.connect(self._on_task_item_double_clicked)
        self.task_list.itemExpanded.connect(self._on_group_expanded_changed)
        self.task_list.itemCollapsed.connect(self._on_group_expanded_changed)
        # 按钮区已经**全部撤掉**（所有者 2026-10-26："右键菜单有的就不要放到项目列表上了"）：
        # 列表编辑动作（全选/清空/上移/下移/新增组/新增步骤/复制/删除）都在右键菜单里，
        # 面板上只留右上角「工具 ▾」（全局功能入口，右键菜单里没有）。
        # `task_buttons` 保留为空列表：外部代码/测试仍可安全遍历，不必改调用方。
        self.task_buttons = []

        # 全局工具：不折叠，直接摆在面板上（所有者 2026-10-26：面板空着不如把工具都放出来）
        # 4 列网格 → 前 4 个工具一行，第 5 个（打开蓝图流程）落到第二行
        self.tool_buttons = []
        self.tool_actions = {}          # 兼容旧引用：标签 -> 按钮
        tool_specs = [
            ("检查更新", self.check_updates),
            ("识别调试", self.open_recognition_debug),
            ("兜底处理", self.open_fallback_editor),
            ("片段", self.open_snippet_menu),
            ("打开蓝图流程", self.open_blueprint),
        ]
        tool_grid = QGridLayout()
        tool_grid.setContentsMargins(0, 0, 0, 0)
        tool_grid.setHorizontalSpacing(8)
        tool_grid.setVerticalSpacing(4)
        for index, (label, handler) in enumerate(tool_specs):
            button = QPushButton(label)
            button.clicked.connect(handler)
            # 4+1 布局，但按钮横向**等比拉伸**：面板变宽时按钮跟着变宽，不再大小不一。
            # 第 5 个「打开蓝图流程」横跨整行，避免第二行只占 1/4 显得空。
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if index < 4:
                tool_grid.addWidget(button, 0, index)
            else:
                tool_grid.addWidget(button, 1, 0, 1, 4)
            self.tool_buttons.append(button)
            self.tool_actions[label] = button
        for column in range(4):
            tool_grid.setColumnStretch(column, 1)
        task_layout.addLayout(tool_grid)

        # 任务列表右键菜单：常用编辑动作不必回按钮区找（列表此前完全没有右键菜单）
        self.task_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_list.customContextMenuRequested.connect(self.show_task_list_menu)
        # 列表快捷键：action 挂在 task_list 上 + WidgetShortcut 作用域，
        # 只有焦点在列表里时才生效——否则会抢走「步骤名称」等输入框里的 Delete / Ctrl+C / Ctrl+A。
        self.list_shortcuts = []
        shortcut_specs = [
            ("Del", self.delete_selected_item),
            ("Ctrl+C", self.copy_selected_item),
            ("Ctrl+V", self.paste_clipboard_item),
            ("Ctrl+A", self.select_all_tasks),
            ("Ins", self.add_task),
            ("Ctrl+Up", lambda: self.move_selected_item(-1)),
            ("Ctrl+Down", lambda: self.move_selected_item(1)),
            ("Ctrl+Z", self.undo),
            ("Ctrl+Y", self.redo),
            ("Ctrl+Shift+Z", self.redo),
        ]
        for sequence, handler in shortcut_specs:
            action = QAction(self.task_list)
            action.setShortcut(QKeySequence(sequence))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(handler)
            self.task_list.addAction(action)
            self.list_shortcuts.append(action)
        self.task_stack = QStackedWidget()
        self.task_stack.addWidget(self.task_list)
        self.task_empty_page = QLabel(
            "这条预设还没有步骤\n\n"
            "· 右键空白处 →「新增步骤 / 新增分组」\n"
            "· 「片段」→「插入片段」复用已有流程\n"
            "· 「打开蓝图流程」可视化连线编辑")
        self.task_empty_page.setObjectName("emptyState")
        self.task_empty_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.task_empty_page.setWordWrap(True)
        # 空预设时列表区显示的是引导页（task_list 被隐藏）；右键也要能呼出
        # 「新增步骤 / 新增组」菜单，否则一个步骤都没有时无法创建第一个步骤。
        self.task_empty_page.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_empty_page.customContextMenuRequested.connect(self._show_empty_list_menu)
        self.task_stack.addWidget(self.task_empty_page)
        task_layout.addWidget(self.task_stack, 1)
        splitter.addWidget(task_panel)

        editor_panel = QGroupBox("当前步骤")
        editor_panel_layout = QVBoxLayout(editor_panel)
        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor_content = QWidget()
        editor_layout = QVBoxLayout(editor_content)
        editor_scroll.setWidget(editor_content)
        self.editor_scroll = editor_scroll
        self.editor_empty_hint = QLabel("从左侧选择一个步骤\n即可在这里编辑参数")
        self.editor_empty_hint.setObjectName("emptyState")
        self.editor_empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.editor_empty_hint.setWordWrap(True)
        self.editor_empty_hint.setVisible(False)
        editor_panel_layout.addWidget(self.editor_empty_hint)
        editor_panel_layout.addWidget(editor_scroll)
        self.editor_layout = editor_layout          # 与蓝图窗口同名，便于共用构造器
        # 面板控件全部由 mixin 的共享构造器建（见 capture_overlay.py）
        self._build_shared_editor_panel(editor_layout)
        # M1：在「类型: … | ID: …」一行后显示编辑状态胶囊（未应用 / 已保存）
        self.editor_state_label = QLabel("已保存")
        self.editor_state_label.setObjectName("editorState")
        self.editor_state_label.setProperty("stateKind", "saved")
        self.editor_state_label.setVisible(False)
        # 与「类型 / ID」同一行：把 selected_label 和状态胶囊放进一个水平布局
        selected_index = editor_layout.indexOf(self.selected_label)
        if selected_index >= 0:
            editor_layout.removeWidget(self.selected_label)
        else:
            selected_index = 0
        state_row = QHBoxLayout()
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.setSpacing(8)
        state_row.addWidget(self.selected_label)
        # 状态胶囊贴到这一行的最右边（中间留伸缩空白）
        state_row.addStretch(1)
        state_row.addWidget(self.editor_state_label)
        editor_layout.insertLayout(selected_index, state_row)
        self._connect_editor_dirty_signals()
        splitter.addWidget(editor_panel)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([320, 500])

        log_panel = QGroupBox("日志输出")
        log_layout = QVBoxLayout(log_panel)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        # M6：日志最多保留 5000 行，避免长脚本/循环执行无限增长导致卡顿
        self.log_box.setMaximumBlockCount(5000)
        log_layout.addWidget(self.log_box)
        content_splitter = QSplitter(Qt.Orientation.Vertical)
        content_splitter.addWidget(splitter)
        content_splitter.addWidget(log_panel)
        content_splitter.setStretchFactor(0, 5)
        content_splitter.setStretchFactor(1, 1)
        content_splitter.setSizes([650, 170])
        root_layout.addWidget(content_splitter, 1)

        # ---------- 底部状态栏（L2/L3/V4/O8） ----------
        status_bar = QWidget()
        status_bar.setObjectName("statusBar")
        self.status_bar_widget = status_bar
        status_layout = QHBoxLayout(status_bar)
        status_layout.setContentsMargins(6, 2, 6, 2)
        status_layout.setSpacing(8)

        # 运行状态胶囊：宽度只按"会显示的最长文案"固定，不再吃满整行；
        # 剩余空间留给执行步骤进度条。
        self.status_label = QLabel("● 待机")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setProperty("statusKind", "idle")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        longest = max(
            self.status_label.fontMetrics().horizontalAdvance(text)
            for text in ("● 待机", "▶ 运行中（第 12/12 步）", "⚠ 异常（第 12/12 步失败）",
                         "⏸ 已暂停（单步执行）", "✔ 已完成", "■ 已停止（正在停止）")
        )
        self.status_label.setFixedWidth(longest + 28)
        status_layout.addWidget(self.status_label)
        self.preset_info_label = QLabel("预设: custom")
        self.preset_info_label.setObjectName("statusInfo")
        status_layout.addWidget(self.preset_info_label)
        self.last_save_label = QLabel("")
        self.last_save_label.setObjectName("statusInfo")
        status_layout.addWidget(self.last_save_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("runProgress")
        self.progress_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.progress_bar.setMinimumWidth(180)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m 步")
        self.progress_bar.setVisible(True)
        status_layout.addWidget(self.progress_bar, 1)
        self.settings_button = QPushButton("设置")
        self.settings_button.setObjectName("secondaryButton")
        self.settings_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.settings_button.setToolTip("检查更新的代理地址等用户设置")
        self.settings_button.clicked.connect(self.open_settings_dialog)
        status_layout.addWidget(self.settings_button)
        self.help_button = QPushButton("使用说明")
        self.help_button.setObjectName("secondaryButton")
        self.help_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.help_button.clicked.connect(self.open_help)
        status_layout.addWidget(self.help_button)
        self.about_button = QPushButton("关于")
        self.about_button.setObjectName("secondaryButton")
        self.about_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.about_button.clicked.connect(self.show_about)
        status_layout.addWidget(self.about_button)
        # 清空日志放在「关于」后面
        self.clear_log_button = QPushButton("清空日志")
        self.clear_log_button.setObjectName("secondaryButton")
        self.clear_log_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.clear_log_button.clicked.connect(self.clear_log)
        status_layout.addWidget(self.clear_log_button)
        root_layout.addWidget(status_bar)

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
            "QToolButton { min-height: 26px; padding: 3px 10px; border: 1px solid #94a3b8; border-radius: 4px;"
            " background: #ffffff; color: #1e293b; }"
            "QToolButton:hover { background: #e0f2fe; border-color: #0284c7; }"
            "QToolButton:pressed { background: #bae6fd; }"
            "QToolButton:disabled { color: #94a3b8; background: #e2e8f0; }"
            "QToolButton::menu-indicator { width: 0px; }"
            "#secondaryButton { color: #475569; background: #f1f5f9; border-color: #cbd5e1; }"
            "#secondaryButton:hover { background: #e2e8f0; border-color: #94a3b8; }"
            "#statusBar { background: #eef2f7; border-top: 1px solid #cbd5e1; }"
            "#statusLabel { color: #475569; font-weight: bold; padding: 1px 8px; border-radius: 9px; background: #e2e8f0; }"
            "#statusLabel[statusKind=\"running\"] { color: #ffffff; background: #0284c7; }"
            "#statusLabel[statusKind=\"paused\"] { color: #92400e; background: #fef3c7; }"
            "#statusLabel[statusKind=\"error\"] { color: #ffffff; background: #dc2626; }"
            "#statusLabel[statusKind=\"done\"] { color: #ffffff; background: #16a34a; }"
            "#statusLabel[statusKind=\"stopped\"] { color: #475569; background: #e2e8f0; }"
            "#statusInfo { color: #64748b; }"
            "#editorState { color: #475569; font-weight: bold; padding: 1px 10px; border-radius: 9px;"
            " background: #e2e8f0; }"
            "#editorState[stateKind=\"dirty\"] { color: #92400e; background: #fef3c7; }"
            "#editorState[stateKind=\"saved\"] { color: #166534; background: #dcfce7; }"
            "#emptyState { color: #94a3b8; font-size: 13px; padding: 16px; }"
            "#runProgress { min-height: 18px; max-height: 20px; border: 1px solid #cbd5e1; border-radius: 5px;"
            " background: #ffffff; color: #334155; text-align: center; font-weight: bold; }"
            "#runProgress::chunk { background: #0284c7; border-radius: 4px; }"
            "QLineEdit, QPlainTextEdit, QComboBox, QTreeWidget { border: 1px solid #cbd5e1; border-radius: 4px; background: #ffffff; padding: 4px; }"
            "QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QTreeWidget:focus { border-color: #0284c7; }"
            "QTreeWidget { padding: 6px; }"
            "QTreeWidget::item { padding: 4px; }"
            "QTreeWidget::item:selected { background: #dbeafe; color: #0f172a; }"
            # 复选框由 TaskItemDelegate 自绘（Windows 11 原生样式下 QSS indicator 在组行上
            # 不会常驻绘制，只有悬浮才出现）；这里只保留 20px 占位，避免文字位置变化。
            "QTreeWidget::indicator { width: 20px; height: 20px; background: transparent; border: none; image: none; }"
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
        # 切换预设后旧快照不再适用（快照里的任务是上一个预设的）
        self.history = []
        self.redo_history = []
        self.mode_tasks.setdefault(mode, deepcopy(get_tasks_for_mode(mode)))
        TASKS[:] = deepcopy(self.mode_tasks[mode])
        self.current_task_index = -1
        # 切换预设：上一预设的编辑器脏标记不该带过来
        self._editor_dirty = False
        self._update_preset_info()
        self.refresh_task_list()
        self.append_log(f"已切换到预设: {mode}")




    def _task_index_from_item(self, item):
        if item is None or item.data(0, Qt.UserRole) == "group":
            return None
        index = item.data(0, Qt.UserRole + 1)
        return int(index) if index is not None else None

    @Slot(QTreeWidgetItem, QTreeWidgetItem)
    def load_selected_task(self, item, _previous_item):
        self._show_editor_page(False)
        self._editor_loading = True
        try:
            if item is not None and item.data(0, Qt.UserRole) == "group":
                # 选中的是分组：置空的步骤下标，分组「保存片段」等动作仍能找回
                self.current_task_index = -1
                self._hide_editor_state()
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
            # 显示用 ID 统一去掉分段横线（历史步骤是 uuid4 带横线、旧数据无横线，界面上统一）
            display_id = str(task.get("id", "")).replace("-", "")
            self.selected_label.setText(f"类型: {task.get('type', 'normal')} | ID: {display_id}")
            is_click_until = task.get("type") == "click_until_gone"
            is_recognition = task.get("type", "normal") in ("normal", "advanced")
            self._update_editor_visibility(task)
            self.special_group.setTitle("特殊字段设置" if is_click_until else "识别专用字段" if is_recognition else "类型设置")
            self._load_common_editor_fields(task)
        finally:
            self._editor_loading = False
        self._set_editor_state(True)

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
        return {"threshold": "匹配阈值(0-1):", "timeout": "超时(秒，0为不限制):", "status": "启用状态:",
                "retry_count": "失败重试次数:", "retry_interval": "重试间隔(秒):"}

    def _editor_special_form_container(self):
        """主窗口把「类型专用字段」套在 GroupBox 里（蓝图窗口是裸表单）。"""
        group = QGroupBox("类型专用字段")
        return group, QFormLayout(group)

    def _editor_preview_placeholder(self):
        """主窗口的预览占位文字（蓝图窗口是空字符串）。"""
        return "无模板预览"


    @Slot(object, int)
    def _on_task_item_double_clicked(self, item, _column):
        """双击组行 = 折叠/展开（蓝图里组头双击也是这个语义，主界面此前没有入口）。"""
        if item is not None and item.data(0, Qt.UserRole) == "group":
            item.setExpanded(not item.isExpanded())

    def _on_group_expanded_changed(self, item):
        """把组的展开/折叠状态写入分组元数据并落盘。

        只处理组项；步骤项的展开状态无意义，直接忽略。
        写入内容进入 __group_metadata__[预设]["expanded"]，重启后由
        refresh_task_list 读回恢复。
        """
        if item is None or item.data(0, Qt.UserRole) != "group":
            return
        if getattr(self, "_suppress_group_expand_sync", False):
            # 程序化选中步骤时，Qt 会为"让选中项可见"临时展开折叠的祖先组；
            # 那不是用户操作，不能写回元数据，否则折叠状态会被改坏。
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
        self._editor_loading = True
        self._hide_editor_state()
        self._show_editor_page(True)
        self.selected_label.setText("未选择步骤")
        self.template_preview.setPixmap(QPixmap())
        self.template_preview.setText("无模板预览")
        self.template_preview.setVisible(False)
        self.editor_actions.setVisible(False)
        # 没选步骤时不该显示"步骤名称 / 启用状态 / 多模板时"
        self.name_block.setVisible(False)
        for editor in (self.description_edit, self.template_edit, self.threshold_edit, self.timeout_edit, self.retry_count_edit, self.retry_interval_edit, self.ocr_text_edit, self.ocr_confidence_edit, self.after_wait_edit):
            editor.clear()
        self.click_until_template_edit.clear()
        self.click_until_interval_edit.clear()
        self.click_until_stop_delay_edit.clear()
        self.click_until_timeout_edit.clear()
        self.click_until_continue_checkbox.setChecked(False)
        self.click_until_stop_on_change_checkbox.setChecked(False)
        self.click_checkbox.setChecked(False)
        # 取消 OCR 勾选（toggled 会把「目标文字」等选项一起隐藏）
        self.ocr_checkbox.setChecked(False)
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
            # 级联到**所有后代分组**：父组勾选框代表整棵子树（此前只改了直属步骤，
            # 子组里的步骤会保持原状态，导致"取消勾选父组但子组步骤还在跑"）。
            metadata = self.mode_group_metadata.get(self.mode_combo.currentText() or "custom", {})
            affected = {group_id} | {str(gid) for gid in self._group_descendants(metadata, group_id)}
            for task in TASKS:
                if str(task.get("group_id") or "group_default") in affected:
                    task["enabled"] = checked
            self.task_list.blockSignals(True)
            try:
                for child_gid, child_item in self._group_items().items():
                    if child_gid in affected and child_gid != group_id:
                        child_item.setCheckState(
                            0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            finally:
                self.task_list.blockSignals(False)
            self._schedule_task_list_commit()



    @Slot()
    def persist_task_order(self):
        """按树里的显示顺序重排 TASKS。

        原实现只遍历"顶层组 → 直接子项"两层：嵌套组里的步骤不在 ordered_ids 里，
        一旦这个钩子被触发就会把深层步骤**丢掉**。现在递归遍历所有层级，
        并把树里没出现的步骤保留在末尾（宁可顺序不对，也不能吞步骤）。
        """
        self._push_history()
        ordered_ids = []

        def walk(parent):
            count = parent.childCount() if hasattr(parent, "childCount") else parent.topLevelItemCount()
            for position in range(count):
                child = parent.child(position) if hasattr(parent, "child") else parent.topLevelItem(position)
                if child.data(0, Qt.UserRole) == "group":
                    walk(child)
                else:
                    ordered_ids.append(str(child.data(0, Qt.UserRole)))

        walk(self.task_list)
        task_by_id = {str(task.get("id", index)): task for index, task in enumerate(TASKS)}
        ordered = [task_by_id[task_id] for task_id in ordered_ids if task_id in task_by_id]
        kept = {id(task) for task in ordered}
        ordered.extend(task for task in TASKS if id(task) not in kept)
        TASKS[:] = ordered
        self.current_task_index = -1
        self.save_current_tasks()
        self.refresh_task_list()

    def _drag_task_indices(self, source_index):
        """拖动时实际要移动的步骤下标。

        Qt 拖拽信号只带「按下的那个步骤」；如果它是多选里的一员，就把所有选中步骤
        作为一个块一起移动（保持 TASKS 里的顺序），否则只移动它自己。
        """
        if not (0 <= int(source_index) < len(TASKS)):
            return []
        # 优先用拖拽部件在按下时记录的选择（拖拽过程中 Qt 可能把选择集折叠成单个）
        recorded = list(getattr(self.task_list, "drag_task_indices", None) or [])
        recorded = [int(index) for index in recorded if 0 <= int(index) < len(TASKS)]
        if len(recorded) > 1 and int(source_index) in recorded:
            return sorted(recorded)
        selected = self._selected_task_indices()
        if int(source_index) in selected and len(selected) > 1:
            return sorted(selected)
        return [int(source_index)]

    def _move_task_block(self, indices, insert_before, group_id, log_text):
        """把多个步骤作为连续块插到 `insert_before`（原列表位置）并统一设置所属组。

        调用方给的 insert_before 是「移动前列表」里的位置；这里换算成移除后的位置。
        块内顺序保持 TASKS 原顺序（与用户看到的选择顺序一致）。
        """
        indices = sorted({int(index) for index in indices if 0 <= int(index) < len(TASKS)})
        if not indices:
            return
        self._push_history()
        old_index_by_id = {str(task.get("id")): index for index, task in enumerate(TASKS)}
        removed = set(indices)
        moving = [TASKS[index] for index in indices]
        remaining = [task for index, task in enumerate(TASKS) if index not in removed]
        target = len(TASKS) if insert_before is None else int(insert_before)
        position = target - sum(1 for index in indices if index < target)
        position = max(0, min(position, len(remaining)))
        group_id = str(group_id) if group_id else None
        metadata = self.mode_group_metadata.get(self.mode_combo.currentText() or "custom", {})
        group_names = metadata.get("names") or {}
        for task in moving:
            if group_id is None:
                task.pop("group_id", None)
                task.pop("group_name", None)
            else:
                task["group_id"] = group_id
                task["group_name"] = str(group_names.get(group_id, group_id))
        TASKS[:] = remaining[:position] + moving + remaining[position:]
        remap_jump_targets(TASKS, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in TASKS]))
        self.current_task_index = position
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(position)
        for offset in range(1, len(moving)):
            item = self._find_task_item(position + offset)
            if item is not None:
                item.setSelected(True)
        self.append_log(log_text)

    @Slot(int, int, bool)
    def reorder_task_from_tree(self, source_index, target_index, insert_after, target_group=None):
        indices = self._drag_task_indices(source_index)
        if not indices or target_index in indices or not (0 <= target_index < len(TASKS)):
            return
        target_key = str(target_group) if target_group else None
        insert_before = target_index + (1 if insert_after else 0)
        self._move_task_block(indices, insert_before, target_key,
                              f"已把 {len(indices)} 个步骤移到目标位置。" if len(indices) > 1
                              else "已把步骤移到目标位置。")

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
        """组及其全部后代（带环检测：元数据被写坏成环时不至于递归到栈溢出）。"""
        children = metadata.get("children", {}) if isinstance(metadata, dict) else {}
        out = []
        stack = [str(group_id)]
        seen = set()
        while stack:
            current = stack.pop(0)
            if current in seen:
                continue
            seen.add(current)
            out.append(current)
            stack.extend(str(child) for child in (children.get(current) or []))
        return out

    @Slot()
    def select_all_tasks(self):
        self._push_history()
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
        self._push_history()
        for task in TASKS:
            task["enabled"] = False
        self.save_current_tasks()
        self.refresh_task_list()
        self.append_log("已停用全部步骤。")

    def _group_member_indices(self, metadata, group_id):
        """该组（含全部后代组）在 TASKS 里的下标。"""
        member_ids = {str(gid) for gid in self._group_descendants(metadata, group_id)}
        return [index for index, task in enumerate(TASKS)
                if str(task.get("group_id") or "group_default") in member_ids]

    def _rebuild_tasks_with_block(self, block_indices, insert_at, old_index_by_id):
        """把 block_indices 的步骤整块搬到 insert_at（相对"去掉块之后"的列表），再重编号跳转。"""
        block_tasks = [TASKS[index] for index in block_indices]
        rest = [task for index, task in enumerate(TASKS) if index not in set(block_indices)]
        insert_at = max(0, min(int(insert_at), len(rest)))
        TASKS[:] = rest[:insert_at] + block_tasks + rest[insert_at:]
        remap_jump_targets(TASKS, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in TASKS]))

    def move_group_block(self, group_id, target_index, insert_after):
        """把某个组（含后代）的步骤块移到 target_index 这个步骤的前/后。

        跨到别的组时同时改归属（等价于"把子组拖到那个组里的某一步旁边"）。
        这样**子组可以插到父组的两个步骤之间**，不再只能待在底部。
        """
        if not (0 <= target_index < len(TASKS)):
            return
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        names = metadata.setdefault("names", {})
        parents = metadata.setdefault("parents", {})
        children = metadata.setdefault("children", {})
        order = metadata.setdefault("order", [])
        group_id = str(group_id)
        if group_id not in names:
            return
        members = set(self._group_member_indices(metadata, group_id))
        if not members or target_index in members:
            return
        target_task = TASKS[target_index]
        target_group = str(target_task.get("group_id") or "group_default")
        current_parent = parents.get(group_id)
        current_parent = str(current_parent) if current_parent else None
        target_parent = target_group if target_group != "group_default" else None
        old_index_by_id = {str(task.get("id")): index for index, task in enumerate(TASKS)}
        block_indices = [index for index in range(len(TASKS)) if index in members]
        target_id = str(target_task.get("id"))
        if target_parent != current_parent:
            if target_parent and target_parent in self._group_descendants(metadata, group_id):
                return          # 不能挂到自己的后代里
            if current_parent:
                siblings = children.setdefault(current_parent, [])
                if group_id in siblings:
                    siblings.remove(group_id)
            else:
                if group_id in order:
                    order.remove(group_id)
            parents[group_id] = target_parent
            if target_parent:
                children.setdefault(target_parent, [])
                if group_id not in children[target_parent]:
                    children[target_parent].append(group_id)
            else:
                if group_id not in order:
                    order.append(group_id)
        rest = [task for index, task in enumerate(TASKS) if index not in members]
        target_position = next((position for position, task in enumerate(rest)
                                if str(task.get("id")) == target_id), None)
        if target_position is None:
            return
        insert_at = target_position + (1 if insert_after else 0)
        self._push_history()
        self._rebuild_tasks_with_block(block_indices, insert_at, old_index_by_id)
        self.save_current_tasks()
        self._save_presets()
        self.refresh_task_list()
        group_item = self._group_items().get(group_id)
        if group_item is not None:
            self.task_list.clearSelection()
            group_item.setSelected(True)
        self.append_log(f"已移动分组: {names.get(group_id, group_id)}")

    def _swap_group_blocks(self, metadata, group_id, other_id, put_self_first):
        """把两个组的步骤块在 TASKS 里对调（组排序 = 执行顺序）。"""
        self_indices = self._group_member_indices(metadata, group_id)
        other_indices = self._group_member_indices(metadata, other_id)
        if not self_indices and not other_indices:
            return
        old_index_by_id = {str(task.get("id")): index for index, task in enumerate(TASKS)}
        self_tasks = [TASKS[index] for index in self_indices]
        other_tasks = [TASKS[index] for index in other_indices]
        skip = set(self_indices) | set(other_indices)
        first = min(self_indices + other_indices)
        first_block, second_block = (self_tasks, other_tasks) if put_self_first else (other_tasks, self_tasks)
        new_tasks = []
        for index, task in enumerate(TASKS):
            if index == first:
                new_tasks.extend(first_block)
                new_tasks.extend(second_block)
            if index in skip:
                continue
            new_tasks.append(task)
        self._push_history()
        TASKS[:] = new_tasks
        remap_jump_targets(TASKS, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in TASKS]))

    def _nudge_group_across_steps(self, metadata, group_id, direction):
        """同层没有相邻组时，让组块越过父组自己的一个步骤（实现"子组插到两步之间"）。"""
        group_id = str(group_id)
        parents = metadata.get("parents") or {}
        parent = parents.get(group_id)
        parent = str(parent) if parent else "group_default"
        member_ids = {str(gid) for gid in self._group_descendants(metadata, group_id)}
        block = [index for index, task in enumerate(TASKS)
                 if str(task.get("group_id") or "group_default") in member_ids]
        if not block:
            return False
        sibling_indices = [index for index, task in enumerate(TASKS)
                           if str(task.get("group_id") or "group_default") == parent
                           and index not in set(block)]
        if direction < 0:
            candidates = [index for index in sibling_indices if index < min(block)]
            if not candidates:
                return False
            target_index = max(candidates)
            insert_after = False
        else:
            candidates = [index for index in sibling_indices if index > max(block)]
            if not candidates:
                return False
            target_index = min(candidates)
            insert_after = True
        self.move_group_block(group_id, target_index, insert_after)
        return True

    def _move_group(self, group_id, direction):
        """在同一层级内移动分组：元数据顺序 + TASKS 里的步骤块一起换，执行顺序才会跟着变。"""
        self._push_history()
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        parents = metadata.setdefault("parents", {})
        children = metadata.setdefault("children", {})
        parent = parents.get(group_id)
        siblings = children.setdefault(str(parent), []) if parent else metadata.setdefault("order", [])
        if group_id not in siblings:
            siblings.append(group_id)
        position = siblings.index(group_id)
        target = position + direction
        if not (0 <= target < len(siblings)):
            # 同层没有相邻的组可换：试着越过父组自己的步骤（子组可以排在两步之间）
            if self._nudge_group_across_steps(metadata, group_id, direction):
                return
            return
        other_id = siblings[target]
        self._swap_group_blocks(metadata, group_id, other_id, put_self_first=direction < 0)
        siblings[position], siblings[target] = siblings[target], siblings[position]
        self.save_current_tasks()
        self._save_presets()
        self.refresh_task_list()
        # 刷新会重建树项、清掉选中：把这一组重新选中，连续点「上移/下移」才不会失效
        group_item = self._group_items().get(group_id)
        if group_item is not None:
            self.task_list.clearSelection()
            group_item.setSelected(True)
            self.task_list.setCurrentItem(group_item)
        self.append_log(f"已移动分组: {metadata.get('names', {}).get(group_id, group_id)}")

    @Slot()
    def move_selected_item(self, direction):
        group_ids = self._selected_group_ids()
        indices = self._selected_task_indices()
        if group_ids and not indices:
            self._move_group(group_ids[0], direction)
            return
        if not indices:
            return
        index = indices[0]
        target = index + direction
        if not (0 <= target < len(TASKS)):
            return
        self._push_history()
        task = TASKS.pop(index)
        TASKS.insert(target, task)
        self.current_task_index = target
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(target)

    def _move_task_with_group(self, source_index, insert_index, group_id, log_text):
        """把步骤移到指定位置并（可选）改归属，统一做跳转重编号与刷新。"""
        if not (0 <= source_index < len(TASKS)):
            return
        self._push_history()
        old_index_by_id = {str(t.get("id")): i for i, t in enumerate(TASKS)}
        task = TASKS.pop(source_index)
        if group_id is not None:
            group_id = str(group_id)
            mode = self.mode_combo.currentText() or "custom"
            metadata = self.mode_group_metadata.get(mode, {})
            task["group_id"] = group_id
            task["group_name"] = str((metadata.get("names") or {}).get(group_id, group_id))
        elif str(task.get("group_id") or "") != "":
            # 拖到空白：移出分组，回到根层
            task.pop("group_id", None)
            task.pop("group_name", None)
        insert_index = max(0, min(int(insert_index), len(TASKS)))
        TASKS.insert(insert_index, task)
        remap_jump_targets(TASKS, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in TASKS]))
        self.current_task_index = insert_index
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(insert_index)
        self.append_log(log_text)

    @Slot(int, str)
    def move_task_before_group(self, source_index, group_id):
        """上到分组前面：拉到该组**前面**（作为同层级的兄弟，跟着组整体上下移动）。

        多选拖动时整块移动（保持选择顺序）。
        """
        indices = self._drag_task_indices(source_index)
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.get(mode, {})
        member_indices = self._group_member_indices(metadata, group_id)
        insert_before = min(member_indices) if member_indices else len(TASKS)
        parent_group = (metadata.get("parents") or {}).get(str(group_id))
        self._move_task_block(indices, insert_before, parent_group, "已把步骤移到组前面。")

    @Slot(int, str)
    def move_task_into_group(self, source_index, group_id):
        """上到分组后面：加入该组（放在该组所有步骤末尾，空组也能放进去）。

        多选拖动时整块加入（保持选择顺序）。
        """
        indices = self._drag_task_indices(source_index)
        members = [index for index, task in enumerate(TASKS)
                   if str(task.get("group_id") or "group_default") == str(group_id)]
        insert_before = (max(members) + 1) if members else len(TASKS)
        self._move_task_block(indices, insert_before, group_id, "已把步骤加入分组。")

    @Slot(int)
    def move_task_to_root(self, source_index):
        """上到空白：移到列表末尾（移出分组）。多选拖动时整块移动。"""
        indices = self._drag_task_indices(source_index)
        self._move_task_block(indices, len(TASKS), None, "已把步骤移出分组到根层。")

    @Slot(str, object)
    def reparent_group(self, group_id, parent_group_id):
        """拖动分组：挂到目标组下面（None=根层）；不允许挂到自己的后代里（会成环）。"""
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        names = metadata.setdefault("names", {})
        parents = metadata.setdefault("parents", {})
        children = metadata.setdefault("children", {})
        order = metadata.setdefault("order", [])
        group_id = str(group_id)
        parent = str(parent_group_id) if parent_group_id else None
        if group_id not in names:
            return
        if parent == group_id:
            return
        if parent:
            if parent not in names or parent in self._group_descendants(metadata, group_id):
                return
        self._push_history()
        current_parent = parents.get(group_id)
        current_siblings = children.setdefault(str(current_parent), []) if current_parent else order
        if group_id in current_siblings:
            current_siblings.remove(group_id)
        parents[group_id] = parent
        if parent:
            children.setdefault(parent, [])
            if group_id not in children[parent]:
                children[parent].append(group_id)
        else:
            if group_id not in order:
                order.append(group_id)
        # 执行顺序跟着走：把整块步骤移到目标组步骤块的末尾（或根层末尾）
        block_indices = self._group_member_indices(metadata, group_id)
        if block_indices:
            old_index_by_id = {str(task.get("id")): index for index, task in enumerate(TASKS)}
            block_tasks = [TASKS[index] for index in block_indices]
            rest = [task for index, task in enumerate(TASKS) if index not in set(block_indices)]
            if parent:
                target_indices = self._group_member_indices(metadata, parent)
                target_ids = {str(TASKS[index].get("id")) for index in target_indices}
                insert_at = len(rest)
                for position, task in enumerate(rest):
                    if str(task.get("id")) in target_ids:
                        insert_at = position + 1
            else:
                insert_at = len(rest)
            TASKS[:] = rest[:insert_at] + block_tasks + rest[insert_at:]
            remap_jump_targets(TASKS, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in TASKS]))
        self.save_current_tasks()
        self._save_presets()
        self.refresh_task_list()
        self.append_log(f"已移动分组: {names.get(group_id, group_id)}")

    @Slot()
    def add_group(self, parent_group_id=None):
        """新增分组；父组为空时建在根层，否则挂到该父组下面（右键「在此组中新增子组」）。"""
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        names = metadata.setdefault("names", {})
        children = metadata.setdefault("children", {})
        parent = str(parent_group_id) if parent_group_id else None
        if parent and parent not in names:
            parent = None
        self._push_history()
        group_id = f"group_{uuid.uuid4().hex[:8]}"
        # 唯一名称：此前用 len(names)+1，删过组之后会出现重名
        number = len(names) + 1
        while f"组 {number}" in names.values():
            number += 1
        names[group_id] = f"组 {number}"
        metadata.setdefault("colors", {})[group_id] = "#eab308"
        metadata.setdefault("parents", {})[group_id] = parent
        children[group_id] = []
        if parent:
            children.setdefault(parent, [])
            if group_id not in children[parent]:
                children[parent].append(group_id)
        else:
            metadata.setdefault("order", []).append(group_id)
        self._save_presets()
        self.refresh_task_list()
        self.append_log(f"已新增组: {names[group_id]}")

    @Slot()
    def add_task(self):
        types = ["normal", "loop", "keyboard_move", "key_press", "drag", "click_until_gone", "delay"]
        task_type, ok = QInputDialog.getItem(self, "新增步骤类型", "请选择步骤类型:", types, 0, False)
        if not ok:
            return
        self._push_history()
        group_ids = self._selected_group_ids()
        group_id = group_ids[0] if group_ids else None
        if group_id is None and 0 <= self.current_task_index < len(TASKS):
            # 选中的是组内步骤：新步骤跟它同组（此前会掉到根层，看起来像"加错地方"）
            group_id = TASKS[self.current_task_index].get("group_id")
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
        # M2：「应用修改」也要进撤销栈——先留改动前快照，确认真的改动了再压栈，
        # 避免"点一下应用但没改任何东西"也产生一条空历史。
        before_tasks = deepcopy(TASKS)
        was_dirty = bool(getattr(self, "_editor_dirty", False))
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
        if was_dirty and TASKS != before_tasks:
            current_snapshot = self._undo_snapshot()
            self._push_history((before_tasks, current_snapshot[1], current_snapshot[2]))
        self.save_current_tasks()
        self.refresh_task_list()
        self._set_editor_state(True)
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
        self._touch_saved_time()
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

    # ---------- 分组右键：重命名 / 改色 ----------

    def rename_group(self, group_id):
        """重命名分组（同时更新组内步骤的 group_name，保持导出/复制时一致）。"""
        if not group_id:
            return
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        names = metadata.setdefault("names", {})
        current = names.get(group_id, group_id)
        new_name, accepted = QInputDialog.getText(self, "重命名分组", "分组名称：", text=str(current))
        new_name = (new_name or "").strip()
        if not accepted or not new_name or new_name == current:
            return
        self._push_history()
        names[group_id] = new_name
        for task in TASKS:
            if str(task.get("group_id") or "group_default") == str(group_id):
                task["group_name"] = new_name
        self._save_presets()
        self.refresh_task_list()
        self.append_log(f"已重命名分组：{new_name}")

    def change_group_color(self, group_id):
        """更改分组颜色（分组标签背景色）。"""
        if not group_id:
            return
        from PySide6.QtGui import QColor

        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        colors = metadata.setdefault("colors", {})
        current = QColor(colors.get(group_id, self._group_default_color()))
        color = QColorDialog.getColor(current, self, "选择分组颜色")
        if not color.isValid():
            return
        self._push_history()
        colors[group_id] = color.name()
        self._save_presets()
        self.refresh_task_list()
        self.append_log(f"已更改分组颜色：{color.name()}")

    def delete_group_only(self, group_id):
        """仅删除分组本身：组内步骤移到上一级分组（没有上一级则回「默认分组」）。

        与「删除该组及其步骤」的区别：任务数量不变、顺序不变、跳转编号不用重算；
        子分组保留并挂到同一个上一级分组下面。
        """
        if not group_id:
            return
        if str(group_id) == "group_default":
            QMessageBox.information(self, "无法删除", "「默认分组」是步骤的根层，不能单独删除。")
            return
        answer = QMessageBox.question(
            self, "仅删除分组", "只删除这个分组，组内步骤移到上一级分组后继续保留，确定吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        parents = metadata.setdefault("parents", {})
        children_map = metadata.setdefault("children", {})
        names = metadata.setdefault("names", {})
        parent_id = parents.get(group_id)
        parent_id = str(parent_id) if parent_id else None
        if parent_id and str(parent_id) == str(group_id):
            parent_id = None
        # 子分组挂到上一级（保持原有先后）；父组为空时子组变顶层，要补登记 order
        for child_id in [str(child) for child in (children_map.get(group_id) or [])]:
            parents[child_id] = parent_id
            if parent_id:
                siblings = children_map.setdefault(parent_id, [])
                if child_id not in siblings:
                    siblings.append(child_id)
            else:
                order_list = metadata.setdefault("order", [])
                if child_id not in order_list:
                    order_list.append(child_id)
        if parent_id:
            children_map[parent_id] = [
                child for child in (children_map.get(parent_id) or [])
                if str(child) != str(group_id)
            ]
        # 清理该组自己的元数据
        for key in ("names", "colors", "parents", "children"):
            metadata.get(key, {}).pop(group_id, None)
        order = metadata.get("order") or []
        metadata["order"] = [item for item in order if str(item) != str(group_id)]
        # 步骤迁移：目标组为上一级；没有上一级则回隐式根层「默认分组」
        target_id = parent_id or "group_default"
        target_name = str(names.get(target_id) or ("根层" if target_id == "group_default" else target_id))
        moved = 0
        for task in TASKS:
            if str(task.get("group_id") or "group_default") != str(group_id):
                continue
            if target_id == "group_default":
                task.pop("group_id", None)
                task.pop("group_name", None)
            else:
                task["group_id"] = target_id
                task["group_name"] = target_name
            moved += 1
        self.save_current_tasks()
        self._save_presets()
        self.refresh_task_list()
        self.append_log(f"已删除分组（仅删组），{moved} 个步骤移到「{target_name}」。")

    # ---------- 任务列表右键菜单 ----------

    # ---------- 撤销 / 重做（主窗口任务列表编辑） ----------

    def _undo_snapshot(self):
        return (deepcopy(TASKS), deepcopy(self.mode_group_metadata), self.current_task_index)

    def _push_history(self, snapshot=None):
        """结构编辑前记录一次快照；撤销/重做恢复过程中不再记录，避免互相污染。

        `snapshot` 可选：调用方已经改完数据、但手里留着"改动前"的快照时传进来
        （例如「应用修改」要先算完字段才知道有没有变化）。
        """
        if getattr(self, "_restoring_history", False):
            return
        self.history.append(snapshot if snapshot is not None else self._undo_snapshot())
        if len(self.history) > 50:
            self.history.pop(0)
        self.redo_history.clear()

    def _restore_snapshot(self, snapshot):
        tasks, metadata, index = snapshot
        self._restoring_history = True
        try:
            TASKS[:] = deepcopy(tasks)
            self.mode_group_metadata.clear()
            self.mode_group_metadata.update(deepcopy(metadata))
            self.current_task_index = index if 0 <= index < len(TASKS) else -1
            self.save_current_tasks()
            self._save_presets()
            self.refresh_task_list()
            if self.current_task_index >= 0:
                self.select_task_index(self.current_task_index)
            else:
                self.clear_editor()
        finally:
            self._restoring_history = False

    @Slot()
    def undo(self, *_args):
        if not self.history:
            self.append_log("没有可撤销的操作。")
            return
        self.redo_history.append(self._undo_snapshot())
        self._restore_snapshot(self.history.pop())
        self.append_log("已撤销上一步编辑。")

    @Slot()
    def redo(self, *_args):
        if not self.redo_history:
            self.append_log("没有可重做的操作。")
            return
        self.history.append(self._undo_snapshot())
        self._restore_snapshot(self.redo_history.pop())
        self.append_log("已重做上一步编辑。")

    def _build_task_list_menu(self, context_item=None):
        """构建「项目列表」的右键菜单（右键弹出与测试共用同一份）。

        三种上下文：
          · 右键在**步骤**上 → 复制 / 删除 / 上移 / 下移
          · 右键在**分组**上 → 组设置 / 重命名组 / 更改组颜色 / 折叠展开 / 删除组 …
          · 右键在**空白**处 → 只有创建类动作（编辑类因为没目标自动禁用）
        """
        menu = QMenu(self)
        undo_action = menu.addAction("撤销", self.undo)
        undo_action.setEnabled(bool(getattr(self, "history", None)))
        redo_action = menu.addAction("重做", self.redo)
        redo_action.setEnabled(bool(getattr(self, "redo_history", None)))
        menu.addSeparator()
        is_group = context_item is not None and context_item.data(0, Qt.UserRole) == "group"
        group_id = str(context_item.data(0, Qt.UserRole + 1)) if is_group else None
        selected = self._selected_task_indices()

        if is_group:
            # 选中该组后右侧会切到「组设置」面板
            menu.addAction("编辑组设置", lambda: self._load_editor_for_group(group_id))
            menu.addAction("重命名分组…", lambda: self.rename_group(group_id))
            menu.addAction("更改分组颜色…", lambda: self.change_group_color(group_id))
            expanded = bool(context_item.isExpanded())
            menu.addAction("折叠该组" if expanded else "展开该组",
                           lambda: context_item.setExpanded(not expanded))
            menu.addSeparator()
            menu.addAction("在此组中新增步骤", self.add_task)
            menu.addAction("在此组中新增子组", lambda: self.add_group(group_id))
            # 两种删除语义（所有者 2026-10-26）：连步骤一起删 / 只删组、步骤移到上一级
            delete_all = menu.addAction("删除该组及其步骤…", self.delete_selected_item)
            delete_only = menu.addAction("仅删除该组（步骤移到上一级）…",
                                         lambda: self.delete_group_only(group_id))
            is_default_group = group_id == "group_default"
            delete_all.setEnabled(not is_default_group)
            delete_only.setEnabled(not is_default_group)
            if is_default_group:
                delete_all.setToolTip("「默认分组」是步骤根层，不能删除")
            menu.addSeparator()
        else:
            menu.addAction("新增步骤", self.add_task)
            menu.addAction("新增组", self.add_group)
            menu.addSeparator()
            copy_action = menu.addAction("复制", self.copy_selected_item)
            paste_action = menu.addAction("粘贴", self.paste_clipboard_item)
            paste_action.setEnabled(bool(getattr(self, "task_clipboard", None)))
            delete_action = menu.addAction("删除", self.delete_selected_item)
            up_action = menu.addAction("上移", lambda: self.move_selected_item(-1))
            down_action = menu.addAction("下移", lambda: self.move_selected_item(1))
            for action in (copy_action, delete_action, up_action, down_action):
                action.setEnabled(bool(selected))
            menu.addSeparator()

        menu.addAction("全部启用", self.select_all_tasks)
        clear_action = menu.addAction("全部停用", self.clear_tasks)
        clear_action.setEnabled(bool(TASKS))
        menu.addSeparator()
        menu.addAction("打开蓝图流程", self.open_blueprint)
        return menu

    def _show_empty_list_menu(self, position):
        """空列表引导页的右键菜单（与列表空白处同一份菜单，含新增步骤/新增组）。"""
        self._build_task_list_menu(None).exec(self.task_empty_page.mapToGlobal(position))

    def show_task_list_menu(self, position):
        """右键：先按被点的对象选中它（步骤 → 复制/删除作用于它；分组 → 右侧切到组设置），再弹菜单。"""
        item = self.task_list.itemAt(position)
        if item is not None:
            self.task_list.setCurrentItem(item)
        self._build_task_list_menu(item).exec(self.task_list.viewport().mapToGlobal(position))

    # ---------- 步骤片段（F13）：保存 / 插入 / 删除常用步骤组 ----------

    @Slot()
    def open_snippet_menu(self):
        """「片段」按钮：保存选中步骤为片段、插入片段、删除片段。"""
        from PySide6.QtGui import QCursor

        menu = QMenu(self)
        menu.addAction("保存选中步骤为片段…", self.save_selection_as_snippet)
        insert_action = menu.addAction("插入片段…", self.insert_snippet_at_selection)
        delete_action = menu.addAction("删除片段…", self.delete_snippet)
        has_snippets = bool(getattr(self, "snippets", None))
        insert_action.setEnabled(has_snippets)
        delete_action.setEnabled(has_snippets)
        menu.exec(QCursor.pos())

    def save_selection_as_snippet(self):
        indices = self._selected_task_indices()
        group_ids = self._selected_group_ids()
        if not indices and not group_ids:
            QMessageBox.warning(self, "无法保存", "请先在左侧列表里选择一个或多个步骤或分组。")
            return
        name, accepted = QInputDialog.getText(self, "保存为片段", "片段名称：", text="")
        name = (name or "").strip()
        if not accepted or not name:
            return
        if name in self.snippets:
            answer = QMessageBox.question(self, "覆盖片段", f"片段「{name}」已存在，是否覆盖？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.get(mode, {})
        # 选中分组时会把组内步骤（含子组）全部收进片段，分组结构一起保存
        snippet = snippet_store.extract_snippet(TASKS, indices, metadata, group_ids)
        if not snippet.get("tasks") and not snippet.get("groups"):
            QMessageBox.warning(self, "无法保存", "选中的内容里没有步骤或分组。")
            return
        # 同时选了"步骤 + 一些空组"时，空组大概率是误选/失效引用：不写进片段，日志说明
        if snippet.get("tasks") and snippet.get("groups"):
            used_groups = {str(task.get("group_id")) for task in snippet["tasks"] if task.get("group_id")}
            keep = set()
            for group_id in used_groups:
                current = group_id
                seen = set()
                while current and current in snippet["groups"] and current not in seen:
                    seen.add(current)
                    keep.add(current)
                    parent = (snippet["groups"].get(current) or {}).get("parent")
                    current = str(parent) if parent else ""
            dropped = [group_id for group_id in snippet["groups"] if group_id not in keep]
            if dropped:
                snippet["groups"] = {gid: info for gid, info in snippet["groups"].items() if gid in keep}
                snippet["group_order"] = [gid for gid in snippet.get("group_order", []) if gid in keep]
                self.append_log(
                    f"保存片段时忽略了 {len(dropped)} 个没有步骤的分组（避免插入后出现空组）。")
        # 只选了一个步骤、且没有显式选组：存成"纯步骤"片段，不带上它所在的分组
        # （否则插入单步骤片段时会自动多出一个只有一步的组）。
        if not group_ids and len(snippet.get("tasks") or []) == 1:
            for task in snippet["tasks"]:
                task.pop("group_id", None)
                task.pop("group_name", None)
                task.pop("group_color", None)
            snippet["groups"] = {}
            snippet["group_order"] = []
            self.append_log("单步骤片段不保存分组结构（避免插入时自动建组）。")
        self.snippets[name] = snippet
        self._save_presets()
        self.append_log(
            f"已保存片段「{name}」：{snippet_store.snippet_summary(snippet)}"
            "（分组会一起保存；片段内部的跳转保留，指向片段外的跳转会被丢弃）"
        )

    def insert_snippet_at_selection(self):
        names = sorted(getattr(self, "snippets", {}) or {})
        if not names:
            QMessageBox.warning(self, "没有片段", "还没有保存过片段。先选中若干步骤或分组，再用「片段 → 保存选中步骤为片段」。")
            return
        name, accepted = QInputDialog.getItem(self, "插入片段", "选择片段：", names, 0, False)
        if not accepted or not name:
            return
        snippet = self.snippets.get(name)
        if not snippet:
            return
        snippet_tasks, _groups, _order = snippet_store.normalize_payload(snippet)
        if not snippet_tasks and not _groups:
            return
        if not snippet_tasks:
            # 只有分组、没有步骤的片段：建出分组结构（此前直接 return，用户以为功能坏了）
            self.append_log(f"片段「{name}」只含分组（{len(_groups)} 个），将只创建分组结构。")
        # 插入位置：选中步骤 → 紧接在它后面；选中分组 → 接在该组最后一个步骤后面
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.get(mode, {})
        selected_indices = self._selected_task_indices()
        selected_groups = self._selected_group_ids()
        parent_group = None
        if selected_indices:
            anchor = max(selected_indices)
            position = anchor + 1
            anchor_task = TASKS[anchor] if anchor < len(TASKS) else {}
            parent_group = str(anchor_task.get("group_id")) if anchor_task.get("group_id") else None
        elif selected_groups:
            # 多选了多个组时选"最外层"的那个（不会把组塞进它自己的后代里），
            # 仍是多个候选就按组内第一个步骤的位置取最靠前的——保证结果稳定可预期。
            candidates = [str(gid) for gid in selected_groups]

            def is_inside(other, gid):
                return str(other) != str(gid) and str(gid) in self._group_descendants(metadata, other)

            outermost = [gid for gid in candidates
                         if not any(is_inside(other, gid) for other in candidates)] or candidates

            def first_member_index(gid):
                members = self._group_member_indices(metadata, gid)
                return min(members) if members else len(TASKS)

            parent_group = sorted(outermost, key=first_member_index)[0]
            if len(candidates) > 1:
                self.append_log(
                    f"插入了 {len(candidates)} 个分组的选择，按最外层/最前原则使用「"
                    f"{metadata.get('names', {}).get(parent_group, parent_group)}」作为父组。")
            members = self._group_member_indices(metadata, parent_group)
            position = (max(members) + 1) if members else len(TASKS)
        else:
            position = len(TASKS)
        new_tasks, new_groups = snippet_store.insert_snippet(
            TASKS, snippet, position, parent_group_id=parent_group)
        inserted = len(new_tasks) - len(TASKS)
        self._push_history()
        TASKS[:] = new_tasks
        # 片段里的分组登记进当前预设（新 id，顶层组挂到插入点所在的组下面）
        if new_groups:
            metadata = self.mode_group_metadata.setdefault(mode, {})
            names_map = metadata.setdefault("names", {})
            colors_map = metadata.setdefault("colors", {})
            parents_map = metadata.setdefault("parents", {})
            children_map = metadata.setdefault("children", {})
            order = metadata.setdefault("order", [])
            taken_names = set(str(name) for name in names_map.values())

            def unique_group_name(base_name):
                """插入同名片段时自动加「副本」/编号，避免出现两个一模一样的组名。"""
                candidate = str(base_name or "分组")
                if candidate in taken_names:
                    candidate = f"{candidate} 副本"
                index = 2
                while candidate in taken_names:
                    candidate = f"{base_name} 副本{index}"
                    index += 1
                taken_names.add(candidate)
                return candidate

            renamed = {}
            for group in new_groups:
                group_id = group["id"]
                renamed[group_id] = unique_group_name(group["name"])
                names_map[group_id] = renamed[group_id]
                colors_map[group_id] = group["color"] or self._group_default_color()
                parents_map[group_id] = group["parent"]
                children_map.setdefault(group_id, [])
                parent = group["parent"]
                if parent:
                    children_map.setdefault(parent, [])
                    if group_id not in children_map[parent]:
                        children_map[parent].append(group_id)
                else:
                    order.append(group_id)
            # 插入的步骤里写的 group_name 要跟着改名后的组名走
            for task in TASKS:
                task_group = str(task.get("group_id") or "")
                if task_group in renamed:
                    task["group_name"] = renamed[task_group]
            # 插进"收起的组"里时自动展开目标组链路，否则用户看不到插入结果
            expanded_map = metadata.setdefault("expanded", {})
            chain = []
            current = parent_group
            while current and str(current) not in chain:
                chain.append(str(current))
                current = (metadata.get("parents") or {}).get(str(current))
            for group_id in chain:
                expanded_map[group_id] = True
        self.current_task_index = position
        self.save_current_tasks()
        self._save_presets()
        self.refresh_task_list()
        self.select_task_index(position)
        group_note = f"，{len(new_groups)} 个分组" if new_groups else ""
        if parent_group:
            group_label = str(self.mode_group_metadata.get(mode, {}).get("names", {}).get(
                str(parent_group), parent_group))
            target_note = f"，插入到分组「{group_label}」下"
        else:
            target_note = "，插入到根层"
        self.append_log(
            f"已插入片段「{name}」：{inserted} 个步骤{group_note}{target_note}（插入点之后的跳转已自动后移）。")

    def delete_snippet(self):
        names = sorted(getattr(self, "snippets", {}) or {})
        if not names:
            return
        name, accepted = QInputDialog.getItem(self, "删除片段", "选择要删除的片段：", names, 0, False)
        if not accepted or not name or name not in self.snippets:
            return
        answer = QMessageBox.question(self, "删除片段", f"确定删除片段「{name}」吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.snippets.pop(name, None)
        self._save_presets()
        self.append_log(f"已删除片段「{name}」。")

    def _fallback_steps_for_mode(self, mode=None):
        """当前模式（预设）的「全局兜底处理步骤」副本（F7）。"""
        mode = mode or self.mode_combo.currentText() or "custom"
        steps = self.mode_fallback_steps.get(mode)
        return deepcopy(steps) if isinstance(steps, list) else []

    @Slot()
    def open_fallback_editor(self):
        """编辑「全局兜底处理」步骤（F7）：识别失败时先执行的通用恢复动作。

        复用「迂回」那套步骤列表编辑器（`_build_step_list_editor`）与单步配置对话框
        （`_configure_detour_step`），不做第二套编辑界面。
        """
        mode = self.mode_combo.currentText() or "custom"
        steps = self._fallback_steps_for_mode(mode)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"全局兜底处理 — {mode}")
        dialog.resize(560, 480)
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "识别失败（重试用完、且还没到迂回）时，按顺序执行下面的步骤；\n"
            "其中**任一步骤成功**就重新识别当前步骤。适合放「关闭弹窗 / 点击继续 / 等待加载」这类通用恢复动作。\n"
            "注意：每个步骤每次运行最多尝试一轮兜底，不会循环。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        _build_step_list_editor(
            dialog, steps, layout,
            lambda step: self._configure_detour_step(step, dialog),
        )
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(dialog.reject)
        buttons.addWidget(cancel_button)
        save_button = QPushButton("保存")
        buttons.addWidget(save_button)
        layout.addLayout(buttons)

        def save():
            self.mode_fallback_steps[mode] = deepcopy(steps)
            self._save_presets()
            self.append_log(f"已保存全局兜底处理：{len(steps)} 个步骤（{mode}）")
            dialog.accept()

        save_button.clicked.connect(save)
        dialog.exec()

    @Slot()
    def open_recognition_debug(self):
        """打开「识别调试」窗口（单例；独立窗口，可单独移动/关闭）。"""
        window = getattr(self, "recognition_debug_window", None)
        if window is not None:
            try:
                window.isVisible()          # 探测底层对象是否已被销毁
            except RuntimeError:
                window = None
        if window is None:
            window = RecognitionDebugWindow(self)
            self.recognition_debug_window = window
        window.show()
        window.raise_()
        window.activateWindow()
        window.refresh_from_snapshot()

    # ---------- 自动更新检查（F11） ----------

    @Slot()
    def check_updates(self):
        """「检查更新」按钮：手动检查（失败会弹提示，自动检查则不弹）。"""
        self.start_update_check(manual=True)

    def start_update_check(self, manual=False):
        """在后台线程检查 GitHub 最新版本（不阻塞界面，失败一律不抛异常）。"""
        runner = getattr(self, "_update_runner", None)
        if runner is not None and getattr(runner, "running", False):
            if manual:
                self.append_log("正在检查更新，请稍候…")
            return
        runner = UpdateCheckRunner(self)
        runner.running = True
        runner.finished.connect(lambda result: self._on_update_result(result, manual))
        self._update_runner = runner
        if manual:
            self.append_log(f"正在检查更新（当前版本 {config.APP_VERSION}）…")
        runner.start()

    def _on_update_result(self, result, manual=False):
        """检查结果回到 GUI 线程后的处理：有新版本就提示，失败按手动/自动区别对待。"""
        runner = getattr(self, "_update_runner", None)
        if runner is not None:
            runner.running = False
        if result.error:
            self.append_log(f"更新检查未完成：{result.error}")
            if manual:
                report_check_failure(self, result)
            return
        if result.has_update:
            self.append_log(f"发现新版本 {result.remote_version}（当前 {result.current_version}）："
                            f"{result.download_url}")
            self._offer_update(result)
        else:
            self.append_log(f"已是最新版本（{result.current_version}）。")

    def _offer_update(self, result):
        """有新版本：打包版且 Release 带 zip 时提供「一键自动更新」，否则沿用打开发布页。"""
        if not can_auto_update(result):
            notify_update(self, result)
            return
        choice = ask_update_choice(self, result)
        if choice == "auto":
            self._start_auto_update(result)
        elif choice == "page":
            notify_update(self, result)

    def _start_auto_update(self, result):
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.warning(self, "正在执行", "请先停止当前脚本，再安装更新。")
            return
        runner = getattr(self, "_update_download_runner", None)
        if runner is not None:
            self.append_log("更新包正在下载，请稍候…")
            return
        destination = os.path.join(
            tempfile.gettempdir(),
            "vf_update_" + time.strftime("%Y%m%d_%H%M%S") + ".zip")
        self._update_download_path = destination
        self._update_info = result
        self._update_download_logged = -10
        runner = UpdateDownloadRunner(self)
        runner.progress.connect(self._on_update_download_progress)
        runner.finished.connect(self._on_update_download_finished)
        self._update_download_runner = runner
        self.append_log(f"开始下载更新包：{result.asset_name or result.asset_url}")
        self._set_status("running", "下载更新")
        self._set_update_download_progress(0, 0)   # 底部进度条改为「下载进度」
        runner.start(result.asset_url, destination)

    def _set_update_download_progress(self, downloaded, total):
        """把底部进度条临时切换成更新包下载进度（下载结束后恢复步骤进度）。"""
        bar = getattr(self, "progress_bar", None)
        if bar is None:
            return
        megabytes = downloaded / 1024 / 1024
        if total and total > 0:
            percent = min(100, int(downloaded * 100 / total))
            bar.setRange(0, 100)
            bar.setValue(percent)
            bar.setFormat(f"更新包下载 {percent}%（{megabytes:.1f}/{total / 1024 / 1024:.1f} MB）")
        else:
            bar.setRange(0, 0)          # 总大小未知：不确定进度动画
            bar.setFormat(f"更新包下载中… {megabytes:.1f} MB")

    def _restore_step_progress(self):
        """下载结束后把底部进度条恢复成「步骤进度」。"""
        bar = getattr(self, "progress_bar", None)
        if bar is not None:
            self._refresh_idle_progress()

    def _on_update_download_progress(self, downloaded, total):
        self._set_update_download_progress(downloaded, total)
        if total:
            percent = int(downloaded * 100 / total)
            if percent < self._update_download_logged + 10 and percent < 100:
                return
            self._update_download_logged = percent
            self.append_log(f"  更新包下载 {percent}%（{downloaded / 1024 / 1024:.1f}/"
                            f"{total / 1024 / 1024:.1f} MB）")
        else:
            if downloaded // (5 * 1024 * 1024) > self._update_download_logged:
                self._update_download_logged = downloaded // (5 * 1024 * 1024)
                self.append_log(f"  更新包已下载 {downloaded / 1024 / 1024:.1f} MB")

    def _on_update_download_finished(self, outcome):
        self._update_download_runner = None
        self._set_status("idle")
        if outcome.get("error"):
            self.append_log(f"更新包下载失败：{outcome['error']}")
            self._restore_step_progress()
            QMessageBox.warning(self, "下载失败",
                                f"更新包下载失败：\n{outcome['error']}\n\n也可以打开发布页手动下载。")
            return
        zip_path = outcome.get("path")
        self.append_log(f"更新包下载完成：{zip_path}")
        try:
            stage_root = tempfile.mkdtemp(prefix="vf_update_stage_")
            update_root = extract_update_zip(zip_path, stage_root)
        except RuntimeError as exc:
            self.append_log(f"更新包校验失败：{exc}")
            self._restore_step_progress()
            QMessageBox.warning(self, "更新包无效", str(exc))
            return
        version = getattr(self._update_info, "remote_version", "")
        answer = QMessageBox.question(
            self, "更新已下载",
            f"新版本 {version} 已下载并校验。\n\n"
            "现在重启并安装吗？（程序会退出，替换完成后自动重新打开；"
            "用户步骤/预设等数据会保留）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            self.append_log(f"已取消安装；更新包保留在 {zip_path}")
            self._restore_step_progress()
            return
        self._install_update(update_root, zip_path)

    def _install_update(self, update_root, zip_path=None):
        install_dir = paths.app_root()
        try:
            helper = build_update_helper(install_dir, update_root, os.getpid(), zip_path=zip_path)
        except OSError as exc:
            QMessageBox.warning(self, "更新失败", f"无法创建更新脚本：{exc}")
            return
        if hasattr(self, "progress_bar"):
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("正在安装更新…")
        self.append_log("即将退出并安装更新，完成后会自动重新打开程序…")
        QApplication.processEvents()
        try:
            launch_update_helper(helper)
        except OSError as exc:
            QMessageBox.warning(self, "更新失败", f"无法启动更新脚本：{exc}")
            return
        self.close()
        QApplication.instance().quit()
        # 兜底：万一关闭/quit 被别的事件卡住，0.8 秒后强制退出，确保更新助手能继续
        QTimer.singleShot(800, lambda: os._exit(0))

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
            snippets_provider=lambda: self.snippets,
            snippets_save=self._save_presets,
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

    # ---------- L3 状态胶囊 / V4 运行进度 / V5 空状态 / O8 关于说明 ----------

    _STATUS_META = {
        "idle": ("●", "待机"),
        "running": ("▶", "运行中"),
        "paused": ("⏸", "已暂停"),
        "error": ("⚠", "异常"),
        "done": ("✔", "已完成"),
        "stopped": ("■", "已停止"),
    }

    def _set_status(self, kind, detail=""):
        """更新底部状态胶囊：颜色由 QSS 的 statusKind 属性控制。"""
        kind = kind if kind in self._STATUS_META else "idle"
        icon, label = self._STATUS_META[kind]
        suffix = f"（{detail}）" if detail else ""
        self.status_kind = kind
        self.status_text = label
        self.status_label.setText(f"{icon} {label}{suffix}")
        self.status_label.setProperty("statusKind", kind)
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)

    def _update_preset_info(self):
        if hasattr(self, "preset_info_label"):
            mode = self.mode_combo.currentText() or "custom"
            self.preset_info_label.setText(f"预设: {mode}")

    def _touch_saved_time(self):
        if hasattr(self, "last_save_label"):
            self.last_save_label.setText(f"保存于 {time.strftime('%H:%M:%S')}")

    def _show_editor_page(self, empty):
        """空编辑器提示：在编辑区顶部显示「选择一个步骤」，编辑区本身保持可见。"""
        if hasattr(self, "editor_empty_hint"):
            self.editor_empty_hint.setVisible(empty)

    def _sync_empty_states(self):
        if hasattr(self, "task_stack"):
            self.task_stack.setCurrentIndex(0 if TASKS else 1)
        if hasattr(self, "_apply_execution_marks"):
            self._apply_execution_marks()
        self._refresh_idle_progress()

    def _refresh_idle_progress(self):
        """空闲时把进度条显示为 0 / 启用步数；运行中不要覆盖实际进度。"""
        if not hasattr(self, "progress_bar"):
            return
        running = bool(getattr(self, "worker_thread", None) and self.worker_thread.isRunning())
        if running:
            return
        total = max(1, len([task for task in TASKS if task.get("enabled", True)]))
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m 步")

    _EXEC_STATE_STYLE = {
        # id 状态 -> (背景色, 前景色, 是否加粗)
        "running": ("#dbeafe", "", True),
        "success": ("", "#15803d", False),
        "skipped": ("", "#15803d", False),
        "failed": ("#fee2e2", "#b91c1c", False),
        "timeout": ("#fee2e2", "#b91c1c", False),
        "error": ("#fee2e2", "#b91c1c", False),
    }

    def _apply_execution_marks(self):
        """按 execution_states 重绘步骤行颜色（列表刷新重建 item 后也要恢复）。

        注意：给 QTreeWidgetItem 设字体/颜色会触发 itemChanged 信号，进而可能被当成
        "勾选状态变化"走刷新/提交，运行标记刷新时必须屏蔽。
        """
        states = getattr(self, "execution_states", None) or {}
        self.task_list.blockSignals(True)
        try:
            self._apply_execution_marks_unlocked(states)
        finally:
            self.task_list.blockSignals(False)

    def _apply_execution_marks_unlocked(self, states):
        for index in range(len(TASKS)):
            item = self._find_task_item(index)
            if item is None:
                continue
            style = self._EXEC_STATE_STYLE.get(states.get(str(TASKS[index].get("id"))))
            background = QBrush(QColor(style[0])) if style and style[0] else QBrush()
            foreground = QBrush(QColor(style[1])) if style and style[1] else QBrush()
            item.setBackground(0, background)
            item.setForeground(0, foreground)
            font = item.font(0)
            font.setBold(bool(style and style[2]))
            item.setFont(0, font)

    def _new_task_item_by_id(self, task_id):
        task_id = str(task_id)
        for index in range(len(TASKS)):
            item = self._find_task_item(index)
            if item is not None and str(item.data(0, Qt.UserRole + 1)) is not None:
                index_value = item.data(0, Qt.UserRole + 1)
                if 0 <= int(index_value) < len(TASKS) and str(TASKS[int(index_value)].get("id")) == task_id:
                    return item, int(index_value)
        return None, None

    def _begin_run_progress(self):
        """开始执行：重置行标记、显示进度条（总步数=启用的步骤数）。"""
        self._run_total = max(1, len([task for task in TASKS if task.get("enabled", True)]))
        self._run_done = 0
        self.task_list.blockSignals(True)
        try:
            for index in range(len(TASKS)):
                item = self._find_task_item(index)
                if item is None:
                    continue
                item.setBackground(0, QBrush())
                item.setForeground(0, QBrush())
                font = item.font(0)
                font.setBold(False)
                item.setFont(0, font)
        finally:
            self.task_list.blockSignals(False)
        self.progress_bar.setRange(0, self._run_total)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m 步")
        self.progress_bar.setVisible(True)

    def _mark_running_task(self, task_id):
        item, index = self._new_task_item_by_id(task_id)
        if item is None:
            return None
        self._apply_execution_marks()
        enabled_ids = [str(task.get("id")) for task in TASKS if task.get("enabled", True)]
        position = enabled_ids.index(str(task_id)) + 1 if str(task_id) in enabled_ids else 1
        if hasattr(self, "progress_bar"):
            self.progress_bar.setValue(max(1, position))
        self._set_status("running", f"第 {position}/{getattr(self, '_run_total', len(enabled_ids))} 步")
        return index

    def _mark_step_result(self, task_id, state):
        self._apply_execution_marks()

    # ---------- M1：编辑状态（未应用 / 已保存） ----------

    _DIRTY_LINE_EDITS = (
        "description_edit", "template_edit", "threshold_edit", "timeout_edit",
        "retry_count_edit", "retry_interval_edit", "after_wait_edit",
        "click_x_edit", "click_y_edit", "next_template_edit",
        "click_until_template_edit", "click_until_interval_edit",
        "click_until_stop_delay_edit", "click_until_timeout_edit",
        "ocr_text_edit", "ocr_confidence_edit",
    )
    _DIRTY_CHECKBOXES = (
        "enabled_checkbox", "click_checkbox", "optional_checkbox",
        "match_required_checkbox", "click_until_continue_checkbox",
        "click_until_stop_on_change_checkbox", "ocr_checkbox", "ocr_case_checkbox",
    )
    _DIRTY_COMBOS = ("match_strategy_combo", "wait_for_combo", "ocr_match_combo")

    def _connect_editor_dirty_signals(self):
        """用户动过编辑器的任何字段 → 状态变「未应用」。"""
        for name in self._DIRTY_LINE_EDITS:
            widget = getattr(self, name, None)
            if widget is not None:
                widget.textEdited.connect(lambda *_: self._mark_editor_dirty())
        for name in self._DIRTY_CHECKBOXES:
            widget = getattr(self, name, None)
            if widget is not None:
                widget.toggled.connect(lambda *_: self._mark_editor_dirty())
        for name in self._DIRTY_COMBOS:
            widget = getattr(self, name, None)
            if widget is not None:
                widget.currentIndexChanged.connect(lambda *_: self._mark_editor_dirty())

    def _set_editor_state(self, saved, visible=True):
        """更新「未应用 / 已保存」胶囊（QSS 用 stateKind 上色）。"""
        label = getattr(self, "editor_state_label", None)
        if label is None:
            return
        label.setVisible(visible)
        if not visible:
            return
        kind = "saved" if saved else "dirty"
        label.setText("已保存" if saved else "未应用")
        label.setToolTip("字段已应用并保存" if saved
                         else "有改动尚未应用：点「应用修改」后才会写入步骤")
        label.setProperty("stateKind", kind)
        self._editor_dirty = not saved
        style = label.style()
        style.unpolish(label)
        style.polish(label)

    def _hide_editor_state(self):
        label = getattr(self, "editor_state_label", None)
        if label is not None:
            label.setVisible(False)

    def _mark_editor_dirty(self):
        if getattr(self, "_editor_loading", False):
            return
        if not 0 <= self.current_task_index < len(TASKS):
            return
        label = getattr(self, "editor_state_label", None)
        if label is None or not label.isVisible():
            return
        self._set_editor_state(False)

    def clear_log(self):
        """清空日志面板（M6：日志有上限，也给一个手动清空入口）。"""
        self.log_box.clear()

    # ---------- L3：程序内「设置」（目前是检查更新的代理） ----------

    def _create_settings_dialog(self):
        """构建设置对话框（测试直接调用它检查预填/保存，不弹模态）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("设置")
        dialog.resize(520, 220)
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "检查更新需要访问 GitHub。\n"
            "如果网络需要代理，在这里填写（例如 http://127.0.0.1:7892）。\n"
            "留空 = 自动探测：环境变量 → Windows 系统代理 → 直连。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        edit = QLineEdit(settings_store.get_update_proxy())
        edit.setPlaceholderText("例如 http://127.0.0.1:7892（留空=自动）")
        layout.addWidget(edit)
        path_label = QLabel(f"设置文件：{settings_store.SETTINGS_FILE}")
        path_label.setObjectName("statusInfo")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path_label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(dialog.reject)
        save_button = QPushButton("保存")
        save_button.clicked.connect(lambda: self._save_update_proxy(edit, dialog))
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)
        layout.addLayout(buttons)
        dialog.proxy_edit = edit
        return dialog

    def open_settings_dialog(self):
        self._create_settings_dialog().exec()

    def _save_update_proxy(self, edit, dialog=None):
        value = (edit.text() or "").strip()
        try:
            settings_store.set_update_proxy(value)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", f"无法写入设置文件：{exc}")
            return
        if value:
            self.append_log(f"已保存更新代理：{value}（下次检查更新时生效）。")
        else:
            self.append_log("已清空更新代理：恢复自动探测（环境变量 / 系统代理 / 直连）。")
        if dialog is not None:
            dialog.accept()

    def show_about(self):
        data_dir = paths.data_dir()
        app_dir = paths.app_root()
        QMessageBox.about(
            self,
            "关于 VisionFlow Automator",
            f"VisionFlow Automator v{config.APP_VERSION}\n\n"
            f"程序目录：{app_dir}\n"
            f"数据目录：{data_dir}\n\n"
            "基于 PySide6 + OpenCV + PyAutoGUI 的视觉识别自动化脚本工具。",
        )

    def open_help(self):
        readme = os.path.join(paths.app_root(), "README.md")
        if os.path.exists(readme):
            QDesktopServices.openUrl(QUrl.fromLocalFile(readme))
        else:
            QMessageBox.information(self, "使用说明", f"未找到 README.md：{readme}")

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
        if hasattr(self, "refresh_window_button"):
            self.refresh_window_button.setEnabled(not running)
        # 进度条常驻状态栏（空闲显示 0 / N，运行中/结束后显示实际进度），不再隐藏
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
        index = self._mark_running_task(task_id)
        if index is not None:
            self.current_task_index = index
            item = self._find_task_item(index)
            if item is not None:
                self.task_list.setCurrentItem(item)
                self.task_list.scrollToItem(item, QTreeWidget.ScrollHint.PositionAtCenter)
                item.setSelected(True)

    @Slot(dict, str)
    def on_execution_result(self, task, state):
        task_id = str(task.get("id"))
        self.execution_states[task_id] = state
        if self.blueprint_window is not None:
            self.blueprint_window.update_execution_state(task_id, state)
        self._mark_step_result(task_id, state)
        self._run_done = getattr(self, "_run_done", 0) + 1
        if hasattr(self, "progress_bar"):
            self.progress_bar.setValue(max(self._run_done, min(self.progress_bar.value(), self._run_total)))
            self.progress_bar.setFormat("%v / %m 步")
        if state in ("failed", "timeout", "error"):
            self._set_status("error", f"第 {self._run_done}/{getattr(self, '_run_total', self._run_done)} 步失败")
        self.append_log(f"步骤结果: {task.get('description', task.get('template', '未命名'))} -> {state}")

    @Slot(str)
    def on_execution_completed(self, state):
        if self.completion_notified:
            return
        self.completion_notified = True
        if hasattr(self, "progress_bar"):
            self.progress_bar.setFormat("%v / %m 步")
        if state == "failed":
            self._set_status("error", "脚本异常")
            QMessageBox.warning(self, "脚本异常", "脚本执行过程中发生错误，请查看日志。")
        elif self.stop_event.is_set():
            self._set_status("stopped")
        else:
            self._set_status("done")
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


def _apply_window_icon(app):
    """打包版有 app.ico 时，窗口/任务栏也用同一个图标；源码运行没有就跳过。"""
    icon_path = os.path.join(paths.resource_root(), "app.ico")
    if not os.path.exists(icon_path):
        return
    icon = QIcon(icon_path)
    if not icon.isNull():
        app.setWindowIcon(icon)


def main():
    app = QApplication(sys.argv)
    try:
        cleanup_update_leftovers()
    except Exception:
        pass
    # 启用「上次命中点」跨启动持久化；模板图片换了会由内容签名自动失效
    try:
        from core.template_match import load_last_hits, prune_invalid_last_hits, set_last_hits_path
        import main as main_module
        set_last_hits_path(paths.LAST_HITS_FILE)
        load_last_hits()
        prune_invalid_last_hits(main_module.templates)
    except Exception:
        pass
    # 加载 Qt 官方中文翻译：QMessageBox 的「显示详细信息」、是/否/确定/取消等
    # 标准文字才会是中文（否则固定显示英文，所有者 2026-10-26 反馈过）。
    install_qt_translations(app)
    app.setApplicationName("VisionFlow Automator")
    _apply_window_icon(app)
    app.setApplicationVersion(config.APP_VERSION)
    window = PySide6ScriptWindow()
    window.show()
    # 启动后自动检查一次更新：后台线程，超时/断网/限流都不影响使用（F11）
    if getattr(config, "CHECK_UPDATE_ON_START", True):
        window.start_update_check(manual=False)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
