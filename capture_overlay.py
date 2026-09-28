# -*- coding: utf-8 -*-
"""屏幕采集覆盖层与两个窗口共用的采集管线（CaptureOverlayMixin）。

从 gui_pyside6.py 外移出来的第一大块：覆盖层本体 + 采集管线 / 分组编辑 /
迂回编辑器等两个窗口共用的实现。两个窗口仍通过「访问器 + 收尾钩子」提供各自的
数据路径，本模块不依赖任何窗口类。

`TASKS` / `reload_templates` 都是模块级对象的引用，测试里的
`gui_pyside6.TASKS[:] = ...`（原地替换）对本模块同样生效。

名字在 gui_pyside6 里重新导出，`gui_pyside6.CaptureOverlayMixin` 等用法不变。
"""
import glob
import os
import uuid
from functools import partial

from PySide6.QtCore import QRect, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import config
from main import reload_templates
from tasks import TASKS
from ui_common import (
    _add_click_until_row,
    _add_coordinate_row,
    _add_form_row,
    _add_region_selector_row,
    _capture_rect,
    _first_available_template_pixmap,
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

    def clear_click_point(self):
        """清空当前步骤的备用点击点（与「记录点击点」对称：立即写入并落盘）。

        引擎的 ``resolve_click_position`` 依次看 ``click_position`` 与
        ``click_x``/``click_y``，所以三个键必须一起删——只清空两个输入框而不删
        ``click_position`` 的话，旧坐标仍然会生效。删掉之后引擎回到"按识别到的位置点击"。
        （同一套键的回写在 ``_apply_common_editor_fields`` 里也有一份，保持一致。）
        """
        task = self._capture_current_task()
        if task is None:
            return
        self.click_x_edit.clear()
        self.click_y_edit.clear()
        for key in ("click_x", "click_y", "click_position"):
            task.pop(key, None)
        self._capture_log("已清空当前步骤的备用点击点（之后按识别位置点击）")
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

    # ---------- 编辑器面板的构造（两个窗口共用） ----------
    #
    # 两个窗口的编辑器面板此前是两份近乎逐行相同的构造代码（蓝图 168 行里有 85 行
    # 与主窗口逐字相同，其中一段连续 41 行）。这里收成一份。
    #
    # 两边**确实不同**的地方用下面 3 个钩子表达（并写明理由）：
    #   _editor_field_labels()           两个窗口这几个标签的文字历史上不同
    #   _editor_special_form_container() 主窗口把「类型专用字段」套了 GroupBox，蓝图没有
    #   _editor_preview_placeholder()    模板预览的占位文字
    # 「应用修改」按钮直接接 `self._apply_editor`：两个窗口都提供这个同名方法
    # （主窗口的实现转调 apply_selected_task），因此不需要额外的钩子。
    # 结构等价性由控件树指纹保证（重构前后逐行一致），不是靠"看起来一样"。

    def _editor_field_labels(self):
        return {"threshold": "匹配阈值:", "timeout": "超时(秒):", "status": "状态:"}

    def _editor_special_form_container(self):
        return None, QFormLayout()

    def _editor_preview_placeholder(self):
        return ""

    def _build_shared_editor_panel(self, layout):
        """把编辑器面板的全部控件建好并放进 layout（两个窗口共用的那一份）。"""
        labels = self._editor_field_labels()

        self.selected_label = QLabel("未选择步骤")
        self.selected_label.setWordWrap(True)
        layout.addWidget(self.selected_label)

        self.template_preview = QLabel(self._editor_preview_placeholder())
        self.template_preview.setAlignment(Qt.AlignCenter)
        self.template_preview.setFixedSize(180, 110)
        self.template_preview.setFixedHeight(110)
        self.template_preview.setStyleSheet("border: 1px solid #cbd5e1; background: #f8fafc;")
        self.template_preview.setVisible(False)

        # 注意：编辑器相关的按钮必须与模板预览一起放进 preview_row —— 直接
        # editor_layout.addWidget(self.editor_actions) 会让无父控件的 QWidget 变成
        # 独立顶层窗口（表现为弹窗）。
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

        # 模板预览在左、操作按钮在右，避免按钮整行铺开导致右侧留白。
        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.setSpacing(8)
        preview_row.addWidget(self.template_preview, 0, Qt.AlignTop)
        preview_row.addWidget(self.editor_actions, 1)
        layout.addLayout(preview_row)

        # 步骤名称与启用状态对所有步骤类型可见
        name_form = QFormLayout()
        self.description_edit = QLineEdit()
        self.enabled_checkbox = QCheckBox("启用步骤")
        name_form.addRow("步骤名称:", self.description_edit)
        name_form.addRow(labels["status"], self.enabled_checkbox)
        layout.addLayout(name_form)

        self.recognition_group = QWidget()
        recognition_layout = QVBoxLayout(self.recognition_group)
        recognition_layout.setContentsMargins(0, 0, 0, 0)

        # 识别区域单独成组：它对 normal/advanced 与 click_until_gone 都可见
        # （引擎两种步骤都会用到 match_rects，见 _set_region_group_visible）。
        self.region_group = QGroupBox("识别区域")
        region_layout = QVBoxLayout(self.region_group)
        region_layout.setContentsMargins(0, 0, 0, 0)

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

        self.template_edit = QLineEdit()
        self.threshold_edit = QLineEdit()
        self.timeout_edit = QLineEdit()
        self.after_wait_edit = QLineEdit()
        self.click_checkbox = QCheckBox("执行点击")
        self.match_required_checkbox = QCheckBox("必须识别到图片再点击")
        self.optional_checkbox = QCheckBox("可选步骤（跳过）")
        self.click_x_edit = QLineEdit()
        self.click_y_edit = QLineEdit()
        # 「清空点击点」：与「记录点击点」对称的入口。此前只能靠"把两个框清空再点应用修改"
        # 达到同样效果，但没有任何可见按钮（所有者 2026-09-28 反馈缺失）。
        self.click_clear_button = QPushButton("清空点击点")
        self.click_clear_button.clicked.connect(self.clear_click_point)
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
        add_row(labels["threshold"], self.threshold_edit)
        add_row(labels["timeout"], self.timeout_edit)
        add_row("完成后等待(秒):", self.after_wait_edit)
        # X/Y 成对并排（此前 4 个坐标各占一行，输入框 529px 却只填一个短数字）
        # 「偏移」一行已于 2026-09-28 按所有者要求删除：界面不再提供偏移输入，
        # 引擎仍读 task["offset"]（缺省 (0, 0)），已有数据里的值原样保留。
        add_coordinate_row("点击:", self.click_x_edit, self.click_y_edit, self.click_clear_button)

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

        # 面板顺序：识别设置 -> 识别区域 -> 持续点击设置 -> 类型专用字段 -> 组设置
        layout.addWidget(self.recognition_group)
        layout.addWidget(self.region_group)
        layout.addWidget(self.click_until_group)

        special_group, special_form = self._editor_special_form_container()
        self.special_form = special_form
        self.special_edits = {}
        if special_group is not None:
            self.special_group = special_group
            layout.addWidget(special_group)
        else:
            layout.addLayout(special_form)

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
        layout.addWidget(self.group_form)

        layout.addStretch(1)

    # ---------- 「类型专用字段」的渲染与回写（两个窗口共用） ----------
    #
    # 两个窗口此前各写一份：渲染部分逐字相同，回写部分只差两处语义
    # （输入为空时是否删键、数值解析失败时的回退值）。这里把**逻辑**收成一份；
    # 规格表仍由各窗口通过 `_special_field_specs(task)` 提供 —— 两边的标签文字
    # 历史上就不同（例如主窗口的「按住时长」在蓝图窗口叫「持续时间」），
    # 那属于窗口自己的文案，保留差异；重复的**代码**才是债。

    def _special_empty_input_pops(self):
        """输入框为空时是否删除该键。

        mixin 默认 = 蓝图窗口的历史行为（删除）；主窗口历史上是写入回退值/空串，
        由主窗口覆盖成 False。
        """
        return True

    def _special_float_fallback(self, key, task):
        """数值输入无法解析时的回退值。

        mixin 默认 = 蓝图窗口（0.0）；主窗口对 threshold 用 config.THRESHOLD、
        其余用任务里的原值，由主窗口覆盖。
        """
        return 0.0

    def _special_field_specs(self, task):
        """返回 [(键, 标签, 种类)]；种类见下面两个方法。默认 = 蓝图窗口的规格表。"""
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
                if text == "" and self._special_empty_input_pops():
                    task.pop(key, None)
                else:
                    task[key] = self._float(text, self._special_float_fallback(key, task))
                continue
            if kind == "int":
                if text == "":
                    task.pop(key, None)
                else:
                    parsed = self._int(text)
                    if parsed is not None:
                        task[key] = parsed
                continue
            if text == "" and self._special_empty_input_pops():
                task.pop(key, None)
            else:
                task[key] = text
        # 事件 / 按键步骤的 template 与专用字段保持同步（两个窗口一致）
        if task.get("type") == "event" and task.get("event_template"):
            task["template"] = task["event_template"]
        if task.get("type") == "key_press" and task.get("key"):
            task["template"] = task["key"]

    # ---------- 编辑器面板「任务 -> 控件 / 控件 -> 任务」的公共核心 ----------
    #
    # 两个窗口此前各写一份加载与回写，并且已经出现过**无声漂移**：
    #   · 主窗口曾漏掉 wait_for / next_template 的加载（点「应用修改」就把它们改坏）
    #   · 主窗口的「可选步骤（跳过）」勾选框在回写时从未被读取（勾了等于没勾）
    # 这里把两边**逐字相同**的部分收成一份；确实不同的行为用下面几个小钩子
    # 显式表达（并在注释里写明差异与理由），避免以后再次无声漂移。
    #
    # 调用约定：窗口自己的 _load_editor_for_task / apply_selected_task 负责
    # 标签文字、显隐与收尾（保存、刷新、历史），公共字段全部走这两个方法。

    def _editor_timeout_text(self, task):
        """「超时(秒)」输入框的初值。

        历史差异：主窗口会回退到旧的 `wait_timeout` 字段，蓝图窗口不会。
        保持现状以免改动行为（见 §8.2 的对称性缺口清单）。
        """
        return str(task.get("timeout", 5))

    def _editor_float_fallback(self, key, task, default):
        """输入框内容无法解析时的回退值。

        历史差异：蓝图窗口保留任务里的原值（`task.get(key, default)`），
        主窗口直接用固定默认值。保持现状。
        """
        return default

    def _update_editor_visibility(self, task):
        """按步骤类型切换面板各组的显隐（两个窗口这段逻辑逐字相同）。"""
        is_click_until = task.get("type") == "click_until_gone"
        is_recognition = task.get("type", "normal") in ("normal", "advanced")
        self.recognition_group.setVisible(is_recognition)
        # 识别区域对「识别型」与「持续点击」都可见：后者同样会用到 match_rects
        self._set_region_group_visible(is_recognition or is_click_until)
        self.click_until_group.setVisible(is_click_until)
        self.editor_actions.setVisible(is_recognition or is_click_until)

    def _load_common_editor_fields(self, task):
        """把任务里两个窗口共有的字段加载到面板控件。"""
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
        self.timeout_edit.setText(self._editor_timeout_text(task))
        self.after_wait_edit.setText(str(task.get("after_wait", 0.25)))
        self.click_checkbox.setChecked(bool(task.get("click", True)))
        self.match_required_checkbox.setChecked(bool(task.get("click_requires_match", True)))
        self.optional_checkbox.setChecked(bool(task.get("optional", not bool(task.get("required", True)))))
        click_position = task.get("click_position")
        self.click_x_edit.setText(str(task.get("click_x", click_position[0] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
        self.click_y_edit.setText(str(task.get("click_y", click_position[1] if isinstance(click_position, (list, tuple)) and len(click_position) >= 2 else "")))
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

    def _apply_common_editor_fields(self, task):
        """把面板控件里两个窗口共有的字段回写到任务（normal / advanced 步骤）。"""
        template_value = self.template_edit.text().strip() or "new_step"
        if task.get("type") == "advanced":
            templates = [item.strip() for item in template_value.replace("，", ",").split(",") if item.strip()]
            task["templates"] = templates or ["new_step"]
            task["template"] = task["templates"][0]
        else:
            task["template"] = template_value
        task["threshold"] = self._float(self.threshold_edit.text(),
                                        self._editor_float_fallback("threshold", task, config.THRESHOLD))
        task["timeout"] = self._float(self.timeout_edit.text(),
                                      self._editor_float_fallback("timeout", task, 5.0))
        task["after_wait"] = self._float(self.after_wait_edit.text(),
                                         self._editor_float_fallback("after_wait", task, 0.25))
        task["click"] = self.click_checkbox.isChecked()
        task["click_requires_match"] = self.match_required_checkbox.isChecked()
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

    def _editor_apply_optional(self, task):
        """回写「可选步骤（跳过）」。

        蓝图窗口本来就会写；主窗口**历史上从不写**（勾选框只显示、点了等于没勾，
        见 §8.2 #7），为了本次重构零行为变化，由主窗口覆盖成空实现。
        """
        task["optional"] = self.optional_checkbox.isChecked()
        task["required"] = not task["optional"]


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
