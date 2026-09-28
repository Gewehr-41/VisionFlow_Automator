# PySide6 主界面：新版任务编辑器、蓝图编辑器和线程化执行控制。
import glob
import json
import os
import sys
import threading
import traceback
import uuid
import zipfile
from copy import deepcopy
from functools import partial

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
import diagnostics
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
    TASK_PRESETS,
)
from nodes import JUMP_FIELD_LABELS, NodeGraph, fill_missing_connections, remap_jump_targets
from core.hotkey import GlobalHotkey
from core.input import release_all_inputs
# ---- 已外移的模块（这里重新导出，保证 gui_pyside6.<名字> 的既有用法不变）----
# 说明：_load_template_pixmap 在 gui_pyside6 内部已无人使用，但为保持
# `gui_pyside6.<名字>` 这一层兼容面完整，仍然一并导出。
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





class CaptureOverlayMixin:
    """屏幕采集覆盖层的共享实现。

    主窗口（PySide6ScriptWindow）与蓝图窗口（BlueprintWindow）此前各有一份
    近乎逐行相同的采集管线（覆盖层生命周期、点击/框选/截图回调、取消、
    清空识别区域、下一模板绑定），差异仅在以下两点：

      1. 当前步骤的访问路径不同
         主窗口 -> self.current_task_index + 全局 TASKS
         蓝图窗口 -> self.current_index + self.tasks
      2. 修改后的收尾动作不同
         主窗口 -> save_current_tasks() / refresh_task_list() / select_task_index()
         蓝图窗口 -> _push_history() / refresh() / _select_node() / save_layout()
      3. 少量额外日志与控件清空范围

    因此这里用「访问器 + 收尾钩子」把它们统一，子类只需覆盖下面几个钩子。
    """

    # ---------- 子类必须覆盖：数据访问 ----------

    def _capture_index(self):
        """返回当前步骤下标（无效时为 -1）。"""
        return getattr(self, "current_task_index", -1)

    def _capture_task_list(self):
        """返回当前步骤所在的列表。"""
        return TASKS

    def _capture_current_task(self):
        """返回当前步骤字典；下标无效时返回 None。"""
        index = self._capture_index()
        tasks = self._capture_task_list()
        if 0 <= index < len(tasks):
            return tasks[index]
        return None

    # ---------- 子类可覆盖：收尾钩子 ----------

    def _capture_after_task_changed(self):
        """步骤数据被修改后调用（保存、刷新、选中态同步）。"""

    def _capture_show_parent(self):
        """恢复显示父窗口（仅蓝图窗口需要，主窗口无父窗口）。"""

    def _capture_log(self, message):
        """输出一条采集相关日志（默认丢弃；主窗口会覆盖为写入日志框）。"""

    def _capture_notify_next_template_widgets(self, image_name):
        """把「下一模板」同步到编辑控件（两个窗口的等待方式下拉框内容不同）。"""
        self.next_template_edit.setText(image_name)

    # ---------- 共享的取值辅助 ----------
    #
    # 这两个方法必须放在 mixin 里，不能只挂在某一个窗口上：迂回子步骤对话框
    # （open_detour_editor / _configure_detour_step）是两个窗口共用的一份实现，
    # 一旦它调用只定义在单个窗口上的辅助方法，另一个窗口点「保存」就是
    # AttributeError，而且异常发生在 Qt 槽里、只打到隐藏的 stderr，用户看到的是
    # 「按了保存没反应」。（36da50b 把两份对话框合成一份时踩过这个坑。）

    @staticmethod
    def _int(value):
        return _to_int(value)

    @staticmethod
    def _float(value, fallback):
        return _to_float(value, fallback)

    # ---------- 识别区域列表（一个步骤可以有多条） ----------
    #
    # 引擎按 `match_rects` 的条数走不同策略（main.match_task_templates）：
    #   0 条 -> 全屏匹配；1 条 -> 先在该区域内找、未命中再逐次扩大到全屏；
    #   N 条 -> 逐条尝试取最高分；全都没命中，同样从第 1 条起逐次扩大到全屏。
    # 所以区域的条数是有语义的，不能被静默压缩。

    @staticmethod
    def _normalize_region(value):
        """把 match_rects 里的一项规范化为 (l, t, r, b) 整数元组；无法解析返回 None。

        这里**有意不做 right>left 校验**：它只用于显示与回写已存数据，退化矩形也
        应该照原样显示出来让用户改，而不是被悄悄丢掉。

        文本形式额外容忍一层方括号/圆括号：界面曾经把矩形按 Python 列表的
        repr 显示成 `[30, 28, 89, 81]`，若不剥掉括号就解析失败，用户只是打开
        对话框再点保存，识别区域就会被静默删掉。
        """
        if isinstance(value, (list, tuple)) and len(value) >= 4:
            numbers = [_to_int(item) for item in value[:4]]
        elif isinstance(value, str):
            text = value.strip().strip("[](){}")
            parts = [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]
            numbers = [_to_int(item) for item in parts] if len(parts) == 4 else []
        else:
            return None
        if len(numbers) != 4 or any(item is None for item in numbers):
            return None
        return tuple(numbers)

    def _region_rects_of(self, task):
        """返回任务里配置的识别区域列表（规范化后的元组列表，兼容旧的单矩形字段）。"""
        if not isinstance(task, dict):
            return []
        rects = task.get("match_rects")
        if isinstance(rects, list):
            normalized = [self._normalize_region(rect) for rect in rects]
            normalized = [rect for rect in normalized if rect is not None]
            if normalized:
                return normalized
        for key in ("match_rect", "search_rect"):
            parsed = self._normalize_region(task.get(key))
            if parsed is not None:
                return [parsed]
        return []

    @staticmethod
    def _store_region_rects(task, rects):
        """把区域列表写回任务；空列表表示"没有识别区域"（三个字段一起清理）。"""
        if not isinstance(task, dict):
            return
        cleaned = [tuple(int(value) for value in rect[:4]) for rect in rects]
        if cleaned:
            task["match_rects"] = cleaned
            task["match_rect"] = cleaned[0]
            task["search_rect"] = cleaned[0]
        else:
            for key in ("match_rects", "match_rect", "search_rect"):
                task.pop(key, None)

    def _region_widgets_for_selector(self):
        """返回 (选择框, 状态标签, 删除按钮)；未构建则返回 (None, None, None)。"""
        return (
            getattr(self, "region_selector", None),
            getattr(self, "region_status_label", None),
            getattr(self, "region_delete_button", None),
        )

    def _refresh_region_selector(self, task):
        """按任务当前的区域列表重建"区域选择"控件，返回规范化后的区域列表。"""
        rects = self._region_rects_of(task)
        if self._region_index >= len(rects):
            self._region_index = max(0, len(rects) - 1)
        selector, status_label, delete_button = self._region_widgets_for_selector()
        if selector is None:
            return rects
        selector.blockSignals(True)
        selector.clear()
        for position, rect in enumerate(rects):
            selector.addItem(f"区域 {position + 1}（{rect[0]}, {rect[1]}, {rect[2]}, {rect[3]}）")
        selector.setCurrentIndex(self._region_index if rects else -1)
        selector.blockSignals(False)
        selector.setEnabled(bool(rects))
        if status_label is not None:
            status_label.setText(f"共 {len(rects)} 个" if rects else "未设置")
        if delete_button is not None:
            delete_button.setEnabled(bool(rects))
        return rects

    def _on_region_selector_changed(self, index):
        """记住当前选中的是哪一条区域（删除按钮按这个下标删）。"""
        if index is None or index < 0:
            return
        rects = self._region_rects_of(self._capture_current_task())
        if not 0 <= index < len(rects):
            return
        self._region_index = index

    def delete_current_region(self):
        """删除"正在编辑的那一条"识别区域，其余区域原样保留。"""
        task = self._capture_current_task()
        if task is None:
            return
        rects = self._region_rects_of(task)
        if not 0 <= self._region_index < len(rects):
            return
        removed = rects.pop(self._region_index)
        self._store_region_rects(task, rects)
        self._region_index = max(0, min(self._region_index, len(rects) - 1))
        self._capture_log(f"已删除识别区域 {tuple(removed)}，剩余 {len(rects)} 个")
        self._capture_after_task_changed()

    def _capture_sync_region_index(self, task):
        """换到另一个步骤时把"正在编辑的区域"重置回第 1 条。

        用任务的稳定 `id` 判断是否换了步骤：同一步骤因刷新而重新载入时不会重置，
        所以"刚框好的那条区域"能一直留在视图里。
        """
        key = task.get("id") if isinstance(task, dict) and task.get("id") is not None else id(task)
        key = str(key)
        if key != getattr(self, "_region_task_key", None):
            self._region_task_key = key
            self._region_index = 0

    # ---------- 分组编辑：子类必须覆盖的数据访问 ----------

    def _group_metadata_for_current_mode(self):
        """返回当前模式下用于读写分组设置的元数据字典。

        主窗口直接操作 mode_group_metadata[当前预设]；
        蓝图窗口操作自己持有一份副本（由 save_blueprint_state 写回）。
        """
        return {}

    def _group_default_color(self):
        """组颜色的缺省值（两个窗口的配色不同）。"""
        return "#e0f2fe"

    def _group_color_button_text_color(self):
        """组颜色按钮上的文字颜色（深浅配色不同）。"""
        return "#1f2937"

    def _group_color_dialog_title(self):
        return "选择分组颜色"

    # ---------- 分组编辑：子类可覆盖的收尾钩子 ----------

    def _group_after_editor_applied(self, group_id, name):
        """应用组设置后的收尾动作（保存方式与日志各自不同）。"""

    def _group_hide_editor_groups(self):
        """切到组设置面板时，需要隐藏的其它编辑分组。"""

    # ---------- 分组编辑：共享实现 ----------

    def _group_names(self):
        value = self._group_metadata_for_current_mode().get("names", {})
        return value if isinstance(value, dict) else {}

    def _group_colors(self):
        value = self._group_metadata_for_current_mode().get("colors", {})
        return value if isinstance(value, dict) else {}

    def _group_order(self):
        value = self._group_metadata_for_current_mode().get("order", [])
        return value if isinstance(value, list) else []

    def _group_children(self):
        value = self._group_metadata_for_current_mode().get("children", {})
        return value if isinstance(value, dict) else {}

    def _group_parents(self):
        value = self._group_metadata_for_current_mode().get("parents", {})
        return value if isinstance(value, dict) else {}

    def _update_group_color_button(self, group_id):
        colors = self._group_colors()
        color = self._pending_group_color or colors.get(str(group_id), self._group_default_color())
        self.group_color_button.setText(color)
        self.group_color_button.setStyleSheet(
            f"background: {color}; color: {self._group_color_button_text_color()};"
        )

    def _pick_group_color(self):
        if self.current_group_id is None:
            return
        colors = self._group_colors()
        current = QColor(
            self._pending_group_color or colors.get(self.current_group_id, self._group_default_color())
        )
        color = QColorDialog.getColor(current, self, self._group_color_dialog_title())
        if color.isValid():
            self._pending_group_color = color.name()
            self._update_group_color_button(self.current_group_id)

    def _load_editor_for_group(self, group_id):
        self.current_group_id = str(group_id)
        self._capture_set_no_current_task()
        self.selected_label.setText("组设置")
        self.template_preview.setVisible(False)
        self.editor_actions.setVisible(False)
        self.recognition_group.setVisible(False)
        self._set_region_group_visible(False)
        self._group_hide_editor_groups()
        self.group_form.setVisible(True)
        self.group_name_edit.setText(self._group_names().get(str(group_id), str(group_id)))
        self._pending_group_color = None
        self._update_group_color_button(str(group_id))

    def _set_region_group_visible(self, visible):
        """显隐「识别区域」组。

        它必须对**两种**步骤都可见：`normal` / `advanced` 自不必说，
        `click_until_gone` 同样会用到识别区域 —— `execute_click_until_gone_task`
        会调 `match_task_templates` -> `resolve_search_rects`。此前区域控件挂在
        `recognition_group` 里，而那个组对 `click_until_gone` 是隐藏的，于是配置
        生效、界面却看不见也改不了。
        """
        group = getattr(self, "region_group", None)
        if group is not None:
            group.setVisible(bool(visible))

    def _capture_set_no_current_task(self):
        """把「当前步骤」置为无效（两个窗口的属性名不同）。"""

    def _apply_group_editor(self):
        if self.current_group_id is None:
            return
        group_id = self.current_group_id
        name = self.group_name_edit.text().strip() or group_id
        metadata = self._group_metadata_for_current_mode()
        metadata.setdefault("names", {})[group_id] = name
        if self._pending_group_color is not None:
            metadata.setdefault("colors", {})[group_id] = self._pending_group_color
        for task in self._capture_task_list():
            if str(task.get("group_id") or "group_default") == group_id:
                task["group_name"] = name
                if self._pending_group_color is not None:
                    task["group_color"] = self._pending_group_color
        self._group_after_editor_applied(group_id, name)
        self._load_editor_for_group(group_id)

    # ---------- 共享实现 ----------

    def _capture_rect(self):
        return _capture_rect()

    def _show_capture_overlay(self, mode):
        self._keep_window_hidden = False
        if self.capture_overlay is not None:
            self.capture_overlay.close()
        self.capture_overlay = CaptureOverlay(self._capture_rect(), mode)
        self.capture_overlay.clicked.connect(self.finish_click_capture)
        self.capture_overlay.region_selected.connect(self.finish_region_capture)
        self.capture_overlay.image_selected.connect(self.finish_image_capture)
        self.capture_overlay.cancelled.connect(self.cancel_capture)
        self.capture_overlay.too_small.connect(self.finish_too_small)
        self.capture_overlay.destroyed.connect(self.clear_capture_overlay)
        self._capture_log("请在覆盖层中选择位置，按 Esc 取消。")
        self.capture_overlay.start()
        self.hide()
        parent = self.parent()
        if parent is not None:
            parent.hide()

    def start_click_capture(self):
        if self._capture_index() >= 0:
            self._capture_callback = None
            self._show_capture_overlay("click")

    def start_region_capture(self):
        if self._capture_index() >= 0:
            self._capture_callback = None
            self._capture_target = "match"
            self._show_capture_overlay("region")

    def start_next_template_capture(self):
        if self._capture_index() >= 0:
            self._capture_callback = None
            self._capture_target = "next_template"
            self._show_capture_overlay("image")

    def start_next_region_capture(self):
        if self._capture_index() < 0:
            return
        if not self.next_template_edit.text().strip():
            self._capture_log("请先选择下一模板图片，再框选它的出现位置。")
            return
        self._capture_callback = None
        self._capture_target = "next"
        self._show_capture_overlay("region")

    def _begin_dialog_capture(self, mode, callback):
        self._capture_callback = callback
        self._capture_target = "match"
        self._show_capture_overlay(mode)

    def select_next_template_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择等待出现的目标图片", config.ICON_DIR, "PNG 图片 (*.png)")
        if path:
            self.next_template_edit.setText(os.path.splitext(os.path.basename(path))[0])
            self._capture_set_wait_for_next()

    def _capture_set_wait_for_next(self):
        """把等待方式切到「等待目标模板出现」（两个窗口的下拉项文本不同）。"""
        combo = self.wait_for_combo
        for position in range(combo.count()):
            if "等待目标模板出现" in combo.itemText(position):
                combo.setCurrentIndex(position)
                return

    def cancel_capture(self):
        self.clear_capture_overlay()
        self.activateWindow()
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        if callback is not None:
            callback(("cancelled",))
        self._capture_log("已取消屏幕采集。")

    def finish_too_small(self):
        self.clear_capture_overlay()
        self.activateWindow()
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        if callback is not None:
            callback(("cancelled",))
        QMessageBox.warning(self, "框选失败", "框选区域太小，请重新拖曳选择。")

    def clear_capture_overlay(self):
        if self.capture_overlay is not None:
            overlay = self.capture_overlay
            self.capture_overlay = None
            overlay.close()
        # 截图流程进行中（_keep_window_hidden=True）不恢复窗口，由 _save_captured_image
        # 在截图完成后统一显示，避免覆盖层销毁回调把脚本窗口提前显示出来。
        if not getattr(self, "_keep_window_hidden", False):
            self.show()
            self._capture_show_parent()

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
        task = self._capture_current_task()
        if task is None:
            return
        task["click_x"] = int(x)
        task["click_y"] = int(y)
        task["click_position"] = (int(x), int(y))
        if task.get("type") != "click_until_gone":
            task.pop("match_rect", None)
            task.pop("search_rect", None)
        self._capture_log(f"已记录备用点击坐标: ({x}, {y})")
        self._capture_after_task_changed()

    def finish_region_capture(self, left, top, right, bottom):
        self.clear_capture_overlay()
        self.activateWindow()
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        if callback is not None:
            callback(("region", (left, top, right, bottom)))
            return
        task = self._capture_current_task()
        if task is None:
            return
        if getattr(self, "_capture_target", "match") == "next":
            task["next_match_rect"] = (left, top, right, bottom)
            task["next_search_rect"] = (left, top, right, bottom)
            self._capture_after_task_changed()
            return
        match_rects = task.setdefault("match_rects", [])
        if not isinstance(match_rects, list):
            match_rects = []
            task["match_rects"] = match_rects
        match_rects.append((left, top, right, bottom))
        # 新框的这一条就是用户此刻想操作的那条（删除按钮按当前下标删）：
        # 此前只把结果写进控件、随后又被重载覆盖回第 1 条，于是"框第 2 个区域"
        # 在界面上毫无反馈（看上去像没框上）。
        self._region_index = len(match_rects) - 1
        self._store_region_rects(task, match_rects)
        self._capture_after_task_changed()

    def clear_match_region(self):
        task = self._capture_current_task()
        if task is None:
            return
        for key in ("match_rects", "match_rect", "search_rect"):
            task.pop(key, None)
        # _region_index 不必手动归零：随后重载会走 _refresh_region_selector，
        # 它在没有区域时会把下标钳到 0（同一语义只留一处实现）
        self._capture_log("已清空当前步骤的全部识别区域")
        self._capture_after_task_changed()

    def finish_image_capture(self, left, top, right, bottom):
        # 保持窗口隐藏直到截图完成：关闭覆盖层前断开 destroyed 联动，并置位
        # _keep_window_hidden，防止覆盖层销毁时 clear_capture_overlay 提前显示窗口，
        # 导致截图截到脚本窗口。
        overlay = self.capture_overlay
        self.capture_overlay = None
        self._keep_window_hidden = True
        if overlay is not None:
            try:
                overlay.destroyed.disconnect(self.clear_capture_overlay)
            except (RuntimeError, TypeError):
                pass
            overlay.close()
        if right <= left or bottom <= top:
            QMessageBox.warning(self, "框选失败", "框选区域太小，请重新拖曳选择。")
            self._keep_window_hidden = False
            callback = getattr(self, "_capture_callback", None)
            self._capture_callback = None
            if callback is not None:
                callback(("cancelled",))
            self.show()
            self._capture_show_parent()
            return
        callback = getattr(self, "_capture_callback", None)
        self._capture_callback = None
        QTimer.singleShot(120, lambda: self._save_captured_image(left, top, right, bottom, callback))

    def _save_captured_image(self, left, top, right, bottom, callback=None):
        # 必须与加载侧用同一个目录：此前这里硬编码 <脚本目录>/icons，而模板加载
        # 走 config.ICON_DIR —— 一旦 ICON_DIR 被改过，截图存到 A 而加载读 B，
        # 新绑定的图片就会"存了却找不到"。
        icons_dir = config.ICON_DIR
        os.makedirs(icons_dir, exist_ok=True)
        image_name = f"captured_{uuid.uuid4().hex[:10]}"
        image_path = os.path.join(icons_dir, f"{image_name}.png")
        screen = QApplication.primaryScreen()
        # 在窗口仍隐藏时截图，完成后才恢复显示，确保截到的是目标画面而不是脚本窗口。
        pixmap = screen.grabWindow(0, left, top, right - left, bottom - top)
        self._keep_window_hidden = False
        self.show()
        self._capture_show_parent()
        self.activateWindow()
        if pixmap.isNull() or not pixmap.save(image_path, "PNG"):
            QMessageBox.warning(self, "保存失败", "无法保存框选图片。")
            return
        reload_templates()
        if callback is not None:
            callback(("image", image_name))
            return
        task = self._capture_current_task()
        if task is None:
            return
        task["next_template"] = image_name
        task["next_templates"] = [image_name]
        task["wait_for"] = "next_appear"
        self._capture_notify_next_template_widgets(image_name)
        self._capture_log(f"已将框选图片保存并绑定: {image_path}")
        self._capture_after_task_changed()


    def _update_template_preview(self, templates):
        """在编辑面板中显示第一个绑定模板的缩略图。

        两个窗口此前各有一份；实测逻辑完全一致，仅文档字符串与空行不同。
        此前该函数是空实现（只做清空并隐藏），导致 template_preview 永不显示内容。

        注意：窗口构建期间不显示。refresh_task_list() 会自动选中第一个步骤并
        触发本函数，若不设此保护，启动瞬间就会弹出第一张模板缩略图。
        """
        if getattr(self, "_initializing", False):
            self.template_preview.setPixmap(QPixmap())
            self.template_preview.setText("无模板预览")
            self.template_preview.setVisible(False)
            return
        names_hint, pixmap = _first_available_template_pixmap(templates)
        if pixmap is None:
            self.template_preview.setPixmap(QPixmap())
            self.template_preview.setText("无模板预览")
            self.template_preview.setVisible(False)
            return
        self.template_preview.setPixmap(pixmap.scaled(
            self.template_preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        self.template_preview.setText("")
        self.template_preview.setToolTip(f"{names_hint}  {pixmap.width()}x{pixmap.height()}")
        self.template_preview.setVisible(True)
    def select_template_file(self):
        """绑定图片对话框：选择 / 手动框选 / 预览 / 删除候选模板。

        两个窗口此前各有一份 90 行的实现，实测差异只有 3 处索引访问
        （self.tasks[self.current_index] 对比 TASKS[self.current_task_index]），
        现已统一到这里，通过 _capture_current_task() 消除差异。
        """
        task = self._capture_current_task()
        if task is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("绑定图片")
        dialog.resize(420, 440)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("绑定图片操作"))

        image_list = QListWidget()
        layout.addWidget(image_list, 1)

        current = self.template_edit.text()
        if task.get("type") == "click_until_gone":
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
            if task.get("type") == "click_until_gone":
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
        task = self._capture_current_task()
        if task is None:
            return
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
        for task_index, main_task in enumerate(self._capture_task_list()):
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
            self._capture_after_task_changed()
            self._capture_log("已保存迂回设置。")
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

        # ── 普通/高级步骤专属：这些键此前的对话框完全不显示 ──
        # 引擎一直在读它们（main.execute_task 的 timeout / click_requires_match /
        # optional / wait_for），而这份对话框两边的窗口都用得到。字段顺序与两个
        # 窗口的步骤面板保持一致：超时 -> 等待方式 -> 两个复选框。只在普通/高级
        # 类型下显示并回写，避免给拖拽、按键等类型塞进引擎根本不读的键。
        # （原来的「偏移」一行已于 2026-09-28 按所有者要求一并删除。）
        normal_group = QGroupBox("识别与点击（仅普通/高级步骤生效）")
        normal_form = QFormLayout(normal_group)
        timeout_edit = QLineEdit(str(detour_task.get("timeout", detour_task.get("wait_timeout", 5))))
        normal_form.addRow("超时(秒，0为不限制):", timeout_edit)
        wait_for_combo = QComboBox()
        wait_for_combo.addItems(["1. 画面结果变化", "2. 等待目标模板出现", "3. 画面变化后目标结果出现"])
        saved_wait_for = str(detour_task.get("wait_for", "time"))
        wait_for_combo.setCurrentIndex(
            2 if saved_wait_for == "change_then_appear" else 1 if saved_wait_for == "next_appear" else 0
        )
        normal_form.addRow("等待方式:", wait_for_combo)
        match_required_checkbox = QCheckBox("必须识别到图片再点击")
        match_required_checkbox.setChecked(bool(detour_task.get("click_requires_match", True)))
        normal_form.addRow(match_required_checkbox)
        optional_checkbox = QCheckBox("可选步骤（跳过）")
        optional_checkbox.setChecked(bool(detour_task.get("optional", not bool(detour_task.get("required", True)))))
        normal_form.addRow(optional_checkbox)

        def sync_normal_group_visibility():
            normal_group.setVisible(type_combo.currentText() in ("normal", "advanced"))

        type_combo.currentTextChanged.connect(lambda _text: sync_normal_group_visibility())

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
        # 5 个新字段紧跟在模板之后，位置与两个窗口的步骤面板一致
        form.addRow(normal_group)
        sync_normal_group_visibility()

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

        # 显示的是引擎实际会用的那条区域（match_rects 优先，兼容旧的单矩形字段），
        # 并且**不带方括号**——此前直接 str() 一个列表，显示成 `[30, 28, 89, 81]`，
        # 保存时解析不出来就把区域整个删掉了。
        region_rects = self._region_rects_of(detour_task)
        match_rect_edit = QLineEdit(", ".join(str(value) for value in region_rects[0]) if region_rects else "")
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
            rect_text = match_rect_edit.text().strip()
            rect = self._normalize_region(rect_text)
            if rect_text and rect is None:
                # 解析不了就**不能**当成"清空区域"：那会在用户毫无察觉的情况下把
                # 识别区域删掉，匹配范围从"某个区域"变成全屏。这里明确拒绝保存。
                QMessageBox.warning(
                    dialog,
                    "识别区域填得不对",
                    "识别区域需要 4 个数字（左, 上, 右, 下），例如：30, 28, 89, 81。\n"
                    "留空表示这一步不使用识别区域。\n\n"
                    f"当前内容：{match_rect_edit.text().strip()}",
                )
                return
            if rect is not None:
                detour_task["match_rect"] = rect
                detour_task["search_rect"] = rect
                detour_task["match_rects"] = [rect]
            else:
                detour_task.pop("match_rect", None)
                detour_task.pop("search_rect", None)
                detour_task.pop("match_rects", None)
            if type_combo.currentText() in ("normal", "advanced"):
                detour_task["timeout"] = self._float(timeout_edit.text(), detour_task.get("timeout", 5))
                wait_mode = wait_for_combo.currentText()
                detour_task["wait_for"] = (
                    "next_appear" if wait_mode.startswith("2")
                    else "change_then_appear" if wait_mode.startswith("3")
                    else "time"
                )
                detour_task["click_requires_match"] = match_required_checkbox.isChecked()
                detour_task["optional"] = optional_checkbox.isChecked()
                detour_task["required"] = not detour_task["optional"]
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


class BlueprintWindow(CaptureOverlayMixin, QMainWindow):
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
        action_layout = QGridLayout(self.editor_actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setHorizontalSpacing(6)
        action_layout.setVerticalSpacing(4)
        for index, (label, handler) in enumerate((
            ("绑定图片", self.select_template_file),
            ("记录点击点", self.start_click_capture),
            ("框选识别区域", self.start_region_capture),
            ("清空识别区域", self.clear_match_region),
        )):
            button = QPushButton(label)
            button.clicked.connect(handler)
            action_layout.addWidget(button, index // 2, index % 2)
        self.apply_button = QPushButton("应用修改")
        self.apply_button.clicked.connect(self._apply_editor)
        action_layout.addWidget(self.apply_button, 2, 0, 1, 2)

        # 模板预览在左、操作按钮在右，与主窗口保持一致，避免按钮整行铺开
        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.setSpacing(8)
        preview_row.addWidget(self.template_preview, 0, Qt.AlignTop)
        preview_row.addWidget(self.editor_actions, 1)
        self.editor_layout.addLayout(preview_row)

        name_form = QFormLayout()
        self.description_edit = QLineEdit()
        self.enabled_checkbox = QCheckBox("启用步骤")
        name_form.addRow("步骤名称:", self.description_edit)
        name_form.addRow("状态:", self.enabled_checkbox)
        self.editor_layout.addLayout(name_form)

        self.recognition_group = QWidget()
        recognition_layout = QVBoxLayout(self.recognition_group)
        recognition_layout.setContentsMargins(0, 0, 0, 0)
        # 识别区域单独成组：它对 normal/advanced 与 click_until_gone 都可见
        # （引擎两种步骤都会用到 match_rects，见 _set_region_group_visible）。
        # 这里只创建，挂到面板上的动作放在 recognition_group 之后以决定显示顺序。
        self.region_group = QGroupBox("识别区域")
        region_layout = QVBoxLayout(self.region_group)
        region_layout.setContentsMargins(0, 0, 0, 0)
        self.template_edit = QLineEdit()
        self.threshold_edit = QLineEdit()
        self.timeout_edit = QLineEdit()
        self.after_wait_edit = QLineEdit()
        self.click_x_edit = QLineEdit()
        self.click_y_edit = QLineEdit()
        self.region_selector = QComboBox()
        self.region_status_label = QLabel("未设置")
        self.region_delete_button = QPushButton("删除本区域")
        self.region_delete_button.clicked.connect(self.delete_current_region)
        self.region_selector.currentIndexChanged.connect(self._on_region_selector_changed)
        self.next_template_edit = QLineEdit()
        self.wait_for_combo = QComboBox()
        self.wait_for_combo.addItems(["1. 画面结果变化", "2. 等待目标模板出现", "3. 画面变化后目标结果出现"])
        self.click_checkbox = QCheckBox("执行点击")
        self.match_required_checkbox = QCheckBox("必须识别到图片再点击")
        self.optional_checkbox = QCheckBox("可选步骤（跳过）")

        add_row = partial(_add_form_row, recognition_layout)

        add_coordinate_row = partial(_add_coordinate_row, recognition_layout)

        add_row("模板名:", self.template_edit)
        add_row("匹配阈值:", self.threshold_edit)
        add_row("超时(秒):", self.timeout_edit)
        add_row("完成后等待(秒):", self.after_wait_edit)
        # X/Y 成对并排（此前 4 个坐标各占一行，输入框 529px 却只填一个短数字）
        # 「偏移」一行已于 2026-09-28 按所有者要求删除（与主窗口一致）。
        add_coordinate_row("点击:", self.click_x_edit, self.click_y_edit)

        # 「识别区域」组：一个步骤可以有多条区域（引擎逐条尝试）。
        # 区域**只能靠框选产生**，这里只负责查看/切换/删除，不提供手工输入坐标。
        _add_region_selector_row(
            region_layout, self.region_selector,
            self.region_status_label, self.region_delete_button,
        )

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

        # 识别区域组紧跟在识别设置之后（创建在 _build_editor_panel 开头）
        self.editor_layout.addWidget(self.region_group)

        self.click_until_group = QGroupBox("持续点击设置")
        click_until_layout = QVBoxLayout(self.click_until_group)
        click_until_layout.setContentsMargins(0, 0, 0, 0)
        self.click_until_template_edit = QLineEdit()
        self.click_until_interval_edit = QLineEdit()
        self.click_until_stop_delay_edit = QLineEdit()
        self.click_until_timeout_edit = QLineEdit()
        self.click_until_continue_checkbox = QCheckBox("超时后继续执行")
        self.click_until_stop_on_change_checkbox = QCheckBox("画面变化视为成功")

        add_click_until_row = partial(_add_click_until_row, click_until_layout)

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

    def _group_metadata_for_current_mode(self):
        return self.group_metadata

    def _group_default_color(self):
        return "#38bdf8"

    def _group_color_button_text_color(self):
        return "#ffffff"

    def _group_color_dialog_title(self):
        return "选择组颜色"

    def _capture_set_no_current_task(self):
        self.current_index = -1

    def _group_hide_editor_groups(self):
        self._clear_special_form()
        self.click_until_group.setVisible(False)

    def _group_after_editor_applied(self, group_id, name):
        self._push_history()
        self.refresh()
        self.save_layout()

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


    def _load_editor_for_task(self, task):
        self.selected_label.setText(f"第 {self.current_index + 1} 步 · 类型: {task.get('type', 'normal')}")
        is_click_until = task.get("type") == "click_until_gone"
        is_recognition = task.get("type", "normal") in ("normal", "advanced")
        self.recognition_group.setVisible(is_recognition)
        # 识别区域对「识别型」与「持续点击」都可见：后者同样会用到 match_rects
        self._set_region_group_visible(is_recognition or is_click_until)
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
        click_position = task.get("click_position")
        self.click_x_edit.setText(str(task.get("click_x", click_position[0] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
        self.click_y_edit.setText(str(task.get("click_y", click_position[1] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
        # 识别区域只刷新"区域选择"（区域只能框选产生，没有手工输入的控件）
        self._capture_sync_region_index(task)
        self._refresh_region_selector(task)
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
            # 「偏移」不再由界面回写：控件已删除（2026-09-28，与主窗口一致）。
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

    # _int / _float / _normalize_region 已上移到 CaptureOverlayMixin：
    # 迂回子步骤对话框由两个窗口共用，以前这三个只挂在蓝图窗口上，主窗口点
    # 「保存」就抛 AttributeError。

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


class PySide6ScriptWindow(CaptureOverlayMixin, QMainWindow):
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
        self._log_pending_warnings()
        # 运行期也会产生诊断信息，其中热键那条来自后台线程、模板那条来自 worker 线程。
        # 只在 GUI 线程里搬运，所以用一个定时器轮询（而不是让 worker 直接碰控件）。
        self._warning_timer = QTimer(self)
        self._warning_timer.timeout.connect(self._log_pending_warnings)
        self._warning_timer.start(500)

    def _log_pending_warnings(self):
        """把 diagnostics 里**新产生**的诊断信息搬进日志框，返回本次搬运条数。

        线程安全：只对共享列表取快照后在 GUI 线程追加，不做跨线程 UI 操作。
        """
        warnings = diagnostics.snapshot()
        pending = warnings[self._warning_cursor:]
        self._warning_cursor = len(warnings)
        for message in pending:
            self.append_log(message)
        return len(pending)

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
        self.selected_label = QLabel("未选择步骤")
        self.selected_label.setWordWrap(True)
        editor_layout.addWidget(self.selected_label)
        self.template_preview = QLabel("无模板预览")
        self.template_preview.setAlignment(Qt.AlignCenter)
        self.template_preview.setFixedSize(180, 110)
        self.template_preview.setStyleSheet("border: 1px solid #cbd5e1; background: #f8fafc;")
        self.template_preview.setVisible(False)

        # 注意：编辑器相关的按钮此前写成 editor_layout.addWidget(self.editor_actions)，
        # 此时 editor_actions 根本没有父控件，会变成一个独立顶层窗口（表现为弹窗）。
        # 现在它与模板预览一起放进 preview_row，父控件关系由布局建立。
        self.editor_actions = QWidget()
        action_layout = QGridLayout(self.editor_actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setHorizontalSpacing(6)
        action_layout.setVerticalSpacing(4)
        bind_button = QPushButton("绑定图片")
        bind_button.clicked.connect(self.select_template_file)
        action_layout.addWidget(bind_button, 0, 0)
        click_capture_button = QPushButton("记录点击点")
        click_capture_button.clicked.connect(self.start_click_capture)
        action_layout.addWidget(click_capture_button, 0, 1)
        region_capture_button = QPushButton("框选识别区域")
        region_capture_button.clicked.connect(self.start_region_capture)
        action_layout.addWidget(region_capture_button, 1, 0)
        clear_region_button = QPushButton("清空识别区域")
        clear_region_button.clicked.connect(self.clear_match_region)
        action_layout.addWidget(clear_region_button, 1, 1)
        self.apply_button = QPushButton("应用修改")
        self.apply_button.clicked.connect(self.apply_selected_task)
        action_layout.addWidget(self.apply_button, 2, 0, 1, 2)

        # 模板预览在左、操作按钮在右，避免按钮整行铺开导致右侧留白。
        # 放在同一行会建立父子关系，使缩略图与按钮都成为内嵌控件。
        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.setSpacing(8)
        # 与预览图同高：预览是 180x110 的固定尺寸控件，若不加高度限制，
        # 外部布局可能把它拉伸（图片仍按 180x110 显示，周边留白），
        # 看起来就像缩略图只占了一小块。
        self.template_preview.setFixedHeight(110)
        preview_row.addWidget(self.template_preview, 0, Qt.AlignTop)
        preview_row.addWidget(self.editor_actions, 1)
        editor_layout.addLayout(preview_row)

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

        # 识别区域单独成组：它对 normal/advanced 与 click_until_gone 都可见
        # （引擎两种步骤都会用到 match_rects，见 _set_region_group_visible）
        self.region_group = QGroupBox("识别区域")
        region_layout = QVBoxLayout(self.region_group)
        region_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addWidget(self.region_group)

        self.click_until_group = QGroupBox("持续点击设置")
        click_until_layout = QVBoxLayout(self.click_until_group)
        click_until_layout.setContentsMargins(0, 0, 0, 0)
        self.click_until_template_edit = QLineEdit()
        self.click_until_interval_edit = QLineEdit()
        self.click_until_stop_delay_edit = QLineEdit()
        self.click_until_timeout_edit = QLineEdit()
        self.click_until_continue_checkbox = QCheckBox("超时后继续执行")
        self.click_until_stop_on_change_checkbox = QCheckBox("画面变化视为成功")

        add_click_until_row = partial(_add_click_until_row, click_until_layout)

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
        self.click_x_edit = QLineEdit()
        self.click_y_edit = QLineEdit()
        self.region_selector = QComboBox()
        self.region_status_label = QLabel("未设置")
        self.region_delete_button = QPushButton("删除本区域")
        self.region_delete_button.clicked.connect(self.delete_current_region)
        self.region_selector.currentIndexChanged.connect(self._on_region_selector_changed)
        self.next_template_edit = QLineEdit()
        self.wait_for_combo = QComboBox()
        self.wait_for_combo.addItems(["1. 画面结果变化", "2. 等待目标模板出现", "3. 画面变化后目标结果出现"])

        add_row = partial(_add_form_row, recognition_layout)

        add_coordinate_row = partial(_add_coordinate_row, recognition_layout)

        add_row("模板名:", self.template_edit)
        # 字段顺序与蓝图窗口保持一致。此前主窗口把匹配阈值放在坐标之后、超时放在
        # 「下一模板」之后、完成后等待与等待方式挤在同一行，同一个数据在两个窗口里
        # 的阅读顺序完全不同。
        add_row("匹配阈值(0-1):", self.threshold_edit)
        add_row("超时(秒，0为不限制):", self.timeout_edit)
        add_row("完成后等待(秒):", self.after_wait_edit)
        # X/Y 成对并排（此前 4 个坐标各占一行，输入框 529px 却只填一个短数字）
        # 「偏移」一行已于 2026-09-28 按所有者要求删除：界面不再提供偏移输入，
        # 引擎仍读 task["offset"]（缺省 (0, 0)），已有数据里的值原样保留。
        add_coordinate_row("点击:", self.click_x_edit, self.click_y_edit)

        # 「识别区域」组：一个步骤可以有多条区域（引擎逐条尝试）。
        # 区域**只能靠框选产生**，这里只负责查看/切换/删除，不提供手工输入坐标。
        _add_region_selector_row(
            region_layout, self.region_selector,
            self.region_status_label, self.region_delete_button,
        )

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

        # 这两个 SVG 是**程序自带的界面资源**（复选框指示器），不是用户模板。
        # 它们有意固定在 <脚本目录>/icons，不跟随 config.ICON_DIR —— 用户把
        # ICON_DIR 指向自己的模板目录时，界面样式仍应正常。
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
        """写入预设文件。

        重要：写入前必须剔除「已删除」的预设。
        加载侧（tasks.load_presets）会按 __deleted__ 名单剔除预设，但保存侧此前
        会把 mode_tasks 整个写出去——两边不对称，导致内存里残留的已删除预设
        （例如被旧 Tkinter 界面写回的）会连同数据一起反复写进文件，形成
        「界面上看不见、文件里删不掉」的僵尸预设；一旦有人手工清空 __deleted__，
        它就会带着旧数据复活。这里从写入侧关掉这条通道，文件也会随之自愈。
        """
        deleted_names = {str(name) for name in self.deleted_preset_names}
        payload = {
            name: deepcopy(tasks)
            for name, tasks in self.mode_tasks.items()
            if name != "custom" and str(name) not in deleted_names
        }
        payload["__deleted__"] = sorted(self.deleted_preset_names)
        # __group_metadata__ 也要清理陈旧条目，否则导入/导出测试与删除预设会持续
        # 留下孤儿元数据。保留规则：
        #   - 内置预设（custom / daily / side）：始终保留。它们的步骤来自 tasks.py，
        #     用户将来仍可能恢复使用，元数据体积极小，留着比删掉更稳妥。
        #   - 其它预设：当前活跃的保留；已被删除的即使仍残留在 mode_tasks 里也不保留，
        #     避免把僵尸状态一起写进文件。
        builtin_names = set(str(name) for name in TASK_PRESETS)
        active_names = set(str(name) for name in self.mode_tasks)
        keep_names = builtin_names | (active_names - deleted_names)
        payload["__group_metadata__"] = {
            name: value
            for name, value in deepcopy(self.mode_group_metadata).items()
            if str(name) in keep_names
        }
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
        # 必须同时从删除名单里移除：否则该名字会同时存在于数据与 __deleted__，
        # 既会被 _save_presets 过滤掉（新建的预设立刻消失），
        # 又会在下次加载时被剔除。
        self.deleted_preset_names.discard(name)
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

    def _selected_copy_entries(self):
        """把当前多选结果整理成 [(kind, value)] 形式，kind 为 task 或 group。"""
        entries = []
        for index in self._selected_task_indices():
            entries.append(("task", index))
        for group_id in self._selected_group_ids():
            entries.append(("group", group_id))
        return entries

    def _copy_selected_entries(self, selected_entries, target_tasks):
        """把选中的步骤/组复制成新任务列表，供追加到目标预设。

        规则（与 gui.py 的既有语义保持一致）：
        - 选中组时连带其所有子组内的步骤；
        - 被整体复制的组会改名为 "<原组id>_copy"，避免与目标预设里的组冲突；
        - 每个步骤重新生成 id，并重映射副本内部的 flow_next 指向。
        """
        selected_task_indices = set()
        selected_group_ids = set()
        metadata = self.mode_group_metadata.get(self.mode_combo.currentText() or "custom", {})
        for kind, value in selected_entries:
            if kind == "task":
                selected_task_indices.add(value)
            elif kind == "group":
                group_id = str(value)
                selected_group_ids.add(group_id)
                for descendant in self._group_descendants(metadata, group_id):
                    selected_group_ids.add(str(descendant))

        source_tasks = []
        for index, task in enumerate(TASKS):
            group_id = str(task.get("group_id") or "group_default")
            if index in selected_task_indices or group_id in selected_group_ids:
                source_tasks.append(task)
        if not source_tasks:
            return []

        existing_group_ids = {
            str(task.get("group_id"))
            for task in target_tasks
            if task.get("group_id") is not None
        }
        group_id_map = {}
        for task in source_tasks:
            source_group_id = str(task.get("group_id") or "group_default")
            if source_group_id in selected_group_ids and source_group_id not in group_id_map:
                candidate = f"{source_group_id}_copy"
                suffix = 2
                while candidate in existing_group_ids or candidate in group_id_map.values():
                    candidate = f"{source_group_id}_copy_{suffix}"
                    suffix += 1
                group_id_map[source_group_id] = candidate

        copied_tasks = []
        copied_id_map = {}
        for task in source_tasks:
            copied_task = deepcopy(task)
            source_group_id = str(task.get("group_id") or "group_default")
            if source_group_id in group_id_map:
                copied_task["group_id"] = group_id_map[source_group_id]
                copied_task["group_name"] = f"{task.get('group_name', '默认分组')} 复制"
            copied_task["id"] = str(uuid.uuid4())
            copied_id_map[str(task.get("id"))] = copied_task["id"]
            copied_tasks.append(copied_task)
        # 副本内部保留原有的先后连接关系
        for copied_task in copied_tasks:
            flow_target = copied_task.get("flow_next")
            if flow_target in copied_id_map:
                copied_task["flow_next"] = copied_id_map[flow_target]
        return copied_tasks

    @Slot()
    def copy_current_preset(self):
        """把当前选中的组/步骤追加到另一个预设（与 gui.py 的既有语义一致）。

        注意：本方法此前是「把整个预设另存为新名称」，与按钮文案、README
        以及 Tkinter 版行为都不符，也没有读取选中项。现按 README 的语义重写。
        """
        entries = self._selected_copy_entries()
        if not entries:
            QMessageBox.warning(self, "无法复制", "请先在项目列表中选择要复制的组或步骤。")
            return

        source_name = self.mode_combo.currentText() or "custom"
        targets = sorted(name for name in self.mode_tasks if name != source_name)
        if not targets:
            QMessageBox.warning(self, "无法复制", "还没有其它预设可作为复制目标，请先新建一个预设。")
            return

        target_name, accepted = QInputDialog.getItem(
            self, "复制到预设", f"已选择 {len(entries)} 项，复制到:", targets, 0, False
        )
        if not accepted or not target_name:
            return
        if target_name == source_name:
            QMessageBox.warning(self, "无法复制", "不能复制到当前预设自身。")
            return
        if QMessageBox.question(self, "确认追加", f"确定把选中的组/步骤追加到预设“{target_name}”吗？") != QMessageBox.Yes:
            return

        # 先保存当前预设，确保源数据是最新的
        self.save_current_tasks()
        target_tasks = self.mode_tasks.setdefault(target_name, [])
        copied = self._copy_selected_entries(entries, target_tasks)
        if not copied:
            QMessageBox.warning(self, "无法复制", "选中的组或步骤没有可复制的内容。")
            return
        target_tasks.extend(copied)
        self._save_presets()
        self._refresh_mode_combo(target_name)
        self.on_mode_selected(target_name)
        self.append_log(f"已将 {len(copied)} 个步骤从“{source_name}”复制到预设“{target_name}”。")

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
        # 同步清掉该预设的分组元数据，否则会留下永远无人引用的孤儿条目。
        self.mode_group_metadata.pop(name, None)
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
        if not isinstance(group_metadata, dict):
            group_metadata = {}
        group_names = group_metadata.get("names", {}) if isinstance(group_metadata.get("names"), dict) else {}
        group_colors = group_metadata.get("colors", {}) if isinstance(group_metadata.get("colors"), dict) else {}
        group_expanded = group_metadata.get("expanded", {}) if isinstance(group_metadata.get("expanded"), dict) else {}
        group_parents = group_metadata.get("parents", {}) if isinstance(group_metadata.get("parents"), dict) else {}
        group_children = group_metadata.get("children", {}) if isinstance(group_metadata.get("children"), dict) else {}

        def make_group(group_id):
            group_label = group_names.get(group_id) or ("默认分组" if group_id == "group_default" else group_id)
            group = QTreeWidgetItem(["分组: " + str(group_label)])
            group.setBackground(0, QColor(group_colors.get(group_id, "#e0f2fe")))
            group.setData(0, Qt.UserRole, "group")
            group.setData(0, Qt.UserRole + 1, group_id)
            group.setFlags(group.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            groups[group_id] = group
            return group

        def ensure_group(group_id):
            """按需创建组控件；若元数据里声明了父组，则挂到父组下面形成嵌套。"""
            group_id = str(group_id)
            if group_id in groups:
                return groups[group_id]
            group = make_group(group_id)
            parent_id = group_parents.get(group_id)
            parent_item = None
            seen = set()
            # 沿 parents 向上找第一个已存在的祖先，避免环或缺失导致的死循环
            while parent_id is not None and str(parent_id) not in seen:
                seen.add(str(parent_id))
                if str(parent_id) in groups:
                    parent_item = groups[str(parent_id)]
                    break
                parent_id = group_parents.get(str(parent_id))
            if parent_item is not None:
                parent_item.addChild(group)
            else:
                self.task_list.addTopLevelItem(group)
            return group

        def make_task_item(index, task):
            description = task.get("description", task.get("template", "未命名步骤"))
            task_type = task.get("type", "normal")
            detour_status = " · 已启用迂回" if task.get("detour_enabled") else ""
            item = QTreeWidgetItem([f"{index + 1:02d}. [{task_type}] {description}{detour_status}"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setData(0, Qt.UserRole, str(task.get("id", index)))
            item.setData(0, Qt.UserRole + 1, index)
            item.setCheckState(0, Qt.CheckState.Checked if task.get("enabled", True) else Qt.CheckState.Unchecked)
            return item

        # 先建出所有组（含仅有元数据、暂无步骤的空组）。
        # ensure_group 会在创建时按 parents 元数据把子组挂到父组下面；
        # 父组即使还没被创建，也会在 child 挂载时由 Qt 自动补为父项，
        # 这里按 order/names/children 的顺序预先建好，保证层级稳定。
        for group_id in (group_metadata.get("order") or []):
            ensure_group(group_id)
        for group_id in group_names:
            ensure_group(group_id)
        for group_id in group_children:
            ensure_group(group_id)

        for index, task in enumerate(TASKS):
            task.setdefault("id", str(uuid.uuid4()))
            group_id = str(task.get("group_id") or "group_default")
            ensure_group(group_id).addChild(make_task_item(index, task))

        # 依据子步骤启用情况回填组勾选状态（自底向上，先算子组再算父组）
        def refresh_group_check_state(group):
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                if child.data(0, Qt.UserRole) == "group":
                    refresh_group_check_state(child)
            states = []
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                if child.data(0, Qt.UserRole) == "group":
                    states.append(child.checkState(0) != Qt.CheckState.Unchecked)
                else:
                    states.append(child.checkState(0) == Qt.CheckState.Checked)
            if not states or all(states):
                group.setCheckState(0, Qt.CheckState.Checked)
            elif any(states):
                group.setCheckState(0, Qt.CheckState.PartiallyChecked)
            else:
                group.setCheckState(0, Qt.CheckState.Unchecked)

        for group_index in range(self.task_list.topLevelItemCount()):
            refresh_group_check_state(self.task_list.topLevelItem(group_index))

        # 恢复各组折叠状态；未记录过的组默认展开
        for group_id, group in groups.items():
            group.setExpanded(bool(group_expanded.get(group_id, True)))

        self.task_list.blockSignals(False)
        if TASKS:
            target_index = min(max(self.current_task_index, 0), len(TASKS) - 1)
            self.select_task_index(target_index)
        else:
            self.clear_editor()

    def select_task_index(self, index):
        item = self._find_task_item(index)
        if item is not None:
            self.task_list.setCurrentItem(item)

    def _find_task_item(self, index):
        """在（可能嵌套的）任务树中按步骤下标查找对应的树项。"""
        def search(parent):
            count = parent.childCount() if hasattr(parent, "childCount") else parent.topLevelItemCount()
            for position in range(count):
                child = parent.child(position) if hasattr(parent, "child") else parent.topLevelItem(position)
                if child.data(0, Qt.UserRole) != "group" and child.data(0, Qt.UserRole + 1) == index:
                    return child
                if child.data(0, Qt.UserRole) == "group":
                    found = search(child)
                    if found is not None:
                        return found
            return None

        return search(self.task_list)

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
        # 识别区域对「识别型」与「持续点击」都可见：后者同样会用到 match_rects
        self._set_region_group_visible(is_recognition or is_click_until)
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
        # 「可选步骤（跳过）」此前从不加载，导致界面显示与实际值不一致（切换步骤时残留上一步的勾选）。
        # 回写逻辑（_apply_editor）本来就读取该控件，这里补上加载。
        self.optional_checkbox.setChecked(bool(task.get("optional", not bool(task.get("required", True)))))
        click_position = task.get("click_position")
        self.click_x_edit.setText(str(task.get("click_x", click_position[0] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
        self.click_y_edit.setText(str(task.get("click_y", click_position[1] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
        # 「下一模板」与「等待方式」也必须加载：apply_selected_task 会把这两个控件的
        # 内容写回任务，此前只写不读 → 选中步骤后不改任何东西直接点「应用修改」，
        # wait_for 会被改成默认的 time、next_template 会被删掉。
        # （蓝图窗口的 _load_editor_for_task 一直是加载的，这里补齐对称。）
        self.next_template_edit.setText(str(task.get("next_template") or ""))
        wait_for = str(task.get("wait_for", "time"))
        if wait_for == "next_appear":
            self.wait_for_combo.setCurrentIndex(1)
        elif wait_for == "change_then_appear":
            self.wait_for_combo.setCurrentIndex(2)
        else:
            self.wait_for_combo.setCurrentIndex(0)
        # 识别区域只刷新"区域选择"（区域只能框选产生，没有手工输入的控件）
        self._capture_sync_region_index(task)
        self._refresh_region_selector(task)
        self._rebuild_special_form(task)


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

    def toggle_item(self, item, _column):
        index = self._task_index_from_item(item)
        if index is None:
            return
        if 0 <= index < len(TASKS):
            TASKS[index]["enabled"] = not bool(TASKS[index].get("enabled", True))
            self._schedule_task_list_commit()

    @Slot()
    def toggle_selected_tasks(self):
        rows = sorted({index for item in self.task_list.selectedItems() if (index := self._task_index_from_item(item)) is not None})
        if not rows:
            return
        should_enable = not all(bool(TASKS[row].get("enabled", True)) for row in rows)
        for row in rows:
            TASKS[row]["enabled"] = should_enable
        self.append_log(f"已切换 {len(rows)} 个选中步骤的启用状态。")
        self._schedule_task_list_commit()

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

    def _schedule_task_list_commit(self):
        """把勾选状态变更统一延迟到下一事件循环一次性保存并刷新任务树。

        不能在 itemChanged 信号处理期间同步 clear()/重建任务树：组勾选会自动联动触发所有
        子项的 itemChanged，此时清空树会让 Qt 继续访问已删除的 QTreeWidgetItem，导致闪退。
        """
        if getattr(self, "_task_list_commit_scheduled", False):
            return
        self._task_list_commit_scheduled = True
        QTimer.singleShot(0, self._run_task_list_commit)

    def _run_task_list_commit(self):
        self._task_list_commit_scheduled = False
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

    @Slot(int)
    def move_task_to_end(self, source_index):
        if not (0 <= source_index < len(TASKS)):
            return
        old_index_by_id = {str(t.get("id")): i for i, t in enumerate(TASKS)}
        task = TASKS.pop(source_index)
        TASKS.append(task)
        # 同上：移动后必须重编号跳转目标。
        remap_jump_targets(TASKS, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in TASKS]))
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

    # ---------- CaptureOverlayMixin 的钩子实现 ----------
    # 采集管线的共享实现在 CaptureOverlayMixin 中，这里只提供主窗口特有的差异。

    def _capture_index(self):
        return self.current_task_index

    def _capture_task_list(self):
        return TASKS

    def _capture_after_task_changed(self):
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(self.current_task_index)

    def _capture_log(self, message):
        self.append_log(message)

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
        # 先按 id 记录旧下标，删除后 id 顺序即等于新顺序。
        old_index_by_id = {str(t.get("id")): i for i, t in enumerate(old_tasks)}
        TASKS[:] = [task for i, task in enumerate(old_tasks) if i not in removed]
        for task in TASKS:
            flow_target = task.get("flow_next")
            if flow_target is not None and not any(str(candidate.get("id")) == str(flow_target) for candidate in TASKS):
                task.pop("flow_next", None)
        # 统一重编号：被删下标不在序列中，指向它们的跳转会被清除。
        remap_jump_targets(TASKS, _jump_index_map([old_index_by_id[str(t.get("id"))] for t in TASKS]))
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
                task["click_interval"] = self._float(self.click_until_interval_edit.text(), 0.5)
                task["stop_delay"] = self._float(self.click_until_stop_delay_edit.text(), 0.0)
                task["timeout"] = self._float(self.click_until_timeout_edit.text(), 30.0)
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
                task["threshold"] = self._float(self.threshold_edit.text(), config.THRESHOLD)
                task["timeout"] = self._float(self.timeout_edit.text(), 5.0)
                task["after_wait"] = self._float(self.after_wait_edit.text(), 0.25)
                task["click"] = self.click_checkbox.isChecked()
                task["click_requires_match"] = self.match_required_checkbox.isChecked()
                # 「偏移」不再由界面回写：控件已删除（2026-09-28），
                # task["offset"] 保持数据里的原值，引擎缺省按 (0, 0) 使用。
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
                    parsed = self._int(value)
                    if parsed is not None:
                        task[key] = parsed
            elif key in {"wait_timeout", "offset_x", "offset_y", "delay_before", "hold_time", "start_x", "start_y", "end_x", "end_y", "duration", "click_interval", "stop_delay", "event_timeout", "after_wait", "threshold"}:
                fallback = config.THRESHOLD if key == "threshold" else task.get(key, 0.0)
                task[key] = self._float(value, fallback)
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
        # 运行期改写 config 是为了让引擎按指定窗口识别；但这两个字段是模块级
        # 全局，此前改完就不管了 —— 脚本停止后 config 仍指向上次的目标窗口，
        # 影响所有在此之后读取它的代码。这里记下原值，收尾时还原
        # （见 _restore_config_after_run，正常运行/异常/停止都会经过）。
        self._saved_config = (config.TARGET_WINDOW_TITLE, config.USE_WINDOW_MODE)
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
        self.worker.finished.connect(self.worker_thread.quit, Qt.ConnectionType.DirectConnection)
        self.worker_thread.finished.connect(self.on_worker_finished)
        self.worker_thread.start()
        self._start_stop_hotkey()

    def _start_stop_hotkey(self):
        """脚本开始执行时注册系统级全局停止快捷键（仅在运行期间生效）。"""
        if self._hotkey is not None and self._hotkey.active:
            return
        try:
            self._hotkey = GlobalHotkey()
            self._hotkey.start(config.STOP_HOTKEY, self._on_global_stop_hotkey)
        except Exception as exc:
            self.append_log(f"注册全局停止快捷键失败: {exc}")
            self._hotkey = None
            return
        if self._hotkey is not None and self._hotkey.active:
            self.append_log(f"已注册全局停止快捷键: {config.STOP_HOTKEY}（脚本运行期间随时可停止）")
        else:
            self.append_log(f"全局停止快捷键 {config.STOP_HOTKEY} 注册失败，可能已被其它程序占用。")

    def _stop_stop_hotkey(self):
        """脚本执行结束后注销全局停止快捷键，避免影响其它程序的按键（如复制粘贴）。"""
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None

    def _on_global_stop_hotkey(self):
        """全局快捷键回调（在后台线程执行）：立即置位停止信号，再经由信号到主线程更新界面。"""
        release_all_inputs()
        self.stop_event.set()
        self.pause_event.clear()
        self.single_step_event.set()
        self.stop_requested.emit()

    @Slot()
    def stop_script(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.stop_event.set()
            self.pause_event.clear()
            self.single_step_event.set()
            release_all_inputs()
            self.status_label.setText("状态: 停止中")
            self.stop_button.setEnabled(False)
            self.append_log("正在停止脚本...")
            # 兜底：若工作线程因事件丢失等原因迟迟未退出，几秒后再次请求其退出事件循环
            QTimer.singleShot(5000, self._request_worker_quit_if_running)

    def _request_worker_quit_if_running(self):
        if self.worker_thread is not None and self.worker_thread.isRunning():
            self.worker_thread.quit()
            self.append_log("当前步骤仍在收尾，等待其结束后自动停止。")

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
    def _restore_config_after_run(self):
        """还原 _start_worker 改写的 config 字段。

        正常结束、脚本异常、用户点停止都会走到 on_worker_finished，所以它是
        唯一的收尾点。没有运行过的窗口调用它是安全的（_saved_config 为 None）。
        """
        saved = getattr(self, "_saved_config", None)
        if saved is None:
            return
        config.TARGET_WINDOW_TITLE, config.USE_WINDOW_MODE = saved
        self._saved_config = None

    def on_worker_finished(self):
        self._set_running_state(False)
        self._restore_config_after_run()
        self.pause_button.setText("暂停")
        if self.stop_event.is_set():
            self.status_text = "已停止"
            self.status_label.setText("状态: 已停止")
        self.append_log("脚本执行结束。")
        self._stop_stop_hotkey()
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker_thread = None
        self.worker = None

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
