import ctypes
import colorsys
import glob
import math
import os
import threading
import uuid
from copy import deepcopy
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from threading import Event

import pyautogui
import pygetwindow as gw

import config
from core.screen import get_window_rect
from main import reload_templates, run_task_queue
from tasks import DELETED_PRESET_NAMES, PRESET_METADATA, TASKS, USER_PRESETS, get_tasks_for_mode, save_presets, save_tasks


class AutoScriptGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("自动脚本控制台")
        self.root.geometry("1180x860")
        self.root.resizable(True, True)

        self.stop_event = Event()
        self.worker_thread = None
        self.selected_task_index = 0
        self.current_mode = "custom"
        self.mode_tasks = {"custom": deepcopy(TASKS)}
        self.mode_tasks.update({name: deepcopy(value) for name, value in USER_PRESETS.items()})
        self.mode_group_metadata = deepcopy(PRESET_METADATA)
        self.deleted_preset_names = set(DELETED_PRESET_NAMES)
        self.capture_timer = None
        self.waiting_for_click_capture = False
        self.region_capture_timer = None
        self.waiting_for_region_capture = False
        self.region_capture_start = None
        self.region_capture_end = None
        self.region_capture_rect = None
        self.region_capture_target = "match"
        self.selection_overlay = None
        self.selection_canvas = None
        self.selection_box_id = None

        self.build_ui()

    def build_ui(self):
        self.root.title("视觉识别自动脚本")
        self.root.geometry("1180x860")
        self.root.minsize(980, 720)

        self.root.configure(background="#f3f5f8")

        header = ttk.Frame(self.root, padding=(14, 12, 14, 8))
        header.pack(fill="x")

        title = ttk.Label(header, text="脚本编辑器", font=("Microsoft YaHei", 18, "bold"))
        title.pack(anchor="w")

        topbar = ttk.Frame(header)
        topbar.pack(fill="x", pady=(8, 0))

        self.loop_var = tk.BooleanVar(value=False)
        self.loop_checkbox = ttk.Checkbutton(topbar, text="循环执行", variable=self.loop_var)
        self.loop_checkbox.pack(side="left")

        ttk.Separator(topbar, orient="vertical").pack(side="left", padx=(16, 12), fill="y")

        ttk.Label(topbar, text="执行功能:").pack(side="left")
        self.mode_var = tk.StringVar(value="custom")
        self.mode_combo = ttk.Combobox(
            topbar,
            textvariable=self.mode_var,
            values=[],
            state="readonly",
            width=16,
        )
        self.mode_combo.pack(side="left", padx=(8, 12))
        self.mode_combo.bind("<<ComboboxSelected>>", self.on_mode_selected)
        ttk.Button(topbar, text="新建预设", command=self.create_preset).pack(side="left", padx=(0, 8))
        ttk.Button(topbar, text="重命名预设", command=self.rename_current_preset).pack(side="left", padx=(0, 8))
        ttk.Button(topbar, text="复制到预设", command=self.copy_current_preset).pack(side="left", padx=(0, 8))
        ttk.Button(topbar, text="删除预设", command=self.delete_current_preset).pack(side="left", padx=(0, 8))
        self.refresh_mode_values()

        ttk.Label(topbar, text="目标窗口:").pack(side="left")
        self.window_var = tk.StringVar()
        self.window_combo = ttk.Combobox(
            topbar,
            textvariable=self.window_var,
            state="readonly",
            width=26,
        )
        self.window_combo.pack(side="left", padx=(8, 8), fill="x", expand=True)

        ttk.Button(topbar, text="刷新窗口", command=self.refresh_window_list).pack(side="left")

        if config.TARGET_WINDOW_TITLE:
            self.window_var.set(config.TARGET_WINDOW_TITLE)

        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=14, pady=(0, 8))
        self.start_btn = ttk.Button(toolbar, text="开始执行", command=self.start_script)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(toolbar, text="停止", command=self.stop_script, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.status_var = tk.StringVar(value="待机")
        ttk.Label(toolbar, textvariable=self.status_var, foreground="#2b7a2b", font=("Microsoft YaHei", 10, "bold")).pack(side="left", padx=(12, 0))

        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        left = ttk.Frame(main, padding=8)
        right = ttk.Frame(main, padding=8)
        main.add(left, weight=3)
        main.add(right, weight=5)

        task_group = ttk.LabelFrame(left, text="项目列表")
        task_group.pack(fill="both", expand=True)

        action_row = ttk.Frame(task_group)
        action_row.pack(fill="x", padx=10, pady=(10, 6))

        buttons = [
            ("全选", self.select_all_tasks), ("清空", self.clear_tasks),
            ("上移", lambda: self.move_selected_item(-1)), ("下移", lambda: self.move_selected_item(1)),
            ("新增组", self.add_group),
            ("复制", self.copy_selected_item), ("删除", self.delete_selected_item),
            ("新增步骤", self.add_task),
        ]

        for i in range(4):
            action_row.grid_columnconfigure(i, weight=1)

        for index, (text, cmd) in enumerate(buttons):
            btn = ttk.Button(action_row, text=text, command=cmd, width=10)
            btn.grid(row=index // 4, column=index % 4, sticky="ew", padx=(0, 6), pady=2)

        self.task_listbox = tk.Listbox(
            task_group,
            height=18,
            selectmode=tk.EXTENDED,
            exportselection=False,
            bg="#f8fafc",
            bd=0,
            highlightthickness=0,
            activestyle="none",
            font=("Microsoft YaHei", 10),
            fg="#1f2937",
        )
        self.task_listbox.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.task_listbox.configure(selectbackground="#dfe9ff", selectforeground="#111827")
        self.task_listbox.bind("<<ListboxSelect>>", self.on_task_select)
        self.task_listbox.bind("<Double-Button-1>", self.toggle_selected_task)
        self.task_listbox.bind("<Return>", self.toggle_selected_tasks_by_enter)
        self.task_listbox.bind("<KP_Enter>", self.toggle_selected_tasks_by_enter)
        self.task_listbox.bind("<ButtonPress-1>", self.on_task_drag_start)
        self.task_listbox.bind("<B1-Motion>", self.on_task_drag_motion)
        self.task_listbox.bind("<ButtonRelease-1>", self.on_task_drag_release)

        self.drag_index = None
        self.drag_kind = None
        self.drag_target_display_index = None
        self.drag_indicator = None
        self.drag_autoscroll_job = None
        self.drag_group_id = None
        self.selected_group_id = None
        self.group_expanded = {}
        self.group_names = {}
        self.group_order = []
        self.group_parents = {}
        self.group_children = {}
        self.group_colors = {}
        self.task_display_map = {}
        self.drag_handle_width = 34

        self.task_editor = ttk.LabelFrame(right, text="当前步骤")
        self.task_editor.pack(fill="both", expand=True)

        editor = ttk.Frame(self.task_editor, padding=12)
        editor.pack(fill="both", expand=True)

        self.template_var = tk.StringVar()
        self.description_var = tk.StringVar()
        self.timeout_var = tk.StringVar()
        self.offset_x_var = tk.StringVar()
        self.offset_y_var = tk.StringVar()
        self.click_x_var = tk.StringVar()
        self.click_y_var = tk.StringVar()
        self.region_left_var = tk.StringVar()
        self.region_top_var = tk.StringVar()
        self.region_right_var = tk.StringVar()
        self.region_bottom_var = tk.StringVar()
        self.region_center_x_var = tk.StringVar()
        self.region_center_y_var = tk.StringVar()
        self.next_template_var = tk.StringVar()
        self.wait_for_var = tk.StringVar(value="1. 画面结果变化")
        self.after_wait_var = tk.StringVar(value="0.25")
        self.click_var = tk.BooleanVar(value=True)
        self.match_required_var = tk.BooleanVar(value=True)
        self.optional_var = tk.BooleanVar(value=False)

        self.summary_var = tk.StringVar(value="未选择步骤")
        ttk.Label(editor, textvariable=self.summary_var, wraplength=620, justify="left", foreground="#374151").pack(anchor="w")

        action_buttons = ttk.Frame(editor)
        action_buttons.pack(fill="x", pady=(12, 10))
        self.action_buttons = action_buttons
        ttk.Button(action_buttons, text="绑定图片", command=self.select_task_image).pack(side="left", padx=(0, 8))
        ttk.Button(action_buttons, text="记录点击点", command=self.capture_current_click_position).pack(side="left", padx=(0, 8))
        ttk.Button(action_buttons, text="框选识别区域", command=self.capture_current_match_region).pack(side="left", padx=(0, 8))
        ttk.Button(action_buttons, text="清空识别区域", command=self.clear_current_match_regions).pack(side="left", padx=(0, 8))
        ttk.Button(action_buttons, text="应用修改", command=self.apply_selected_task).pack(side="left")

        self.mode_task_title_var = tk.StringVar(value="普通步骤")
        self.mode_task_summary_var = tk.StringVar(value="")
        self.special_task_form = ttk.Frame(editor)
        self.special_task_form.pack(fill="both", expand=True)
        self.special_task_form.pack_forget()

        self.special_task_container = ttk.Frame(self.special_task_form, padding=10)
        self.special_task_container.pack(fill="both", expand=True)

        self.task_form = ttk.Frame(editor)
        self.task_form.pack(fill="both", expand=True)

        form = ttk.Frame(self.task_form)
        form.pack(fill="both", expand=True)

        for field_name, variable, label in [
            ("模板名", self.template_var, "模板名"),
            ("描述", self.description_var, "描述"),
            ("X偏移", self.offset_x_var, "X偏移"),
            ("Y偏移", self.offset_y_var, "Y偏移"),
            ("点击X", self.click_x_var, "点击X"),
            ("点击Y", self.click_y_var, "点击Y"),
        ]:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=f"{label}:", width=14, anchor="w").pack(side="left")
            ttk.Entry(row, textvariable=variable, width=32).pack(side="left", fill="x", expand=True)

        region_pairs = [
            ("左上", self.region_left_var, self.region_top_var),
            ("右下", self.region_right_var, self.region_bottom_var),
            ("中心", self.region_center_x_var, self.region_center_y_var),
        ]
        for label_text, x_var, y_var in region_pairs:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=f"{label_text}:", width=14, anchor="w").pack(side="left")
            ttk.Label(row, text="X", width=3, anchor="w").pack(side="left")
            ttk.Entry(row, textvariable=x_var, width=14).pack(side="left", padx=(0, 8))
            ttk.Label(row, text="Y", width=3, anchor="w").pack(side="left")
            ttk.Entry(row, textvariable=y_var, width=14).pack(side="left", fill="x", expand=True)

        next_row = ttk.Frame(form)
        next_row.pack(fill="x", pady=4)
        ttk.Label(next_row, text="下一模板:", width=12, anchor="w").pack(side="left")
        ttk.Entry(next_row, textvariable=self.next_template_var, width=22).pack(side="left")
        ttk.Button(next_row, text="选择图片", command=self.select_next_template_image).pack(side="left", padx=(8, 0))
        ttk.Button(next_row, text="框选出现位置", command=self.capture_next_template_region).pack(side="left", padx=(8, 0))

        timeout_row = ttk.Frame(form)
        timeout_row.pack(fill="x", pady=4)
        ttk.Label(timeout_row, text="超时(秒，0为不限制):", width=20, anchor="w").pack(side="left")
        ttk.Entry(timeout_row, textvariable=self.timeout_var, width=10).pack(side="left")

        wait_row = ttk.Frame(form)
        wait_row.pack(fill="x", pady=4)
        ttk.Label(wait_row, text="等待方式:", width=12, anchor="w").pack(side="left")
        ttk.Combobox(wait_row, textvariable=self.wait_for_var, values=["1. 画面结果变化", "2. 等待目标模板出现", "3. 画面变化后目标结果出现"], state="readonly", width=22).pack(side="left")
        ttk.Label(wait_row, text="完成后等待(秒):").pack(side="left", padx=(12, 6))
        ttk.Entry(wait_row, textvariable=self.after_wait_var, width=10).pack(side="left")

        option_row = ttk.Frame(form)
        option_row.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(option_row, text="点击", variable=self.click_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(option_row, text="必须识别到图片再点击", variable=self.match_required_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(option_row, text="可选步骤（跳过）", variable=self.optional_var).pack(side="left", padx=(0, 16))
        ttk.Button(option_row, text="迂回", command=self.open_detour_editor).pack(side="left")

        self.group_form = ttk.Frame(editor)
        self.group_form.pack(fill="both", expand=True)
        self.group_name_var = tk.StringVar()
        self.group_color_var = tk.StringVar(value="#eaf1ff")
        self.group_expanded_var = tk.BooleanVar(value=True)
        self.group_enabled_var = tk.BooleanVar(value=True)

        group_form_inner = ttk.Frame(self.group_form, padding=10)
        group_form_inner.pack(fill="both", expand=True)

        ttk.Label(group_form_inner, text="组名称:", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Entry(group_form_inner, textvariable=self.group_name_var, width=24).pack(fill="x")

        ttk.Label(group_form_inner, text="组颜色:", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(14, 6))
        color_frame = ttk.Frame(group_form_inner)
        color_frame.pack(fill="x")
        ttk.Button(color_frame, text="HSV 颜色盘", command=self.choose_group_color).pack(side="left")
        ttk.Label(color_frame, textvariable=self.group_color_var, width=14, anchor="w", foreground="#374151").pack(side="left", padx=(8, 0))

        ttk.Label(group_form_inner, text="显示状态:", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(14, 6))
        ttk.Checkbutton(group_form_inner, text="展开该组", variable=self.group_expanded_var).pack(anchor="w")
        ttk.Checkbutton(group_form_inner, text="启用该组（同步启用组下所有步骤）", variable=self.group_enabled_var).pack(anchor="w", pady=(6, 0))

        ttk.Button(group_form_inner, text="应用组设置", command=self.apply_group_settings).pack(anchor="w", pady=(18, 0))

        self.group_form.pack_forget()
        self.task_form.pack(fill="both", expand=True)
        self.refresh_task_list()

        log_frame = ttk.LabelFrame(self.root, text="日志输出")
        log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_box = ScrolledText(log_frame, height=12, state="disabled", wrap="word", padx=8, pady=8)
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)

        self.refresh_window_list()

    def on_mode_selected(self, event=None):
        mode = self.mode_var.get() or "custom"
        self.save_current_tasks()
        self.current_mode = mode
        if mode == "custom":
            TASKS[:] = deepcopy(self.mode_tasks[mode])
        else:
            self.mode_tasks.setdefault(mode, deepcopy(get_tasks_for_mode(mode)))
            TASKS[:] = deepcopy(self.mode_tasks[mode])
        self._restore_group_metadata(mode)
        self.selected_group_id = None
        self.selected_task_index = 0
        self.refresh_task_list()

    def refresh_mode_values(self):
        modes = ["custom"] + sorted(name for name in self.mode_tasks if name != "custom")
        self.mode_combo["values"] = modes
        self.mode_var.set(self.current_mode if self.current_mode in modes else "custom")

    def create_preset(self):
        name = tk.StringVar()
        win = tk.Toplevel(self.root)
        win.title("新建预设")
        win.geometry("300x130")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="预设名称:").pack(anchor="w", padx=12, pady=(12, 6))
        ttk.Entry(win, textvariable=name, width=28).pack(padx=12, fill="x")

        def confirm():
            preset_name = name.get().strip()
            if not preset_name or preset_name.lower() == "custom":
                messagebox.showwarning("名称无效", "请输入非 custom 的预设名称。")
                return
            if preset_name in self.mode_tasks:
                messagebox.showwarning("名称重复", "该预设已经存在。")
                return
            self.deleted_preset_names.discard(preset_name)
            self.mode_tasks[preset_name] = []
            self.save_all_presets()
            self.refresh_mode_values()
            self.mode_var.set(preset_name)
            win.destroy()
            self.on_mode_selected()

        ttk.Button(win, text="创建", command=confirm).pack(pady=12)

    def rename_current_preset(self):
        old_name = self.current_mode
        if old_name == "custom":
            messagebox.showwarning("无法重命名", "custom 是自定义任务，不能重命名。")
            return

        new_name_var = tk.StringVar(value=old_name)
        win = tk.Toplevel(self.root)
        win.title("重命名预设")
        win.geometry("320x140")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=f"预设名称（当前：{old_name}）:").pack(anchor="w", padx=12, pady=(12, 6))
        entry = ttk.Entry(win, textvariable=new_name_var, width=30)
        entry.pack(padx=12, fill="x")
        entry.focus_set()
        entry.select_range(0, tk.END)

        def confirm():
            new_name = new_name_var.get().strip()
            if not new_name or new_name.lower() == "custom":
                messagebox.showwarning("名称无效", "请输入非 custom 的预设名称。", parent=win)
                return
            if new_name != old_name and new_name in self.mode_tasks:
                messagebox.showwarning("名称重复", "该预设已经存在。", parent=win)
                return
            if new_name == old_name:
                win.destroy()
                return

            self.save_current_tasks()
            self.mode_tasks[new_name] = self.mode_tasks.pop(old_name)
            if old_name in self.mode_group_metadata:
                self.mode_group_metadata[new_name] = self.mode_group_metadata.pop(old_name)
            self.deleted_preset_names.discard(new_name)
            self.deleted_preset_names.discard(old_name)
            self.current_mode = new_name
            self.mode_var.set(new_name)
            self.save_all_presets()
            self.refresh_mode_values()
            self.mode_var.set(new_name)
            win.destroy()
            self.append_log(f"已将预设“{old_name}”重命名为“{new_name}”。")

        ttk.Button(win, text="保存", command=confirm).pack(pady=12)

    def copy_current_preset(self):
        selected_entries = [
            self.task_display_map[index]
            for index in self.task_listbox.curselection()
            if index in self.task_display_map
        ]
        if not selected_entries:
            messagebox.showwarning("无法复制", "请先选择要复制的组或步骤。")
            return

        target_var = tk.StringVar()
        target_names = [name for name in self.mode_tasks if name != self.current_mode]
        win = tk.Toplevel(self.root)
        win.title("复制到预设")
        win.geometry("360x190")
        win.transient(self.root)
        win.grab_set()
        selected_count = len(selected_entries)
        ttk.Label(win, text=f"已选择 {selected_count} 个组/步骤，复制到:").pack(anchor="w", padx=12, pady=(12, 6))
        target_combo = ttk.Combobox(win, textvariable=target_var, values=sorted(target_names), state="readonly", width=26)
        target_combo.pack(padx=12, fill="x")
        if target_names:
            target_combo.current(0)

        def confirm():
            target_name = target_var.get().strip()
            if not target_name:
                messagebox.showwarning("无法复制", "请先创建其它预设作为复制目标。")
                return
            if not messagebox.askyesno("确认追加", f"确定将选中的组/步骤追加到预设“{target_name}”吗？"):
                return
            self.save_current_tasks()
            copied_tasks = self._copy_selected_entries(selected_entries, self.mode_tasks[target_name])
            if not copied_tasks:
                messagebox.showwarning("无法复制", "选中的组或步骤没有可复制的内容。")
                return
            self.mode_tasks[target_name].extend(copied_tasks)
            self.save_all_presets()
            win.destroy()
            self.mode_var.set(target_name)
            self.on_mode_selected()
            self.append_log(f"已将 {len(copied_tasks)} 个步骤复制到预设“{target_name}”。")

        ttk.Button(win, text="复制并切换", command=confirm).pack(pady=12)

    def _copy_selected_entries(self, selected_entries, target_tasks):
        selected_task_indices = set()
        selected_group_ids = set()
        for kind, value in selected_entries:
            if kind == "task":
                selected_task_indices.add(value)
            elif kind == "group":
                group_id = str(value)
                selected_group_ids.add(group_id)
                selected_group_ids.update(str(item) for item in self._get_group_descendants(group_id))

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
        for task in source_tasks:
            copied_task = deepcopy(task)
            source_group_id = str(task.get("group_id") or "group_default")
            if source_group_id in group_id_map:
                copied_task["group_id"] = group_id_map[source_group_id]
                copied_task["group_name"] = f"{task.get('group_name', '默认分组')} 复制"
            copied_task["id"] = f"{task.get('id', 'task')}_copy"
            copied_tasks.append(copied_task)
        return copied_tasks

    def delete_current_preset(self):
        preset_name = self.current_mode
        if preset_name == "custom":
            messagebox.showwarning("无法删除", "custom 是自定义任务，不能作为预设删除。")
            return
        if not messagebox.askyesno("确认删除", f"确定删除预设“{preset_name}”及其全部步骤吗？"):
            return

        self.save_current_tasks()
        self.mode_tasks.pop(preset_name, None)
        self.deleted_preset_names.add(preset_name)
        self.save_all_presets()
        self.current_mode = "custom"
        self.mode_var.set("custom")
        self.refresh_mode_values()
        TASKS[:] = deepcopy(self.mode_tasks["custom"])
        self.selected_group_id = None
        self.selected_task_index = 0
        self.refresh_task_list()
        self.append_log(f"已删除预设“{preset_name}”。")

    def toggle_selected_tasks_by_enter(self, event=None):
        selected_indices = list(self.task_listbox.curselection())
        if not selected_indices:
            return "break"

        task_indices = set()
        group_ids = set()
        for display_index in selected_indices:
            entry = self.task_display_map.get(display_index)
            if not entry:
                continue
            if entry[0] == "task":
                task_indices.add(entry[1])
            elif entry[0] == "group":
                group_id = str(entry[1])
                group_ids.add(group_id)
                group_ids.update(str(item) for item in self._get_group_descendants(group_id))

        for index, task in enumerate(TASKS):
            if index in task_indices or str(task.get("group_id") or "group_default") in group_ids:
                task["enabled"] = not bool(task.get("enabled", True))

        self.save_current_tasks()
        self.refresh_task_list()
        for display_index, entry in self.task_display_map.items():
            if (entry[0] == "task" and entry[1] in task_indices) or (entry[0] == "group" and str(entry[1]) in group_ids):
                self.task_listbox.selection_set(display_index)
        self.append_log(f"已切换 {len(task_indices)} 个选中步骤的启用状态。")
        return "break"

    def save_all_presets(self):
        presets = {key: value for key, value in self.mode_tasks.items() if key != "custom"}
        presets["__deleted__"] = sorted(self.deleted_preset_names)
        presets["__group_metadata__"] = deepcopy(self.mode_group_metadata)
        save_presets(presets)

    def refresh_window_list(self):
        titles = [w.title for w in gw.getAllWindows() if w.title.strip()]
        if titles:
            self.append_log(f"当前可见窗口: {titles[:10]}")
            self.window_combo['values'] = titles
            self.window_var.set(titles[0])
        else:
            self.window_combo['values'] = []
            self.window_var.set("")
            self.append_log("未检测到可见窗口")

    def show_group_editor(self):
        if self.selected_group_id is None:
            return
        self.action_buttons.pack_forget()
        group_id = str(self.selected_group_id)
        self.group_name_var.set(self.group_names.get(group_id, "默认分组"))
        self.group_color_var.set(self.group_colors.get(group_id, "#eaf1ff"))
        self.group_expanded_var.set(bool(self.group_expanded.get(group_id, True)))
        group_ids = {group_id, *self._get_group_descendants(group_id)}
        group_tasks = [task for task in TASKS if str(task.get("group_id") or "group_default") in group_ids]
        self.group_enabled_var.set(bool(group_tasks) and all(task.get("enabled", True) for task in group_tasks))
        self.group_form.pack(fill="both", expand=True)
        self.task_form.pack_forget()
        self.special_task_form.pack_forget()

    def _render_special_task_config(self, task):
        for child in self.special_task_container.winfo_children():
            child.destroy()

        ttk.Label(self.special_task_container, textvariable=self.mode_task_title_var, font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
        ttk.Label(self.special_task_container, textvariable=self.mode_task_summary_var, wraplength=520, justify="left", foreground="#374151").pack(anchor="w", pady=(8, 12))

        task_type = task.get("type", "normal")
        config_frame = ttk.Frame(self.special_task_container)
        config_frame.pack(fill="both", expand=True)

        if task_type == "keyboard_move":
            name_var = tk.StringVar(value=str(task.get("description", "每日移动步骤")))
            key_var = tk.StringVar(value="W")
            duration_var = tk.StringVar(value="1.0")
            delay_before_var = tk.StringVar(value=str(task.get("delay_before", 0.0)))
            after_wait_var = tk.StringVar(value=str(task.get("after_wait", 0.0)))
            step_list = tk.Listbox(config_frame, height=8, exportselection=False)
            step_list.pack(fill="x", pady=(0, 8))

            row = ttk.Frame(config_frame)
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text="步骤名称:").pack(side="left", padx=(0, 8))
            ttk.Entry(row, textvariable=name_var, width=22).pack(side="left")

            def sync_move_steps_from_list():
                steps = []
                for i in range(step_list.size()):
                    text = step_list.get(i).strip()
                    if not text:
                        continue
                    parts = text.split()
                    if len(parts) >= 2:
                        key = parts[0]
                        try:
                            duration = float(parts[1].rstrip("sS"))
                        except ValueError:
                            duration = 1.0
                        steps.append({"key": key, "duration": duration})
                task["move_steps"] = steps

            for step in task.get("move_steps", []):
                step_list.insert(tk.END, f"{step.get('key','W')} {step.get('duration', 1.0)}s")

            drag_index = {"value": None}

            def load_selected_step(_event=None):
                selection = step_list.curselection()
                if selection:
                    parts = step_list.get(selection[0]).split()
                    key_var.set(parts[0])
                    duration_var.set(parts[1].rstrip("sS") if len(parts) > 1 else "1.0")

            def update_selected_step():
                selection = step_list.curselection()
                if not selection:
                    return
                try:
                    duration = float(duration_var.get() or 1.0)
                except ValueError:
                    duration = 1.0
                step_list.delete(selection[0])
                step_list.insert(selection[0], f"{key_var.get()} {duration}s")
                step_list.selection_set(selection[0])
                sync_move_steps_from_list()

            def start_drag(event):
                drag_index["value"] = step_list.nearest(event.y)

            def drag_step(event):
                source = drag_index["value"]
                target = step_list.nearest(event.y)
                if source is None or target == source or target < 0 or target >= step_list.size():
                    return
                value = step_list.get(source)
                step_list.delete(source)
                step_list.insert(target, value)
                step_list.selection_set(target)
                drag_index["value"] = target

            def finish_drag(_event):
                drag_index["value"] = None
                sync_move_steps_from_list()

            step_list.bind("<<ListboxSelect>>", load_selected_step)
            step_list.bind("<ButtonPress-1>", start_drag)
            step_list.bind("<B1-Motion>", drag_step)
            step_list.bind("<ButtonRelease-1>", finish_drag)

            row = ttk.Frame(config_frame)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text="按键:").pack(side="left", padx=(0, 8))
            ttk.Combobox(row, textvariable=key_var, values=["W", "A", "S", "D", "Q", "E", "R", "F", "Z", "X", "C", "V"], state="readonly", width=10).pack(side="left")
            ttk.Label(row, text="持续时间(秒):").pack(side="left", padx=(8, 8))
            ttk.Entry(row, textvariable=duration_var, width=10).pack(side="left")

            timing_row = ttk.Frame(config_frame)
            timing_row.pack(fill="x", pady=4)
            ttk.Label(timing_row, text="执行前延时(秒):").pack(side="left", padx=(0, 8))
            ttk.Entry(timing_row, textvariable=delay_before_var, width=10).pack(side="left")
            ttk.Label(timing_row, text="执行后等待(秒):").pack(side="left", padx=(16, 8))
            ttk.Entry(timing_row, textvariable=after_wait_var, width=10).pack(side="left")

            def add_step():
                key = key_var.get()
                try:
                    duration = float(duration_var.get() or 1.0)
                except ValueError:
                    duration = 1.0
                step_list.insert(tk.END, f"{key} {duration}s")
                sync_move_steps_from_list()

            def remove_step():
                selection = step_list.curselection()
                if not selection:
                    return
                idx = selection[0]
                step_list.delete(idx)
                sync_move_steps_from_list()

            btn_row = ttk.Frame(config_frame)
            btn_row.pack(fill="x", pady=(8, 0))
            ttk.Button(btn_row, text="添加步骤", command=add_step).pack(side="left", padx=(0, 8))
            ttk.Button(btn_row, text="更新选中步骤", command=update_selected_step).pack(side="left", padx=(0, 8))
            ttk.Button(btn_row, text="删除步骤", command=remove_step).pack(side="left")

            def save_keyboard_move():
                sync_move_steps_from_list()
                task["description"] = name_var.get().strip() or task.get("description", "每日移动步骤")
                task["delay_before"] = max(0.0, float(delay_before_var.get() or 0.0))
                task["after_wait"] = max(0.0, float(after_wait_var.get() or 0.0))
                task.pop("trigger_action", None)
                task.pop("action_key", None)
                task.pop("exit_key", None)
                task.pop("exit_after", None)
                self.save_current_tasks(); self.refresh_task_list(); self.load_task_to_form(self.selected_task_index)

            ttk.Button(config_frame, text="保存移动设置", command=save_keyboard_move).pack(anchor="w", pady=(12, 0))

        elif task_type == "key_press":
            name_var = tk.StringVar(value=str(task.get("description", "按键步骤")))
            key_var = tk.StringVar(value=str(task.get("key") or task.get("template") or "E"))
            hold_var = tk.StringVar(value=str(task.get("hold_time", 0.1)))
            delay_var = tk.StringVar(value=str(task.get("delay_before", 0.0)))

            row = ttk.Frame(config_frame)
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text="步骤名称:").pack(side="left", padx=(0, 8))
            ttk.Entry(row, textvariable=name_var, width=22).pack(side="left")

            after_wait_var = tk.StringVar(value=str(task.get("after_wait", 0.2)))
            for label_text, var in [("按键", key_var), ("执行前延时(秒)", delay_var), ("按住时长(秒)", hold_var), ("执行后等待(秒)", after_wait_var)]:
                row = ttk.Frame(config_frame)
                row.pack(fill="x", pady=6)
                ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8))
                ttk.Entry(row, textvariable=var, width=20).pack(side="left")

            def save_key_press():
                task["description"] = name_var.get().strip() or task.get("description", "按键步骤")
                task["key"] = (key_var.get().strip() or "E").upper()
                task["template"] = task["key"]
                task["delay_before"] = max(0.0, float(delay_var.get() or 0.0))
                task["hold_time"] = float(hold_var.get() or 0.1)
                task["after_wait"] = max(0.0, float(after_wait_var.get() or 0.0))
                self.save_current_tasks(); self.refresh_task_list(); self.load_task_to_form(self.selected_task_index)

            ttk.Button(config_frame, text="保存按键设置", command=save_key_press).pack(anchor="w", pady=(8, 0))

        elif task_type == "drag":
            name_var = tk.StringVar(value=str(task.get("description", "拖曳步骤")))
            start_x_var = tk.StringVar(value=str(task.get("start_x", 0)))
            start_y_var = tk.StringVar(value=str(task.get("start_y", 0)))
            end_x_var = tk.StringVar(value=str(task.get("end_x", 100)))
            end_y_var = tk.StringVar(value=str(task.get("end_y", 100)))
            duration_var = tk.StringVar(value=str(task.get("duration", 0.25)))

            row = ttk.Frame(config_frame)
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text="步骤名称:").pack(side="left", padx=(0, 8))
            ttk.Entry(row, textvariable=name_var, width=22).pack(side="left")

            for label_text, var in [("起点X", start_x_var), ("起点Y", start_y_var), ("终点X", end_x_var), ("终点Y", end_y_var), ("拖动时长(秒)", duration_var)]:
                row = ttk.Frame(config_frame)
                row.pack(fill="x", pady=6)
                ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8))
                ttk.Entry(row, textvariable=var, width=18).pack(side="left")

            def save_drag():
                task["description"] = name_var.get().strip() or task.get("description", "拖曳步骤")
                task["start_x"] = float(start_x_var.get() or 0)
                task["start_y"] = float(start_y_var.get() or 0)
                task["end_x"] = float(end_x_var.get() or 100)
                task["end_y"] = float(end_y_var.get() or 100)
                task["duration"] = float(duration_var.get() or 0.25)
                task["template"] = "drag"
                self.save_current_tasks(); self.refresh_task_list(); self.load_task_to_form(self.selected_task_index)

            ttk.Button(config_frame, text="保存拖曳设置", command=save_drag).pack(anchor="w", pady=(8, 0))

        elif task_type == "click_until_gone":
            name_var = tk.StringVar(value=str(task.get("description", "持续点击直到识别步骤")))
            saved_templates = task.get("templates") or task.get("template", "")
            if isinstance(saved_templates, (list, tuple)):
                saved_templates = ", ".join(str(item) for item in saved_templates)
            template_var = tk.StringVar(value=str(saved_templates))
            interval_var = tk.StringVar(value=str(task.get("click_interval", 0.5)))
            timeout_var = tk.StringVar(value=str(task.get("timeout", 30)))
            stop_delay_var = tk.StringVar(value=str(task.get("stop_delay", 0.0)))
            continue_timeout_var = tk.BooleanVar(value=bool(task.get("continue_after_timeout", False)))
            stop_on_change_var = tk.BooleanVar(value=bool(task.get("stop_on_change", False)))

            for label_text, var in [
                ("步骤名称", name_var),
                ("绑定图片", template_var),
                ("点击间隔(秒)", interval_var),
                ("超时(秒，0为不限制)", timeout_var),
            ]:
                row = ttk.Frame(config_frame)
                row.pack(fill="x", pady=6)
                ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8))
                ttk.Entry(row, textvariable=var, width=24).pack(side="left")

            action_row = ttk.Frame(config_frame)
            action_row.pack(fill="x", pady=(8, 4))

            def bind_image():
                file_paths = filedialog.askopenfilenames(
                    title="选择持续点击图片",
                    initialdir=os.path.join(os.getcwd(), "icons"),
                    filetypes=[("PNG Images", "*.png"), ("All Files", "*.*")],
                )
                if file_paths:
                    current_names = [item.strip() for item in template_var.get().replace("，", ",").split(",") if item.strip()]
                    for file_path in file_paths:
                        image_name = os.path.splitext(os.path.basename(file_path))[0]
                        if image_name not in current_names:
                            current_names.append(image_name)
                    template_var.set(", ".join(current_names))

            ttk.Button(action_row, text="绑定图片", command=bind_image).pack(side="left", padx=(0, 8))
            ttk.Button(action_row, text="记录点击点", command=self.capture_current_click_position).pack(side="left", padx=(0, 8))
            ttk.Button(action_row, text="框选识别区域", command=self.capture_current_match_region).pack(side="left")
            ttk.Button(action_row, text="清空识别区域", command=self.clear_current_match_regions).pack(side="left", padx=(8, 0))
            ttk.Checkbutton(
                config_frame,
                text="超时后继续执行下一步骤",
                variable=continue_timeout_var,
            ).pack(anchor="w", pady=(4, 0))
            change_row = ttk.Frame(config_frame)
            change_row.pack(fill="x", pady=(4, 0))
            ttk.Checkbutton(
                change_row,
                text="画面发生变化后视为识别成功",
                variable=stop_on_change_var,
            ).pack(side="left")
            ttk.Label(change_row, text="完成后等待(秒):").pack(side="left", padx=(16, 8))
            ttk.Entry(change_row, textvariable=stop_delay_var, width=10).pack(side="left")

            def save_click_until_gone():
                task["description"] = name_var.get().strip() or "持续点击直到识别步骤"
                template_names = [item.strip() for item in template_var.get().replace("，", ",").split(",") if item.strip()]
                task["templates"] = template_names
                task["template"] = template_names[0] if template_names else ""
                task["click_interval"] = max(0.01, float(interval_var.get() or 0.5))
                task["stop_delay"] = max(0.0, float(stop_delay_var.get() or 0.0))
                task["timeout"] = max(0.0, float(timeout_var.get() or 30))
                task["continue_after_timeout"] = bool(continue_timeout_var.get())
                task["stop_on_change"] = bool(stop_on_change_var.get())
                task["click"] = True
                task["required"] = True
                self.save_current_tasks()
                self.refresh_task_list()
                self.load_task_to_form(self.selected_task_index)
                self.append_log(f"已保存持续点击设置: {task['template']}")

            ttk.Button(config_frame, text="保存持续点击设置", command=save_click_until_gone).pack(anchor="w", pady=(8, 0))

    def show_task_editor(self, task=None):
        self.group_form.pack_forget()
        if task is None:
            if not (0 <= self.selected_task_index < len(TASKS)):
                self.task_form.pack_forget()
                self.special_task_form.pack_forget()
                return
            task = TASKS[self.selected_task_index]

        task_type = task.get("type", "normal")
        if task_type in ("normal", "advanced"):
            self.special_task_form.pack_forget()
            self.action_buttons.pack(fill="x", pady=(12, 10))
            self.task_form.pack(fill="both", expand=True)
            return

        self.action_buttons.pack_forget()
        self.task_form.pack_forget()
        self.special_task_form.pack(fill="both", expand=True)

        if task_type == "keyboard_move":
            steps = task.get("move_steps", [])
            self.mode_task_title_var.set("每日移动步骤")
            self.mode_task_summary_var.set(f"移动步数: {len(steps)}\n仅执行键盘移动序列，不触发额外动作")
        elif task_type == "key_press":
            self.mode_task_title_var.set("按键步骤")
            self.mode_task_summary_var.set(f"按键: {task.get('key', 'E')}\n按住时长: {task.get('hold_time', 0.1)} 秒")
        elif task_type == "drag":
            self.mode_task_title_var.set("拖曳步骤")
            self.mode_task_summary_var.set(
                f"起点: ({task.get('start_x', 0)}, {task.get('start_y', 0)})\n"
                f"终点: ({task.get('end_x', 100)}, {task.get('end_y', 100)})\n"
                f"时长: {task.get('duration', 0.25)} 秒"
            )
        elif task_type == "click_until_gone":
            self.mode_task_title_var.set("持续点击直到识别步骤")
            self.mode_task_summary_var.set(
                f"绑定图片: {task.get('template', '-')}\n"
                f"点击间隔: {task.get('click_interval', 0.5)} 秒\n"
                f"识别结束后等待 {task.get('stop_delay', 0.0)} 秒才停止点击\n"
                f"超时: {task.get('timeout', 30)} 秒"
            )
        else:
            self.mode_task_title_var.set("自定义步骤")
            self.mode_task_summary_var.set(f"模板: {task.get('template', '-')}\n描述: {task.get('description', '-')}")

        self._render_special_task_config(task)

    def open_selected_task_detail_settings(self):
        return

    def _hex_to_hsv(self, hex_color):
        hex_color = (hex_color or "#eaf1ff").strip()
        if not hex_color.startswith("#"):
            hex_color = "#" + hex_color
        hex_color = hex_color[:7]
        if len(hex_color) != 7:
            return (0.0, 0.0, 1.0)
        try:
            red = int(hex_color[1:3], 16) / 255.0
            green = int(hex_color[3:5], 16) / 255.0
            blue = int(hex_color[5:7], 16) / 255.0
            return colorsys.rgb_to_hsv(red, green, blue)
        except ValueError:
            return (0.0, 0.0, 1.0)

    def _hsv_to_hex(self, hue, saturation, value):
        r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
        return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))

    def choose_group_color(self):
        current_hsv = self._hex_to_hsv(self.group_color_var.get() or "#eaf1ff")
        current_h, current_s, current_v = current_hsv

        win = tk.Toplevel(self.root)
        win.title("组颜色选择")
        win.geometry("320x240")
        win.transient(self.root)
        win.grab_set()

        form = ttk.Frame(win, padding=12)
        form.pack(fill="both", expand=True)

        h_var = tk.DoubleVar(value=current_h * 360)
        s_var = tk.DoubleVar(value=current_s * 100)
        v_var = tk.DoubleVar(value=current_v * 100)

        preview = tk.Label(win, text="", width=28, height=2, bg=self.group_color_var.get() or "#eaf1ff", relief="solid", borderwidth=1)
        preview.pack(fill="x", padx=12, pady=(0, 8))

        def update_preview():
            h = h_var.get() / 360.0
            s = s_var.get() / 100.0
            v = v_var.get() / 100.0
            color_hex = self._hsv_to_hex(h, s, v)
            self.group_color_var.set(color_hex)
            preview.configure(bg=color_hex)

        for label, variable, min_value, max_value in [
            ("Hue", h_var, 0, 360),
            ("Saturation", s_var, 0, 100),
            ("Value", v_var, 0, 100),
        ]:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=6)
            ttk.Label(row, text=f"{label}:", width=10, anchor="w").pack(side="left")
            ttk.Scale(row, from_=min_value, to=max_value, variable=variable, command=lambda *_: update_preview()).pack(side="left", fill="x", expand=True, padx=(8, 0))
            tk.Label(row, text=f"{int(variable.get())}", width=5, anchor="e").pack(side="left", padx=(8, 0))

        ttk.Button(win, text="确定", command=win.destroy).pack(pady=(0, 12))
        update_preview()

    def apply_group_settings(self):
        if self.selected_group_id is None:
            return
        group_id = str(self.selected_group_id)
        new_name = self.group_name_var.get().strip() or self.group_names.get(group_id, "默认分组")
        self.group_names[group_id] = new_name
        color_value = self.group_color_var.get().strip() or "#eaf1ff"
        self.group_colors[group_id] = color_value
        self.group_expanded[group_id] = bool(self.group_expanded_var.get())
        group_ids = {group_id, *self._get_group_descendants(group_id)}
        for task in TASKS:
            if str(task.get("group_id") or "group_default") in group_ids:
                task["group_name"] = new_name
                task["group_color"] = color_value
                task["enabled"] = bool(self.group_enabled_var.get())
        self.save_current_tasks()
        self.refresh_task_list()
        self.append_log(f"已更新组设置: {new_name}")

    def on_task_select(self, event=None):
        selected = list(self.task_listbox.curselection())
        if not selected:
            return
        entry = self.task_display_map.get(selected[0])
        if not entry:
            self.selected_group_id = None
            self.selected_task_index = 0
            self.clear_task_form()
            self.show_task_editor()
            return

        if entry[0] == "group":
            self.selected_group_id = str(entry[1])
            self.selected_task_index = None
            self.summary_var.set(f"已选择组: {self.group_names.get(self.selected_group_id, '默认分组')}")
            self.show_group_editor()
            return

        self.selected_group_id = None
        selected_index = entry[1]
        if not (0 <= selected_index < len(TASKS)):
            self.selected_task_index = 0
            self.clear_task_form()
            self.show_task_editor()
            return
        self.selected_task_index = selected_index
        self.show_task_editor()
        self.load_task_to_form(self.selected_task_index)

    def _get_default_group_id(self):
        if self.group_order:
            return str(self.group_order[0])
        for task in TASKS:
            group_id = task.get("group_id")
            if group_id:
                return str(group_id)
        return "group_default"

    def _reconcile_group_hierarchy(self):
        self.group_expanded = self.group_expanded or {}
        self.group_names = self.group_names or {}
        self.group_order = list(self.group_order or [])
        self.group_parents = self.group_parents or {}
        self.group_children = self.group_children or {}

        for group_id in list(self.group_names.keys()):
            self.group_parents.setdefault(group_id, None)
            self.group_children.setdefault(group_id, [])

        for group_id in list(self.group_names.keys()):
            parent_id = self.group_parents.get(group_id)
            if parent_id is not None and parent_id not in self.group_names:
                self.group_parents[group_id] = None
                parent_id = None
            if parent_id is not None:
                self.group_children.setdefault(parent_id, [])
                if group_id not in self.group_children[parent_id]:
                    self.group_children[parent_id].append(group_id)
                if group_id in self.group_order:
                    self.group_order.remove(group_id)
            else:
                if group_id not in self.group_order:
                    self.group_order.append(group_id)

        for group_id in list(self.group_children.keys()):
            valid_children = []
            for child_id in self.group_children.get(group_id, []):
                if child_id in self.group_names and self.group_parents.get(child_id) == group_id:
                    valid_children.append(child_id)
            self.group_children[group_id] = valid_children

        self.group_order = [group_id for group_id in self.group_order if group_id in self.group_names and self.group_parents.get(group_id) is None]

        for group_id in list(self.group_names.keys()):
            self.group_names.setdefault(group_id, "默认分组")
            self.group_expanded.setdefault(group_id, True)
            self.group_parents.setdefault(group_id, None)
            self.group_children.setdefault(group_id, [])

    def _ensure_group_metadata(self):
        self._reconcile_group_hierarchy()

        for task in TASKS:
            group_id = str(task.get("group_id") or self._get_default_group_id())
            task["group_id"] = group_id
            group_name = str(task.get("group_name") or self.group_names.get(group_id) or "默认分组")
            task["group_name"] = group_name
            if task.get("group_color"):
                self.group_colors[group_id] = str(task["group_color"])
            self._ensure_group_exists(group_id=group_id, group_name=group_name, parent_id=self.group_parents.get(group_id))

        self._reconcile_group_hierarchy()

    def _get_group_task_indices(self, group_id):
        indices = [idx for idx, task in enumerate(TASKS) if str(task.get("group_id")) == str(group_id)]
        return indices

    def _get_group_enabled_status(self, group_id):
        group_ids = {str(group_id), *self._get_group_descendants(group_id)}
        group_tasks = [
            task for task in TASKS
            if str(task.get("group_id") or "group_default") in group_ids
        ]
        if not group_tasks:
            return "空"
        enabled_count = sum(1 for task in group_tasks if task.get("enabled", True))
        if enabled_count == len(group_tasks):
            return "已启用"
        if enabled_count > 0:
            return "已部分启用"
        return "已停用"

    def _get_group_descendants(self, group_id):
        descendants = []
        for child_id in self.group_children.get(group_id, []):
            descendants.append(child_id)
            descendants.extend(self._get_group_descendants(child_id))
        return descendants

    def _get_group_siblings(self, group_id):
        parent_id = self.group_parents.get(group_id)
        if parent_id is None:
            return list(self.group_order)
        return list(self.group_children.get(parent_id, []))

    def _attach_group_to_parent(self, group_id, parent_id):
        group_id = str(group_id)
        parent_id = None if parent_id is None else str(parent_id)
        if group_id == parent_id:
            return

        current_parent = self.group_parents.get(group_id)
        if current_parent is not None and current_parent in self.group_children:
            self.group_children[current_parent] = [child for child in self.group_children.get(current_parent, []) if child != group_id]
        if current_parent is not None and group_id in self.group_order:
            self.group_order = [g for g in self.group_order if g != group_id]

        self.group_parents[group_id] = parent_id
        if parent_id is None:
            if group_id not in self.group_order:
                self.group_order.append(group_id)
            return

        self.group_children.setdefault(parent_id, [])
        if group_id not in self.group_children[parent_id]:
            self.group_children[parent_id].append(group_id)
        if group_id in self.group_order:
            self.group_order = [g for g in self.group_order if g != group_id]

    def _build_task_display_map(self):
        self.task_display_map = {}
        display_index = 0

        def walk_group(parent_id, depth):
            nonlocal display_index
            sibling_ids = self.group_order if parent_id is None else self.group_children.get(parent_id, [])
            for group_id in sibling_ids:
                indent = "  " * depth
                self.task_display_map[display_index] = ("group", group_id)
                prefix = "▼" if self.group_expanded.get(group_id, True) else "▶"
                group_status = self._get_group_enabled_status(group_id)
                self.task_listbox.insert(tk.END, f"{indent}{prefix}  {self.group_names.get(group_id, '默认分组')} · {group_status}")
                display_index += 1

                if self.group_expanded.get(group_id, True):
                    for task_index in self._get_group_task_indices(group_id):
                        task = TASKS[task_index]
                        state = "✓" if task.get("enabled", True) else " "
                        check = "☑" if task.get("enabled", True) else "☐"
                        detour_status = " · 已启用迂回" if task.get("detour_enabled") else ""
                        label = f"{indent}  {check}  {task_index + 1}. {task.get('type', 'normal')} · {task.get('description', task.get('template', 'unknown'))}{detour_status}"
                        self.task_display_map[display_index] = ("task", task_index)
                        self.task_listbox.insert(tk.END, label)
                        display_index += 1
                    for child_group_id in self.group_children.get(group_id, []):
                        walk_group(group_id, depth + 1)

        walk_group(None, 0)
        return display_index

    def _resolve_display_selection(self, display_index):
        entry = self.task_display_map.get(display_index)
        if not entry:
            return None
        kind, value = entry
        if kind == "group":
            group_indices = self._get_group_task_indices(value)
            return group_indices[0] if group_indices else None
        return value

    def toggle_selected_task(self, event=None):
        selection = list(self.task_listbox.curselection())
        if not selection:
            return "break"

        entry = self.task_display_map.get(selection[0])
        if entry is None:
            return "break"

        if entry[0] == "group":
            group_id = entry[1]
            self.group_expanded[group_id] = not bool(self.group_expanded.get(group_id, True))
            self.refresh_task_list()
            self.task_listbox.selection_set(selection[0])
            return "break"

        selected_index = entry[1]
        if not (0 <= selected_index < len(TASKS)):
            return "break"

        task = TASKS[selected_index]
        task["enabled"] = not bool(task.get("enabled", True))
        self.refresh_task_list()
        self.task_listbox.selection_set(self._find_display_index_for_task(selected_index))
        self.append_log(f"步骤 {selected_index + 1} 已{'启用' if task['enabled'] else '禁用'}")
        return "break"

    def _find_display_index_for_task(self, task_index):
        for display_index, entry in self.task_display_map.items():
            if entry[0] == "task" and entry[1] == task_index:
                return display_index
        return 0

    def _default_group_name(self, count):
        return f"组 {count + 1}"

    def _ensure_group_exists(self, group_id=None, group_name=None, parent_id=None):
        if group_id is None:
            group_id = f"group_{uuid.uuid4().hex[:8]}"
        group_id = str(group_id)
        group_name = group_name or self.group_names.get(group_id) or self._default_group_name(len(self.group_names))
        self.group_names[group_id] = group_name
        self.group_expanded.setdefault(group_id, True)
        self.group_parents.setdefault(group_id, None)
        self.group_children.setdefault(group_id, [])

        if parent_id is not None:
            parent_id = str(parent_id)
            self.group_parents[group_id] = parent_id
            self.group_children.setdefault(parent_id, [])
            if group_id not in self.group_children[parent_id]:
                self.group_children[parent_id].append(group_id)
            if group_id in self.group_order:
                self.group_order.remove(group_id)
        else:
            self.group_parents[group_id] = None
            if group_id not in self.group_order:
                self.group_order.append(group_id)
        return group_id

    def add_group(self):
        selected = list(self.task_listbox.curselection())
        parent_group_id = None
        if selected:
            entry = self.task_display_map.get(selected[0])
            if entry and entry[0] == "group":
                parent_group_id = entry[1]

        group_id = self._ensure_group_exists(parent_id=parent_group_id)
        self.save_current_tasks()
        self.refresh_task_list()
        self.task_listbox.selection_clear(0, tk.END)
        self.append_log(f"已新增组: {self.group_names.get(group_id, '新组')}")

    def rename_group(self):
        selected = list(self.task_listbox.curselection())
        if not selected:
            return
        display_index = selected[0]
        entry = self.task_display_map.get(display_index)
        if not entry or entry[0] != "group":
            return
        group_id = entry[1]
        current_name = self.group_names.get(group_id, "默认分组")

        win = tk.Toplevel(self.root)
        win.title("重命名分组")
        win.geometry("260x120")
        win.transient(self.root)
        win.grab_set()

        name_var = tk.StringVar(value=current_name)
        ttk.Label(win, text="分组名称:").pack(anchor="w", padx=12, pady=(12, 6))
        ttk.Entry(win, textvariable=name_var, width=24).pack(padx=12, pady=(0, 10))

        def save_name():
            new_name = name_var.get().strip() or current_name
            self.group_names[group_id] = new_name
            for task in TASKS:
                if str(task.get("group_id") or "group_default") == str(group_id):
                    task["group_name"] = new_name
            self.save_current_tasks()
            self.refresh_task_list()
            self.append_log(f"已重命名组: {new_name}")
            win.destroy()

        ttk.Button(win, text="保存", command=save_name).pack()

    def copy_group(self):
        selected = list(self.task_listbox.curselection())
        if not selected:
            return
        display_index = selected[0]
        entry = self.task_display_map.get(display_index)
        if not entry or entry[0] != "group":
            return
        source_group_id = entry[1]
        source_name = self.group_names.get(source_group_id, "组")
        new_group_id = self._ensure_group_exists(group_name=f"{source_name} 复制", parent_id=self.group_parents.get(source_group_id))
        self.group_names[new_group_id] = f"{source_name} 复制"
        copied_tasks = [task for task in TASKS if str(task.get("group_id")) == str(source_group_id)]
        for task in copied_tasks:
            clone = {}
            for key, value in task.items():
                if isinstance(value, list):
                    clone[key] = list(value)
                elif isinstance(value, tuple):
                    clone[key] = tuple(value)
                else:
                    clone[key] = value
            clone["group_id"] = new_group_id
            clone["group_name"] = self.group_names.get(new_group_id, "复制组")
            clone["description"] = f"{task.get('description', task.get('template', 'step'))} (复制)"
            TASKS.append(clone)
        self.save_current_tasks()
        self.refresh_task_list()
        self.append_log(f"已复制组: {source_name}")

    def delete_group(self):
        selected = list(self.task_listbox.curselection())
        if not selected:
            return
        display_index = selected[0]
        entry = self.task_display_map.get(display_index)
        if not entry or entry[0] != "group":
            return
        group_id = entry[1]
        group_name = self.group_names.get(group_id, "组")
        if not messagebox.askyesno("确认删除", f"确定删除组“{group_name}”及其包含的步骤吗？"):
            return
        remove_ids = {group_id}
        for child_group_id in self.group_children.get(group_id, []):
            remove_ids.add(child_group_id)
            remove_ids.update(self._get_group_descendants(child_group_id))

        TASKS[:] = [task for task in TASKS if str(task.get("group_id")) not in {str(item) for item in remove_ids}]
        for remove_group_id in remove_ids:
            self.group_names.pop(remove_group_id, None)
            self.group_expanded.pop(remove_group_id, None)
            self.group_children.pop(remove_group_id, None)
            self.group_parents.pop(remove_group_id, None)
            self.group_order = [gid for gid in self.group_order if gid != remove_group_id]
            for parent_id, children in list(self.group_children.items()):
                self.group_children[parent_id] = [child for child in children if child != remove_group_id]
        self.save_current_tasks()
        self.refresh_task_list()
        self.append_log(f"已删除组: {group_name}")

    def load_task_to_form(self, index):
        if self.selected_group_id is not None:
            self.summary_var.set(f"已选择组: {self.group_names.get(self.selected_group_id, '默认分组')}")
            self.clear_task_form()
            return
        if not (0 <= index < len(TASKS)):
            self.clear_task_form()
            return

        task = TASKS[index]
        self.show_task_editor(task)
        template_names = task.get("templates") or task.get("template", "new_step")
        if isinstance(template_names, (list, tuple)):
            template_names = ", ".join(str(item) for item in template_names)
        self.template_var.set(str(template_names))
        self.description_var.set(task.get("description", task.get("template", "new_step")))
        self.timeout_var.set(str(task.get("timeout", 5)))
        self.offset_x_var.set(str(task.get("offset", (0, 0))[0]))
        self.offset_y_var.set(str(task.get("offset", (0, 0))[1]))
        self.click_x_var.set(str(task.get("click_x", task.get("click_position", (None, None))[0] if isinstance(task.get("click_position"), (list, tuple)) and len(task.get("click_position")) >= 2 else "")))
        self.click_y_var.set(str(task.get("click_y", task.get("click_position", (None, None))[1] if isinstance(task.get("click_position"), (list, tuple)) and len(task.get("click_position")) >= 2 else "")))

        rects = task.get("match_rects")
        rect = (rects[0] if isinstance(rects, list) and rects else None) or task.get("match_rect") or task.get("search_rect")
        if isinstance(rect, (list, tuple)) and len(rect) >= 4:
            left, top, right, bottom = map(int, rect[:4])
            center_x = int((left + right) / 2)
            center_y = int((top + bottom) / 2)
            self.region_left_var.set(str(left))
            self.region_top_var.set(str(top))
            self.region_right_var.set(str(right))
            self.region_bottom_var.set(str(bottom))
            self.region_center_x_var.set(str(center_x))
            self.region_center_y_var.set(str(center_y))
        else:
            self.region_left_var.set("")
            self.region_top_var.set("")
            self.region_right_var.set("")
            self.region_bottom_var.set("")
            self.region_center_x_var.set("")
            self.region_center_y_var.set("")
        self.next_template_var.set(task.get("next_template", ""))
        self.after_wait_var.set(str(task.get("after_wait", 0.25)))
        wait_mode = task.get("wait_for", "time")
        if wait_mode == "next_appear":
            self.wait_for_var.set("2. 等待目标模板出现")
        elif wait_mode == "change_then_appear":
            self.wait_for_var.set("3. 画面变化后目标结果出现")
        else:
            self.wait_for_var.set("1. 画面结果变化")
        self.click_var.set(bool(task.get("click", True)))
        self.match_required_var.set(bool(task.get("click_requires_match", True)))
        self.optional_var.set(bool(task.get("optional", not bool(task.get("required", True)))))

        mode = task.get("mode", "normal")
        if task.get("type") == "keyboard_move":
            steps = task.get("move_steps", [])
            summary = f"类型: {task.get('type')} | 模式: {mode} | 移动步骤: {len(steps)}"
        elif task.get("type") == "key_press":
            summary = f"类型: {task.get('type')} | 模式: {mode} | 按键: {task.get('key', '-')} | 持续: {task.get('hold_time', 0.1)} 秒"
        elif task.get("type") == "advanced":
            templates = task.get("templates") or [task.get("template", "")]
            summary = f"类型: advanced | 模式: {mode} | 可识别图片: {len(templates)} | 描述: {task.get('description', '-') }"
        else:
            summary = f"类型: {task.get('type', 'normal')} | 模式: {mode} | 模板: {task.get('template', '-') } | 描述: {task.get('description', '-') }"
        self.summary_var.set(summary)

    def clear_task_form(self):
        self.template_var.set("")
        self.description_var.set("")
        self.timeout_var.set("5")
        self.offset_x_var.set("0")
        self.offset_y_var.set("0")
        self.click_x_var.set("")
        self.click_y_var.set("")
        self.region_left_var.set("")
        self.region_top_var.set("")
        self.region_right_var.set("")
        self.region_bottom_var.set("")
        self.region_center_x_var.set("")
        self.region_center_y_var.set("")
        self.next_template_var.set("")
        self.after_wait_var.set("0.25")
        self.wait_for_var.set("1. 画面结果变化")
        self.click_var.set(True)
        self.match_required_var.set(True)
        self.optional_var.set(False)
        if self.selected_group_id is not None:
            self.summary_var.set(f"已选择组: {self.group_names.get(self.selected_group_id, '默认分组')}")
        else:
            self.summary_var.set("未选择步骤")

    def _capture_group_metadata(self):
        return {
            "names": deepcopy(self.group_names),
            "colors": deepcopy(self.group_colors),
            "expanded": deepcopy(self.group_expanded),
            "order": deepcopy(self.group_order),
            "parents": deepcopy(self.group_parents),
            "children": deepcopy(self.group_children),
        }

    def _restore_group_metadata(self, mode):
        metadata = self.mode_group_metadata.get(mode, {})
        self.group_names = deepcopy(metadata.get("names", {}))
        self.group_colors = deepcopy(metadata.get("colors", {}))
        self.group_expanded = deepcopy(metadata.get("expanded", {}))
        self.group_order = deepcopy(metadata.get("order", []))
        self.group_parents = deepcopy(metadata.get("parents", {}))
        self.group_children = deepcopy(metadata.get("children", {}))

    def save_current_tasks(self):
        self.mode_tasks[self.current_mode] = deepcopy(TASKS)
        self.mode_group_metadata[self.current_mode] = self._capture_group_metadata()
        if self.current_mode == "custom":
            save_tasks(TASKS)
        self.save_all_presets()

    def apply_selected_task(self):
        if self.selected_group_id is not None:
            return
        if not (0 <= self.selected_task_index < len(TASKS)):
            return

        task = TASKS[self.selected_task_index]
        template_value = self.template_var.get().strip() or "new_step"
        if task.get("type") == "advanced":
            templates = [item.strip() for item in template_value.replace("，", ",").split(",") if item.strip()]
            task["templates"] = templates or ["new_step"]
            task["template"] = task["templates"][0]
        else:
            task["template"] = template_value
        task["description"] = self.description_var.get().strip() or task["template"]
        task["timeout"] = float(self.timeout_var.get() or 5)
        task["offset"] = (
            int(float(self.offset_x_var.get() or 0)),
            int(float(self.offset_y_var.get() or 0)),
        )
        click_x = self.click_x_var.get().strip()
        click_y = self.click_y_var.get().strip()
        if click_x and click_y:
            task["click_x"] = int(float(click_x))
            task["click_y"] = int(float(click_y))
            task["click_position"] = (task["click_x"], task["click_y"])
        else:
            task.pop("click_x", None)
            task.pop("click_y", None)
            task.pop("click_position", None)

        region_left = self.region_left_var.get().strip()
        region_top = self.region_top_var.get().strip()
        region_right = self.region_right_var.get().strip()
        region_bottom = self.region_bottom_var.get().strip()
        if region_left and region_top and region_right and region_bottom:
            task["match_rect"] = (
                int(float(region_left)),
                int(float(region_top)),
                int(float(region_right)),
                int(float(region_bottom)),
            )
            task["search_rect"] = task["match_rect"]
            task["match_rects"] = [task["match_rect"]]
            self.region_center_x_var.set(str(int((int(float(region_left)) + int(float(region_right))) / 2)))
            self.region_center_y_var.set(str(int((int(float(region_top)) + int(float(region_bottom))) / 2)))
        else:
            task.pop("match_rect", None)
            task.pop("search_rect", None)
            task.pop("match_rects", None)
        task["next_template"] = self.next_template_var.get().strip() or None
        task["after_wait"] = max(0.0, float(self.after_wait_var.get() or 0.0))
        wait_mode = self.wait_for_var.get()
        if wait_mode == "2. 等待目标模板出现":
            task["wait_for"] = "next_appear"
        elif wait_mode == "3. 画面变化后目标结果出现":
            task["wait_for"] = "change_then_appear"
        else:
            task["wait_for"] = "time"
        task["click"] = bool(self.click_var.get())
        task["click_requires_match"] = bool(self.match_required_var.get())
        task["optional"] = bool(self.optional_var.get())
        task["required"] = not task["optional"]
        self.save_current_tasks()
        self.refresh_task_list()
        self.task_listbox.selection_set(self.selected_task_index)
        self.load_task_to_form(self.selected_task_index)
        self.append_log(f"已更新步骤: {task['template']}")

    def sync_selected_task_template(self, template_name, description=None):
        if self.selected_group_id is not None:
            return
        if not (0 <= self.selected_task_index < len(TASKS)):
            return
        task = TASKS[self.selected_task_index]
        task["template"] = template_name.strip() or task.get("template", "new_step")
        if description is not None:
            task["description"] = str(description).strip() or task.get("description", task["template"])
        self.template_var.set(task["template"])
        self.description_var.set(task.get("description", task["template"]))
        self.save_current_tasks()
        self.refresh_task_list()
        self.task_listbox.selection_set(self.selected_task_index)
        self.load_task_to_form(self.selected_task_index)

    @staticmethod
    def _is_left_mouse_button_down():
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        return bool(user32.GetAsyncKeyState(0x01) & 0x8000)

    def capture_current_click_position(self):
        if self.selected_group_id is not None:
            return
        if not (0 <= self.selected_task_index < len(TASKS)):
            return
        if self.waiting_for_click_capture:
            self._cancel_click_capture()
            return

        self.waiting_for_click_capture = True
        self.root.withdraw()
        self.append_log("已隐藏脚本界面，点击任意位置记录备用点击坐标...")
        self._schedule_click_capture_poll()

    def _schedule_click_capture_poll(self):
        if not self.waiting_for_click_capture:
            return
        if self._is_left_mouse_button_down():
            x, y = pyautogui.position()
            self.root.after(0, self._finish_click_capture, int(x), int(y))
            return
        self.capture_timer = self.root.after(50, self._schedule_click_capture_poll)

    def _cancel_click_capture(self):
        if self.capture_timer is not None:
            self.root.after_cancel(self.capture_timer)
            self.capture_timer = None
        self.waiting_for_click_capture = False
        self.root.deiconify()
        self.root.focus_force()
        self.append_log("已取消记录点击点")

    def _finish_click_capture(self, x, y):
        if self.capture_timer is not None:
            self.root.after_cancel(self.capture_timer)
            self.capture_timer = None

        if not (0 <= self.selected_task_index < len(TASKS)):
            self.waiting_for_click_capture = False
            self.root.deiconify()
            self.root.focus_force()
            return

        self.click_x_var.set(str(x))
        self.click_y_var.set(str(y))
        task = TASKS[self.selected_task_index]
        task["click_x"] = int(x)
        task["click_y"] = int(y)
        task["click_position"] = (int(x), int(y))
        if task.get("type") != "click_until_gone":
            task.pop("match_rect", None)
            task.pop("search_rect", None)
        self.save_current_tasks()
        self.waiting_for_click_capture = False
        self.root.deiconify()
        self.root.focus_force()
        self.append_log(f"已记录备用点击坐标: ({x}, {y})")

    def _create_selection_overlay(self):
        if self.selection_overlay is not None and self.selection_overlay.winfo_exists():
            return

        screen_w, screen_h = pyautogui.size()
        overlay = tk.Toplevel(self.root)
        overlay.withdraw()
        overlay.attributes("-topmost", True)
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-alpha", 0.18)
        overlay.configure(bg="#000000")
        overlay.overrideredirect(True)

        canvas = tk.Canvas(overlay, bg="#000000", highlightthickness=0, cursor="cross")
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Escape>", lambda event: self._cancel_region_capture())

        self.selection_overlay = overlay
        self.selection_canvas = canvas
        self.selection_box_id = None
        self.selection_overlay.geometry(f"{screen_w}x{screen_h}+0+0")

    def _update_selection_preview(self, start_pos, end_pos):
        if self.selection_canvas is None:
            return
        x1 = min(start_pos[0], end_pos[0])
        y1 = min(start_pos[1], end_pos[1])
        x2 = max(start_pos[0], end_pos[0])
        y2 = max(start_pos[1], end_pos[1])
        if self.selection_box_id is None:
            self.selection_box_id = self.selection_canvas.create_rectangle(
                x1, y1, x2, y2,
                outline="#4ad3ff",
                width=3,
                fill="#4ad3ff",
                stipple="gray50",
                tag="selection_box",
            )
        else:
            self.selection_canvas.coords(self.selection_box_id, x1, y1, x2, y2)
        self.selection_canvas.tag_raise(self.selection_box_id)

    def _clear_selection_preview(self):
        if self.selection_canvas is not None and self.selection_box_id is not None:
            self.selection_canvas.delete(self.selection_box_id)
            self.selection_box_id = None
        if self.selection_overlay is not None and self.selection_overlay.winfo_exists():
            self.selection_overlay.withdraw()
            self.selection_overlay.destroy()
        self.selection_overlay = None
        self.selection_canvas = None
        self.selection_box_id = None

    def capture_current_match_region(self):
        self._start_region_capture("match")

    def clear_current_match_regions(self):
        if self.selected_group_id is not None:
            return
        if not (0 <= self.selected_task_index < len(TASKS)):
            return
        task = TASKS[self.selected_task_index]
        for key in ("match_rects", "match_rect", "search_rect"):
            task.pop(key, None)
        for variable in (
            self.region_left_var,
            self.region_top_var,
            self.region_right_var,
            self.region_bottom_var,
            self.region_center_x_var,
            self.region_center_y_var,
        ):
            variable.set("")
        self.save_current_tasks()
        self.refresh_task_list()
        self.load_task_to_form(self.selected_task_index)
        self.append_log("已清空当前步骤的全部识别区域")

    def capture_next_template_region(self):
        if not self.next_template_var.get().strip():
            self.append_log("请先选择下一模板图片，再框选它的出现位置。")
            return
        self._start_region_capture("next")

    def _start_region_capture(self, target):
        if self.selected_group_id is not None:
            return
        if not (0 <= self.selected_task_index < len(TASKS)):
            return
        if self.waiting_for_region_capture:
            self._cancel_region_capture()
            return

        self.waiting_for_region_capture = True
        self.region_capture_target = target
        self.region_capture_start = None
        self.region_capture_end = None
        if config.USE_WINDOW_MODE and config.TARGET_WINDOW_TITLE:
            self.region_capture_rect = get_window_rect() or {"left": 0, "top": 0, "width": 1920, "height": 1080}
        else:
            self.region_capture_rect = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        self._create_selection_overlay()
        self.selection_overlay.deiconify()
        self.selection_overlay.focus_force()
        self.root.withdraw()
        self.append_log("已隐藏脚本界面，按住鼠标左键并拖动，框选识别区域...")
        self.region_capture_timer = self.root.after(50, self._schedule_region_capture_poll)

    def _schedule_region_capture_poll(self):
        if not self.waiting_for_region_capture:
            return

        current_pos = pyautogui.position()
        if self._is_left_mouse_button_down():
            if self.region_capture_start is None:
                self.region_capture_start = current_pos
                self.region_capture_end = current_pos
                self._update_selection_preview(self.region_capture_start, self.region_capture_end)
            else:
                self.region_capture_end = current_pos
                self._update_selection_preview(self.region_capture_start, self.region_capture_end)
        elif self.region_capture_start is not None:
            start_x, start_y = self.region_capture_start
            end_x, end_y = current_pos
            self.root.after(0, self._finish_region_capture, int(start_x), int(start_y), int(end_x), int(end_y))
            return

        self.region_capture_timer = self.root.after(50, self._schedule_region_capture_poll)

    def _cancel_region_capture(self):
        if self.region_capture_timer is not None:
            self.root.after_cancel(self.region_capture_timer)
            self.region_capture_timer = None
        self.waiting_for_region_capture = False
        self.region_capture_start = None
        self.region_capture_end = None
        self.region_capture_rect = None
        self._clear_selection_preview()
        self.root.deiconify()
        self.root.focus_force()
        self.append_log("已取消框选识别区域")

    def _finish_region_capture(self, start_x, start_y, end_x, end_y):
        if self.region_capture_timer is not None:
            self.root.after_cancel(self.region_capture_timer)
            self.region_capture_timer = None

        self.waiting_for_region_capture = False
        self.region_capture_start = None
        self.region_capture_end = None

        if not (0 <= self.selected_task_index < len(TASKS)):
            self.region_capture_rect = None
            self._clear_selection_preview()
            self.root.deiconify()
            self.root.focus_force()
            return

        left = min(start_x, end_x)
        top = min(start_y, end_y)
        right = max(start_x, end_x)
        bottom = max(start_y, end_y)

        if self.region_capture_target == "image":
            self.region_capture_rect = None
            self._clear_selection_preview()
            self._save_captured_image(int(left), int(top), int(right), int(bottom))
            self.root.deiconify()
            self.root.focus_force()
            return

        rect = self.region_capture_rect or {"left": 0, "top": 0, "width": 1920, "height": 1080}
        local_left = max(0, int(left - rect["left"]))
        local_top = max(0, int(top - rect["top"]))
        local_right = max(0, int(right - rect["left"]))
        local_bottom = max(0, int(bottom - rect["top"]))

        task = TASKS[self.selected_task_index]
        selected_rect = (local_left, local_top, local_right, local_bottom)
        if self.region_capture_target == "next":
            task["next_match_rect"] = selected_rect
            task["next_search_rect"] = selected_rect
        else:
            match_rects = task.setdefault("match_rects", [])
            if not isinstance(match_rects, list):
                match_rects = []
            match_rects.append(selected_rect)
            task["match_rect"] = match_rects[0]
            task["search_rect"] = match_rects[0]
        center_x = int((left + right) / 2)
        center_y = int((top + bottom) / 2)
        if self.region_capture_target == "next":
            self.append_log(f"已记录下一模板识别区域: 左上=({local_left}, {local_top}) 右下=({local_right}, {local_bottom})")
        else:
            self.region_left_var.set(str(local_left))
            self.region_top_var.set(str(local_top))
            self.region_right_var.set(str(local_right))
            self.region_bottom_var.set(str(local_bottom))
            self.region_center_x_var.set(str(int((local_left + local_right) / 2)))
            self.region_center_y_var.set(str(int((local_top + local_bottom) / 2)))
            self.append_log(f"已追加第 {len(task.get('match_rects', []))} 段识别区域")
        self.save_current_tasks()
        self.region_capture_rect = None
        self._clear_selection_preview()
        self.root.deiconify()
        self.root.focus_force()

    def select_task_image(self):
        if self.selected_group_id is not None:
            return
        if not (0 <= self.selected_task_index < len(TASKS)):
            return

        task = TASKS[self.selected_task_index]
        self.open_bind_image_menu(task)

    def open_bind_image_menu(self, task):
        win = tk.Toplevel(self.root)
        win.title("绑定图片")
        win.geometry("390x360")
        win.transient(self.root)
        win.grab_set()

        ttk.Label(win, text="绑定图片操作", font=("Microsoft YaHei", 10, "bold")).pack(pady=(14, 8))
        image_list = None
        supports_multiple_templates = task.get("type") in ("advanced", "click_until_gone")
        if supports_multiple_templates:
            image_list = tk.Listbox(win, height=8, selectmode=tk.SINGLE, exportselection=False)
            image_list.pack(fill="both", expand=True, padx=12, pady=(0, 10))
            current = task.get("templates") or task.get("template") or []
            if isinstance(current, str):
                current = [current]
            for image_name in current:
                if str(image_name).strip():
                    image_list.insert(tk.END, str(image_name).strip())
        else:
            ttk.Label(win, text=f"当前图片: {task.get('template', '未绑定')}").pack(pady=(4, 12))

        def close_menu():
            if win.winfo_exists():
                win.destroy()

        def choose_image():
            file_paths = filedialog.askopenfilenames(
                title="选择要绑定的图片",
                initialdir=os.path.join(os.path.dirname(__file__), "icons"),
                filetypes=[("PNG Images", "*.png"), ("All Files", "*.*")],
            )
            if not file_paths:
                return
            if supports_multiple_templates:
                existing = [str(image_list.get(index)) for index in range(image_list.size())]
                for file_path in file_paths:
                    image_name = os.path.splitext(os.path.basename(file_path))[0]
                    if image_name not in existing:
                        image_list.insert(tk.END, image_name)
                        existing.append(image_name)
                if task.get("type") == "advanced":
                    save_advanced_images()
                else:
                    save_multiple_click_until_gone_images()
            else:
                image_name = os.path.splitext(os.path.basename(file_paths[0]))[0]
                self.sync_selected_task_template(image_name, None)
                self.append_log(f"已绑定图片: {image_name} -> {file_paths[0]}")
                close_menu()

        def manual_capture():
            close_menu()
            self._start_region_capture("image")

        def preview_images():
            image_names = task.get("templates") or task.get("template") or []
            if isinstance(image_names, str):
                image_names = [image_names]
            icons_dir = os.path.join(os.path.dirname(__file__), "icons")
            opened = 0
            for image_name in image_names:
                matches = glob.glob(os.path.join(icons_dir, f"{str(image_name).strip()}.*"))
                if matches:
                    os.startfile(matches[0])
                    opened += 1
            if opened == 0:
                messagebox.showinfo("预览绑定图片", "当前步骤没有找到可预览的绑定图片。", parent=win)

        def save_advanced_images():
            values = [image_list.get(index) for index in range(image_list.size())]
            if not values:
                messagebox.showwarning("无法保存", "高级步骤至少需要绑定一张图片。", parent=win)
                return
            task["templates"] = values
            task["template"] = values[0]
            self.save_current_tasks()
            self.refresh_task_list()
            self.load_task_to_form(self.selected_task_index)
            self.append_log(f"已保存高级步骤图片: {', '.join(values)}")

        def save_multiple_click_until_gone_images():
            values = [image_list.get(index) for index in range(image_list.size())]
            if not values:
                messagebox.showwarning("无法保存", "持续点击步骤至少需要绑定一张图片。", parent=win)
                return
            task["templates"] = values
            task["template"] = values[0]
            self.save_current_tasks()
            self.refresh_task_list()
            self.load_task_to_form(self.selected_task_index)
            self.append_log(f"已保存持续点击图片: {', '.join(values)}")

        ttk.Button(win, text="1. 选择图片", command=choose_image).pack(fill="x", padx=36, pady=4)
        ttk.Button(win, text="2. 手动框选图片", command=manual_capture).pack(fill="x", padx=36, pady=4)
        ttk.Button(win, text="3. 预览绑定图片", command=preview_images).pack(fill="x", padx=36, pady=4)
        if image_list is not None:
            ttk.Button(win, text="删除选中图片", command=lambda: image_list.delete(image_list.curselection()[0]) if image_list.curselection() else None).pack(pady=(8, 4))
            def save_and_close():
                values = [image_list.get(index) for index in range(image_list.size())]
                if not values:
                    messagebox.showwarning(
                        "无法保存",
                        "至少需要绑定一张图片。",
                        parent=win,
                    )
                    return
                if task.get("type") == "advanced":
                    save_advanced_images()
                else:
                    save_multiple_click_until_gone_images()
                close_menu()

            button_text = "保存图片列表并关闭" if task.get("type") == "advanced" else "保存持续点击图片并关闭"
            ttk.Button(win, text=button_text, command=save_and_close).pack(pady=(0, 12))
        else:
            ttk.Button(win, text="关闭", command=close_menu).pack(pady=(10, 12))

    def _save_captured_image(self, left, top, right, bottom):
        if right <= left or bottom <= top:
            messagebox.showwarning("框选失败", "框选区域太小，请重新拖曳选择。", parent=self.root)
            return
        icons_dir = os.path.join(os.path.dirname(__file__), "icons")
        os.makedirs(icons_dir, exist_ok=True)
        image_name = f"captured_{uuid.uuid4().hex[:10]}"
        image_path = os.path.join(icons_dir, f"{image_name}.png")
        screenshot = pyautogui.screenshot()
        screenshot.crop((left, top, right, bottom)).save(image_path)
        reload_templates()
        task = TASKS[self.selected_task_index]
        if task.get("type") in ("advanced", "click_until_gone"):
            templates = task.get("templates") or task.get("template") or []
            if isinstance(templates, str):
                templates = [templates]
            templates = [str(item).strip() for item in templates if str(item).strip()]
            if image_name not in templates:
                templates.append(image_name)
            task["templates"] = templates
            task["template"] = templates[0]
        else:
            task["template"] = image_name
        self.save_current_tasks()
        self.refresh_task_list()
        self.load_task_to_form(self.selected_task_index)
        self.append_log(f"已将框选图片保存并绑定: {image_path}")

    def open_advanced_image_menu(self, task):
        self.open_bind_image_menu(task)

    def _legacy_select_task_image(self):
        default_dir = os.path.join(os.getcwd(), "icons")
        file_path = filedialog.askopenfilename(
            title="选择要绑定的图片",
            initialdir=default_dir,
            filetypes=[("PNG Images", "*.png"), ("All Files", "*.*")],
        )
        if not file_path:
            return

        file_name = os.path.splitext(os.path.basename(file_path))[0]
        self.sync_selected_task_template(file_name, None)
        self.append_log(f"已绑定图片: {file_name} -> {file_path}")

    def open_task_detail_settings(self):
        return

    def open_normal_task_settings(self, task):
        win = tk.Toplevel(self.root)
        win.title("普通步骤设置")
        win.geometry("420x300")
        win.transient(self.root)
        win.grab_set()

        form = ttk.Frame(win, padding=12)
        form.pack(fill="both", expand=True)

        fields = {
            "模板名": tk.StringVar(value=task.get("template", "")),
            "描述": tk.StringVar(value=task.get("description", "")),
            "超时秒": tk.StringVar(value=str(task.get("timeout", 5))),
            "X偏移": tk.StringVar(value=str(task.get("offset", (0, 0))[0])),
            "Y偏移": tk.StringVar(value=str(task.get("offset", (0, 0))[1])),
            "点击X": tk.StringVar(value=str(task.get("click_x", ""))),
            "点击Y": tk.StringVar(value=str(task.get("click_y", ""))),
            "等待方式": tk.StringVar(value="3. 画面变化后目标结果出现" if task.get("wait_for", "time") == "change_then_appear" else "2. 等待目标模板出现" if task.get("wait_for", "time") == "next_appear" else "1. 画面结果变化"),
            "完成后等待(秒)": tk.StringVar(value=str(task.get("after_wait", 0.25))),
        }

        rows = []
        for label_text, var in fields.items():
            row = ttk.Frame(form)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8))
            if label_text == "等待方式":
                ttk.Combobox(row, textvariable=var, values=["1. 画面结果变化", "2. 等待目标模板出现", "3. 画面变化后目标结果出现"], state="readonly", width=24).pack(side="left")
            else:
                ttk.Entry(row, textvariable=var, width=20).pack(side="left")
            rows.append((label_text, var))

        click_var = tk.BooleanVar(value=bool(task.get("click", True)))
        match_required_var = tk.BooleanVar(value=bool(task.get("click_requires_match", True)))
        optional_var = tk.BooleanVar(value=bool(task.get("optional", not bool(task.get("required", True)))))
        ttk.Checkbutton(form, text="点击", variable=click_var).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(form, text="必须识别到图片再点击", variable=match_required_var).pack(anchor="w", pady=(0, 4))
        ttk.Checkbutton(form, text="可选步骤（跳过）", variable=optional_var).pack(anchor="w")

        def save():
            task["template"] = fields["模板名"].get().strip() or task.get("template", "new_step")
            task["description"] = fields["描述"].get().strip() or task["template"]
            task["timeout"] = float(fields["超时秒"].get() or 5)
            task["offset"] = (
                int(float(fields["X偏移"].get() or 0)),
                int(float(fields["Y偏移"].get() or 0)),
            )
            click_x = fields["点击X"].get().strip()
            click_y = fields["点击Y"].get().strip()
            if click_x and click_y:
                task["click_x"] = int(float(click_x))
                task["click_y"] = int(float(click_y))
                task["click_position"] = (task["click_x"], task["click_y"])
            else:
                task.pop("click_x", None)
                task.pop("click_y", None)
                task.pop("click_position", None)
            wait_mode = fields["等待方式"].get()
            if wait_mode == "2. 等待目标模板出现":
                task["wait_for"] = "next_appear"
            elif wait_mode == "3. 画面变化后目标结果出现":
                task["wait_for"] = "change_then_appear"
            else:
                task["wait_for"] = "time"
            task["after_wait"] = max(0.0, float(fields["完成后等待(秒)"].get() or 0.0))
            task["click"] = bool(click_var.get())
            task["click_requires_match"] = bool(match_required_var.get())
            task["optional"] = bool(optional_var.get())
            task["required"] = not task["optional"]
            self.refresh_task_list()
            win.destroy()

        ttk.Button(form, text="保存", command=save).pack(pady=(12, 0))

    def select_next_template_image(self):
        if self.selected_group_id is not None:
            return
        if not (0 <= self.selected_task_index < len(TASKS)):
            return

        default_dir = os.path.join(os.getcwd(), "icons")
        file_path = filedialog.askopenfilename(
            title="选择等待出现的目标图片",
            initialdir=default_dir,
            filetypes=[("PNG Images", "*.png"), ("All Files", "*.*")],
        )
        if not file_path:
            return

        template_name = os.path.splitext(os.path.basename(file_path))[0]
        self.next_template_var.set(template_name)
        task = TASKS[self.selected_task_index]
        task["next_template"] = template_name
        task["next_templates"] = [template_name]
        task["wait_for"] = "next_appear"
        self.wait_for_var.set("2. 等待目标模板出现")
        self.save_current_tasks()
        self.refresh_task_list()
        self.append_log(f"已绑定等待目标图片: {template_name} -> {file_path}")

    def _bind_detour_template_image(self, detour_task, template_var):
        default_dir = os.path.join(os.getcwd(), "icons")
        file_path = filedialog.askopenfilename(
            title="选择要绑定的图片",
            initialdir=default_dir,
            filetypes=[("PNG Images", "*.png"), ("All Files", "*.*")],
        )
        if not file_path:
            return

        template_name = os.path.splitext(os.path.basename(file_path))[0]
        template_var.set(template_name)
        detour_task["template"] = template_name
        self.append_log(f"已绑定迂回图片: {template_name} -> {file_path}")

    def _bind_detour_next_template_image(self, detour_task, template_var):
        default_dir = os.path.join(os.getcwd(), "icons")
        file_path = filedialog.askopenfilename(
            title="选择迂回步骤下一模板图片",
            initialdir=default_dir,
            filetypes=[("PNG Images", "*.png"), ("All Files", "*.*")],
        )
        if not file_path:
            return

        template_name = os.path.splitext(os.path.basename(file_path))[0]
        template_var.set(template_name)
        detour_task["next_template"] = template_name
        detour_task["next_templates"] = [template_name]
        self.append_log(f"已绑定迂回下一模板: {template_name} -> {file_path}")

    def _capture_detour_click_position(self, detour_task, click_x_var, click_y_var, editor=None):
        if editor is not None:
            editor.grab_release()
            editor.withdraw()
        self.root.withdraw()
        self.append_log("已隐藏脚本和迂回设置，点击任意位置记录迂回步骤点击坐标...")

        def poll_click():
            if self._is_left_mouse_button_down():
                x, y = pyautogui.position()
                click_x_var.set(str(x))
                click_y_var.set(str(y))
                detour_task["click_x"] = int(x)
                detour_task["click_y"] = int(y)
                detour_task["click_position"] = (int(x), int(y))
                self.save_current_tasks()
                self.root.deiconify()
                if editor is not None:
                    editor.deiconify()
                    editor.grab_set()
                    editor.focus_force()
                else:
                    self.root.focus_force()
                self.append_log(f"已记录迂回点击坐标: ({x}, {y})")
                return
            self.root.after(50, poll_click)

        self.root.after(100, poll_click)

    def _capture_detour_match_region(self, detour_task, region_left_var, region_top_var, region_right_var, region_bottom_var, region_center_x_var, region_center_y_var, click_x_var, click_y_var, editor=None):
        if config.USE_WINDOW_MODE and config.TARGET_WINDOW_TITLE:
            capture_rect = get_window_rect() or {"left": 0, "top": 0, "width": 1920, "height": 1080}
        else:
            capture_rect = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        if editor is not None:
            editor.grab_release()
            editor.withdraw()
        self.root.withdraw()
        self.append_log("已隐藏脚本界面，按住鼠标左键并拖动，框选迂回步骤识别区域...")

        screen_w, screen_h = pyautogui.size()
        overlay = tk.Toplevel(self.root)
        overlay.attributes("-topmost", True)
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-alpha", 0.18)
        overlay.configure(bg="#000000")
        overlay.overrideredirect(True)
        canvas = tk.Canvas(overlay, bg="#000000", highlightthickness=0, cursor="cross")
        canvas.pack(fill="both", expand=True)

        start_pos = None
        box_id = None

        def update_preview(end_pos):
            nonlocal box_id
            if start_pos is None:
                return
            x1 = min(start_pos[0], end_pos[0])
            y1 = min(start_pos[1], end_pos[1])
            x2 = max(start_pos[0], end_pos[0])
            y2 = max(start_pos[1], end_pos[1])
            if box_id is None:
                box_id = canvas.create_rectangle(x1, y1, x2, y2, outline="#4ad3ff", width=3, fill="#4ad3ff", stipple="gray50")
            else:
                canvas.coords(box_id, x1, y1, x2, y2)

        def finish_capture(event=None):
            nonlocal start_pos
            x, y = pyautogui.position()
            overlay.destroy()
            self.root.deiconify()
            if editor is not None:
                editor.deiconify()
                editor.grab_set()
                editor.focus_force()
            else:
                self.root.focus_force()

            rect = capture_rect
            left = min(start_pos[0], x)
            top = min(start_pos[1], y)
            right = max(start_pos[0], x)
            bottom = max(start_pos[1], y)

            local_left = max(0, int(left - rect["left"]))
            local_top = max(0, int(top - rect["top"]))
            local_right = max(0, int(right - rect["left"]))
            local_bottom = max(0, int(bottom - rect["top"]))
            center_x = int((left + right) / 2)
            center_y = int((top + bottom) / 2)

            selected_rect = (local_left, local_top, local_right, local_bottom)
            match_rects = detour_task.setdefault("match_rects", [])
            if not isinstance(match_rects, list):
                match_rects = []
            match_rects.append(selected_rect)
            detour_task["match_rect"] = match_rects[0]
            detour_task["search_rect"] = match_rects[0]

            region_left_var.set(str(local_left))
            region_top_var.set(str(local_top))
            region_right_var.set(str(local_right))
            region_bottom_var.set(str(local_bottom))
            region_center_x_var.set(str(int((local_left + local_right) / 2)))
            region_center_y_var.set(str(int((local_top + local_bottom) / 2)))
            self.save_current_tasks()
            self.append_log(f"已记录迂回识别区域: 左上=({local_left}, {local_top}) 右下=({local_right}, {local_bottom})，中心=({center_x}, {center_y})")

        def on_drag(event):
            if start_pos is None:
                return
            update_preview((event.x_root, event.y_root))

        def on_press(event):
            nonlocal start_pos
            start_pos = (event.x_root, event.y_root)
            update_preview(start_pos)

        overlay.bind("<ButtonPress-1>", on_press)
        overlay.bind("<B1-Motion>", on_drag)
        overlay.bind("<ButtonRelease-1>", finish_capture)
        def cancel_capture(event=None):
            overlay.destroy()
            self.root.deiconify()
            if editor is not None:
                editor.deiconify()
                editor.grab_set()
                editor.focus_force()
            else:
                self.root.focus_force()
            self.append_log("已取消迂回框选识别区域")

        overlay.bind("<Escape>", cancel_capture)
        overlay.geometry(f"{screen_w}x{screen_h}+0+0")
        overlay.deiconify()
        overlay.focus_force()

    def _clear_detour_match_region(self, detour_task, region_left_var, region_top_var, region_right_var, region_bottom_var, region_center_x_var, region_center_y_var):
        for key in ("match_rects", "match_rect", "search_rect"):
            detour_task.pop(key, None)
        for variable in (
            region_left_var,
            region_top_var,
            region_right_var,
            region_bottom_var,
            region_center_x_var,
            region_center_y_var,
        ):
            variable.set("")
        self.append_log("已清空迂回步骤的全部识别区域")

    def open_detour_step_config_editor(self, detour_task):
        win = tk.Toplevel(self.root)
        win.title("迂回步骤设置")
        win.geometry("420x320")
        win.transient(self.root)
        win.grab_set()

        task_type = detour_task.get("type", "normal")

        name_var = tk.StringVar(value=str(detour_task.get("description", detour_task.get("template", "新步骤"))))

        if task_type in ("normal", "advanced"):
            template_var = tk.StringVar(value=str(detour_task.get("template", "new_step")))
            next_template_var = tk.StringVar(value=str(detour_task.get("next_template") or ""))
            timeout_var = tk.StringVar(value=str(detour_task.get("timeout", 5)))
            offset_x_var = tk.StringVar(value=str((detour_task.get("offset") or (0, 0))[0]))
            offset_y_var = tk.StringVar(value=str((detour_task.get("offset") or (0, 0))[1]))
            click_x_var = tk.StringVar(value=str(detour_task.get("click_x", "")))
            click_y_var = tk.StringVar(value=str(detour_task.get("click_y", "")))
            region_left_var = tk.StringVar(value=str((detour_task.get("search_rect") or (0, 0, 0, 0))[0]))
            region_top_var = tk.StringVar(value=str((detour_task.get("search_rect") or (0, 0, 0, 0))[1]))
            region_right_var = tk.StringVar(value=str((detour_task.get("search_rect") or (0, 0, 0, 0))[2]))
            region_bottom_var = tk.StringVar(value=str((detour_task.get("search_rect") or (0, 0, 0, 0))[3]))
            region_center_x_var = tk.StringVar(value=str(detour_task.get("click_x", "")))
            region_center_y_var = tk.StringVar(value=str(detour_task.get("click_y", "")))
            wait_mode_var = tk.StringVar(value="3. 画面变化后目标结果出现" if detour_task.get("wait_for", "time") == "change_then_appear" else "2. 等待目标模板出现" if detour_task.get("wait_for", "time") == "next_appear" else "1. 画面结果变化")
            click_var = tk.BooleanVar(value=bool(detour_task.get("click", True)))
            match_required_var = tk.BooleanVar(value=bool(detour_task.get("click_requires_match", True)))
            optional_var = tk.BooleanVar(value=bool(detour_task.get("optional", not bool(detour_task.get("required", True)))))

            form = ttk.Frame(win, padding=12)
            form.pack(fill="both", expand=True)

            fields = {
                "模板名": template_var,
                "下一模板": next_template_var,
                "描述": name_var,
                "超时秒": timeout_var,
                "X偏移": offset_x_var,
                "Y偏移": offset_y_var,
                "点击X": click_x_var,
                "点击Y": click_y_var,
                "等待方式": wait_mode_var,
            }

            for label_text, var in fields.items():
                row = ttk.Frame(form)
                row.pack(fill="x", pady=4)
                ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8))
                if label_text == "等待方式":
                    ttk.Combobox(row, textvariable=var, values=["1. 画面结果变化", "2. 等待目标模板出现", "3. 画面变化后目标结果出现"], state="readonly", width=24).pack(side="left")
                else:
                    ttk.Entry(row, textvariable=var, width=20).pack(side="left")

            action_row = ttk.Frame(form)
            action_row.pack(fill="x", pady=(8, 4))
            ttk.Button(action_row, text="绑定图片", command=lambda: self._bind_detour_template_image(detour_task, template_var)).pack(side="left", padx=(0, 8))
            ttk.Button(action_row, text="绑定下一模板", command=lambda: self._bind_detour_next_template_image(detour_task, next_template_var)).pack(side="left", padx=(0, 8))
            ttk.Button(action_row, text="记录点击点", command=lambda: self._capture_detour_click_position(detour_task, click_x_var, click_y_var, win)).pack(side="left", padx=(0, 8))
            ttk.Button(action_row, text="框选识别区域", command=lambda: self._capture_detour_match_region(detour_task, region_left_var, region_top_var, region_right_var, region_bottom_var, region_center_x_var, region_center_y_var, click_x_var, click_y_var, win)).pack(side="left")
            ttk.Button(action_row, text="清空识别区域", command=lambda: self._clear_detour_match_region(detour_task, region_left_var, region_top_var, region_right_var, region_bottom_var, region_center_x_var, region_center_y_var)).pack(side="left", padx=(8, 0))

            region_row = ttk.Frame(form)
            region_row.pack(fill="x", pady=(4, 0))
            for label_text, var in [("左上", region_left_var), ("上", region_top_var), ("右下", region_right_var), ("下", region_bottom_var), ("中心X", region_center_x_var), ("中心Y", region_center_y_var)]:
                row = ttk.Frame(region_row)
                row.pack(side="left", padx=(0, 8), pady=2)
                ttk.Label(row, text=f"{label_text}:").pack(side="left")
                ttk.Entry(row, textvariable=var, width=8).pack(side="left")

            row = ttk.Frame(form)
            row.pack(fill="x", pady=(8, 0))
            ttk.Checkbutton(row, text="点击", variable=click_var).pack(side="left")
            ttk.Checkbutton(row, text="必须识别到图片再点击", variable=match_required_var).pack(side="left", padx=(18, 0))
            ttk.Checkbutton(row, text="可选步骤（跳过）", variable=optional_var).pack(side="left", padx=(18, 0))

            def save():
                detour_task["type"] = "normal"
                detour_task["template"] = template_var.get().strip() or "new_step"
                detour_task["description"] = name_var.get().strip() or detour_task["template"]
                next_template = next_template_var.get().strip()
                if next_template:
                    detour_task["next_template"] = next_template
                    detour_task["next_templates"] = [next_template]
                else:
                    detour_task.pop("next_template", None)
                    detour_task.pop("next_templates", None)
                detour_task["timeout"] = float(timeout_var.get() or 5)
                detour_task["offset"] = (
                    int(float(offset_x_var.get() or 0)),
                    int(float(offset_y_var.get() or 0)),
                )
                click_x = click_x_var.get().strip()
                click_y = click_y_var.get().strip()
                if click_x and click_y:
                    detour_task["click_x"] = int(float(click_x))
                    detour_task["click_y"] = int(float(click_y))
                    detour_task["click_position"] = (detour_task["click_x"], detour_task["click_y"])
                else:
                    detour_task.pop("click_x", None)
                    detour_task.pop("click_y", None)
                    detour_task.pop("click_position", None)

                left = int(float(region_left_var.get() or 0))
                top = int(float(region_top_var.get() or 0))
                right = int(float(region_right_var.get() or 0))
                bottom = int(float(region_bottom_var.get() or 0))
                if left or top or right or bottom:
                    detour_task["search_rect"] = (left, top, right, bottom)
                    detour_task["match_rect"] = (left, top, right, bottom)
                    detour_task["match_rects"] = [detour_task["match_rect"]]
                else:
                    detour_task.pop("search_rect", None)
                    detour_task.pop("match_rect", None)
                    detour_task.pop("match_rects", None)

                wait_mode = wait_mode_var.get()
                if wait_mode == "2. 等待目标模板出现":
                    detour_task["wait_for"] = "next_appear"
                elif wait_mode == "3. 画面变化后目标结果出现":
                    detour_task["wait_for"] = "change_then_appear"
                else:
                    detour_task["wait_for"] = "time"

                detour_task["click"] = bool(click_var.get())
                detour_task["click_requires_match"] = bool(match_required_var.get())
                detour_task["optional"] = bool(optional_var.get())
                detour_task["required"] = not detour_task["optional"]
                win.destroy()

            ttk.Button(win, text="保存", command=save).pack(pady=(0, 12))
            return

        if task_type == "key_press":
            key_var = tk.StringVar(value=str(detour_task.get("key") or detour_task.get("template") or "E"))
            delay_var = tk.StringVar(value=str(detour_task.get("delay_before", 0.0)))
            hold_var = tk.StringVar(value=str(detour_task.get("hold_time", 0.1)))
            after_wait_var = tk.StringVar(value=str(detour_task.get("after_wait", 0.2)))
            ttk.Label(win, text="名称:").pack(anchor="w", padx=12, pady=(12, 0))
            ttk.Entry(win, textvariable=name_var).pack(fill="x", padx=12)
            ttk.Label(win, text="按键:").pack(anchor="w", padx=12, pady=(8, 0))
            ttk.Entry(win, textvariable=key_var).pack(fill="x", padx=12)
            ttk.Label(win, text="执行前延时(秒):").pack(anchor="w", padx=12, pady=(8, 0))
            ttk.Entry(win, textvariable=delay_var).pack(fill="x", padx=12)
            ttk.Label(win, text="按住时长(秒):").pack(anchor="w", padx=12, pady=(8, 0))
            ttk.Entry(win, textvariable=hold_var).pack(fill="x", padx=12)
            ttk.Label(win, text="执行后等待(秒):").pack(anchor="w", padx=12, pady=(8, 0))
            ttk.Entry(win, textvariable=after_wait_var).pack(fill="x", padx=12)

            def save():
                detour_task["type"] = "key_press"
                detour_task["description"] = name_var.get().strip() or "按键步骤"
                detour_task["key"] = (key_var.get().strip() or "E").upper()
                detour_task["template"] = detour_task["key"]
                detour_task["delay_before"] = max(0.0, float(delay_var.get() or 0.0))
                detour_task["hold_time"] = float(hold_var.get() or 0.1)
                detour_task["after_wait"] = max(0.0, float(after_wait_var.get() or 0.0))
                detour_task["click"] = False
                detour_task["required"] = bool(detour_task.get("required", False))
                win.destroy()

            ttk.Button(win, text="保存", command=save).pack(pady=(18, 0))
            return

        if task_type == "keyboard_move":
            key_var = tk.StringVar(value="W")
            duration_var = tk.StringVar(value="1.0")
            delay_before_var = tk.StringVar(value=str(detour_task.get("delay_before", 0.0)))
            after_wait_var = tk.StringVar(value=str(detour_task.get("after_wait", 0.0)))
            step_list = tk.Listbox(win, height=8, exportselection=False)
            step_list.pack(fill="both", expand=True, padx=12, pady=(12, 8))
            for step in detour_task.get("move_steps", []):
                step_list.insert(tk.END, f"{step.get('key', 'W')} {step.get('duration', 1.0)}s")

            drag_index = {"value": None}

            def load_selected_step(_event=None):
                selection = step_list.curselection()
                if selection:
                    parts = step_list.get(selection[0]).split()
                    key_var.set(parts[0])
                    duration_var.set(parts[1].rstrip("sS") if len(parts) > 1 else "1.0")

            def update_selected_step():
                selection = step_list.curselection()
                if not selection:
                    return
                try:
                    duration = float(duration_var.get() or 1.0)
                except ValueError:
                    duration = 1.0
                step_list.delete(selection[0])
                step_list.insert(selection[0], f"{key_var.get()} {duration}s")
                step_list.selection_set(selection[0])

            def start_drag(event):
                drag_index["value"] = step_list.nearest(event.y)

            def drag_step(event):
                source = drag_index["value"]
                target = step_list.nearest(event.y)
                if source is None or target == source or target < 0 or target >= step_list.size():
                    return
                value = step_list.get(source)
                step_list.delete(source)
                step_list.insert(target, value)
                step_list.selection_set(target)
                drag_index["value"] = target

            def finish_drag(_event):
                drag_index["value"] = None

            step_list.bind("<<ListboxSelect>>", load_selected_step)
            step_list.bind("<ButtonPress-1>", start_drag)
            step_list.bind("<B1-Motion>", drag_step)
            step_list.bind("<ButtonRelease-1>", finish_drag)

            ttk.Label(win, text="名称:").pack(anchor="w", padx=12)
            ttk.Entry(win, textvariable=name_var).pack(fill="x", padx=12)
            ttk.Label(win, text="按键 / 持续时间:").pack(anchor="w", padx=12, pady=(8, 0))
            row = ttk.Frame(win)
            row.pack(fill="x", padx=12, pady=(0, 6))
            ttk.Combobox(row, textvariable=key_var, values=["W", "A", "S", "D", "Q", "E", "R", "F", "Z", "X", "C", "V"], state="readonly", width=10).pack(side="left")
            ttk.Entry(row, textvariable=duration_var, width=10).pack(side="left", padx=(8, 0))

            timing_row = ttk.Frame(win)
            timing_row.pack(fill="x", padx=12, pady=(4, 8))
            ttk.Label(timing_row, text="执行前延时(秒):").pack(side="left")
            ttk.Entry(timing_row, textvariable=delay_before_var, width=10).pack(side="left", padx=(8, 0))
            ttk.Label(timing_row, text="执行后等待(秒):").pack(side="left", padx=(16, 8))
            ttk.Entry(timing_row, textvariable=after_wait_var, width=10).pack(side="left")

            def add_step():
                try:
                    duration = float(duration_var.get() or 1.0)
                except ValueError:
                    duration = 1.0
                step_list.insert(tk.END, f"{key_var.get()} {duration}s")

            def remove_step():
                idx = step_list.curselection()
                if idx:
                    step_list.delete(idx[0])

            row2 = ttk.Frame(win)
            row2.pack(fill="x", padx=12, pady=(0, 12))
            ttk.Button(row2, text="添加", command=add_step).pack(side="left")
            ttk.Button(row2, text="更新选中步骤", command=update_selected_step).pack(side="left", padx=(8, 0))
            ttk.Button(row2, text="删除", command=remove_step).pack(side="left", padx=(8, 0))

            def save():
                steps = []
                for i in range(step_list.size()):
                    text = step_list.get(i).strip()
                    if not text:
                        continue
                    parts = text.split()
                    if len(parts) >= 2:
                        try:
                            duration = float(parts[1].rstrip("sS"))
                        except ValueError:
                            duration = 1.0
                        steps.append({"key": parts[0], "duration": duration})
                detour_task["type"] = "keyboard_move"
                detour_task["description"] = name_var.get().strip() or "移动步骤"
                detour_task["move_steps"] = steps or [{"key": "W", "duration": 1.0}]
                detour_task["delay_before"] = max(0.0, float(delay_before_var.get() or 0.0))
                detour_task["after_wait"] = max(0.0, float(after_wait_var.get() or 0.0))
                detour_task["click"] = False
                detour_task["required"] = bool(detour_task.get("required", False))
                win.destroy()

            ttk.Button(win, text="保存", command=save).pack()
            return

        if task_type == "drag":
            name_var = tk.StringVar(value=str(detour_task.get("description", "拖曳步骤")))
            start_x_var = tk.StringVar(value=str(detour_task.get("start_x", 0)))
            start_y_var = tk.StringVar(value=str(detour_task.get("start_y", 0)))
            end_x_var = tk.StringVar(value=str(detour_task.get("end_x", 100)))
            end_y_var = tk.StringVar(value=str(detour_task.get("end_y", 100)))
            duration_var = tk.StringVar(value=str(detour_task.get("duration", 0.25)))

            ttk.Label(win, text="名称:").pack(anchor="w", padx=12, pady=(12, 0))
            ttk.Entry(win, textvariable=name_var).pack(fill="x", padx=12)
            for label_text, var in [("起点X", start_x_var), ("起点Y", start_y_var), ("终点X", end_x_var), ("终点Y", end_y_var), ("拖动时长(秒)", duration_var)]:
                row = ttk.Frame(win)
                row.pack(fill="x", padx=12, pady=6)
                ttk.Label(row, text=f"{label_text}:").pack(side="left")
                ttk.Entry(row, textvariable=var, width=18).pack(side="left", padx=(8, 0))

            def save():
                detour_task["type"] = "drag"
                detour_task["description"] = name_var.get().strip() or "拖曳步骤"
                detour_task["start_x"] = float(start_x_var.get() or 0)
                detour_task["start_y"] = float(start_y_var.get() or 0)
                detour_task["end_x"] = float(end_x_var.get() or 100)
                detour_task["end_y"] = float(end_y_var.get() or 100)
                detour_task["duration"] = float(duration_var.get() or 0.25)
                detour_task["click"] = False
                detour_task["required"] = bool(detour_task.get("required", False))
                detour_task["template"] = "drag"
                win.destroy()

            ttk.Button(win, text="保存", command=save).pack(pady=(12, 0))
            return

        if task_type == "click_until_gone":
            name_var = tk.StringVar(value=str(detour_task.get("description", "持续点击直到识别步骤")))
            saved_templates = detour_task.get("templates") or detour_task.get("template", "")
            if isinstance(saved_templates, (list, tuple)):
                saved_templates = ", ".join(str(item) for item in saved_templates)
            template_var = tk.StringVar(value=str(saved_templates))
            interval_var = tk.StringVar(value=str(detour_task.get("click_interval", 0.5)))
            timeout_var = tk.StringVar(value=str(detour_task.get("timeout", 30)))
            stop_delay_var = tk.StringVar(value=str(detour_task.get("stop_delay", 0.0)))
            continue_timeout_var = tk.BooleanVar(value=bool(detour_task.get("continue_after_timeout", False)))
            stop_on_change_var = tk.BooleanVar(value=bool(detour_task.get("stop_on_change", False)))
            saved_rects = detour_task.get("match_rects")
            current_rect = (saved_rects[0] if isinstance(saved_rects, list) and saved_rects else None) or detour_task.get("search_rect") or detour_task.get("match_rect") or ("", "", "", "")
            region_left_var = tk.StringVar(value=str(current_rect[0]))
            region_top_var = tk.StringVar(value=str(current_rect[1]))
            region_right_var = tk.StringVar(value=str(current_rect[2]))
            region_bottom_var = tk.StringVar(value=str(current_rect[3]))
            region_center_x_var = tk.StringVar(value="")
            region_center_y_var = tk.StringVar(value="")
            click_x_var = tk.StringVar(value=str(detour_task.get("click_x", "")))
            click_y_var = tk.StringVar(value=str(detour_task.get("click_y", "")))
            ttk.Label(win, text="步骤名称:").pack(anchor="w", padx=12, pady=(12, 0))
            ttk.Entry(win, textvariable=name_var).pack(fill="x", padx=12)
            ttk.Label(win, text="绑定图片:").pack(anchor="w", padx=12, pady=(8, 0))
            ttk.Entry(win, textvariable=template_var).pack(fill="x", padx=12)
            def bind_image():
                file_paths = filedialog.askopenfilenames(
                    title="选择持续点击图片",
                    initialdir=os.path.join(os.path.dirname(__file__), "icons"),
                    filetypes=[("PNG Images", "*.png"), ("All Files", "*.*")],
                )
                if file_paths:
                    current_names = [item.strip() for item in template_var.get().replace("，", ",").split(",") if item.strip()]
                    for file_path in file_paths:
                        image_name = os.path.splitext(os.path.basename(file_path))[0]
                        if image_name not in current_names:
                            current_names.append(image_name)
                    template_var.set(", ".join(current_names))
            ttk.Button(win, text="绑定图片", command=bind_image).pack(anchor="w", padx=12, pady=(4, 0))
            ttk.Label(win, text="点击间隔(秒):").pack(anchor="w", padx=12, pady=(8, 0))
            ttk.Entry(win, textvariable=interval_var).pack(fill="x", padx=12)
            ttk.Label(win, text="超时(秒，0为不限制):").pack(anchor="w", padx=12, pady=(8, 0))
            ttk.Entry(win, textvariable=timeout_var).pack(fill="x", padx=12)
            ttk.Button(
                win,
                text="框选识别区域",
                command=lambda: self._capture_detour_match_region(
                    detour_task,
                    region_left_var,
                    region_top_var,
                    region_right_var,
                    region_bottom_var,
                    region_center_x_var,
                    region_center_y_var,
                    click_x_var,
                    click_y_var,
                    win,
                ),
            ).pack(anchor="w", padx=12, pady=(8, 0))
            ttk.Button(
                win,
                text="清空识别区域",
                command=lambda: self._clear_detour_match_region(
                    detour_task,
                    region_left_var,
                    region_top_var,
                    region_right_var,
                    region_bottom_var,
                    region_center_x_var,
                    region_center_y_var,
                ),
            ).pack(anchor="w", padx=12, pady=(4, 0))
            ttk.Button(
                win,
                text="记录点击点",
                command=lambda: self._capture_detour_click_position(detour_task, click_x_var, click_y_var, win),
            ).pack(anchor="w", padx=12, pady=(8, 0))
            ttk.Checkbutton(
                win,
                text="超时后继续执行下一步骤",
                variable=continue_timeout_var,
            ).pack(anchor="w", padx=12, pady=(8, 0))
            change_row = ttk.Frame(win)
            change_row.pack(fill="x", padx=12, pady=(8, 0))
            ttk.Checkbutton(
                change_row,
                text="画面发生变化后视为识别成功",
                variable=stop_on_change_var,
            ).pack(side="left")
            ttk.Label(change_row, text="完成后等待(秒):").pack(side="left", padx=(16, 8))
            ttk.Entry(change_row, textvariable=stop_delay_var, width=10).pack(side="left")

            def save():
                detour_task["type"] = "click_until_gone"
                detour_task["description"] = name_var.get().strip() or "持续点击直到识别步骤"
                template_names = [item.strip() for item in template_var.get().replace("，", ",").split(",") if item.strip()]
                detour_task["templates"] = template_names
                detour_task["template"] = template_names[0] if template_names else ""
                detour_task["click_interval"] = max(0.01, float(interval_var.get() or 0.5))
                detour_task["stop_delay"] = max(0.0, float(stop_delay_var.get() or 0.0))
                detour_task["timeout"] = max(0.0, float(timeout_var.get() or 30))
                detour_task["continue_after_timeout"] = bool(continue_timeout_var.get())
                detour_task["stop_on_change"] = bool(stop_on_change_var.get())
                detour_task["click"] = True
                detour_task["required"] = True
                win.destroy()

            ttk.Button(win, text="保存持续点击设置", command=save).pack(pady=(12, 0))
            return

        ttk.Label(win, text="当前类型还未配置，先保存后再编辑").pack(padx=12, pady=12)
        ttk.Button(win, text="保存", command=lambda: win.destroy()).pack()

    def open_detour_editor(self):
        if self.selected_group_id is not None:
            return
        if not (0 <= self.selected_task_index < len(TASKS)):
            return
        task = TASKS[self.selected_task_index]
        if task.get("type", "normal") not in ("normal", "advanced"):
            return

        win = tk.Toplevel(self.root)
        win.title("迂回设置")
        win.geometry("620x470")
        win.transient(self.root)
        win.grab_set()

        detour_steps = task.setdefault("detour_steps", [])
        enabled_var = tk.BooleanVar(value=bool(task.get("detour_enabled", False)))
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
                if option_number == int(target_number):
                    return option
            return "不跳转"

        jump_var = tk.StringVar(value=jump_label(task.get("detour_jump_to")))
        success_jump_var = tk.StringVar(value=jump_label(task.get("detour_success_jump_to")))
        step_type_var = tk.StringVar(value="normal")
        step_list = tk.Listbox(win, height=12, exportselection=False)
        step_list.pack(fill="both", expand=True, padx=12, pady=(12, 8))

        def refresh_list():
            step_list.delete(0, tk.END)
            for step in detour_steps:
                desc = step.get("description") or step.get("template") or step.get("type", "步骤")
                step_list.insert(tk.END, f"{step.get('type', 'normal')} - {desc}")

        refresh_list()

        row = ttk.Frame(win)
        row.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Checkbutton(row, text="启用迂回", variable=enabled_var).pack(side="left")
        ttk.Label(row, text="未识别时跳到:").pack(side="left", padx=(18, 6))
        ttk.Combobox(row, textvariable=jump_var, values=jump_options, state="readonly", width=28).pack(side="left")

        success_row = ttk.Frame(win)
        success_row.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(success_row, text="识别成功后跳到:").pack(side="left", padx=(0, 6))
        ttk.Combobox(success_row, textvariable=success_jump_var, values=jump_options, state="readonly", width=28).pack(side="left")

        add_row = ttk.Frame(win)
        add_row.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Combobox(add_row, textvariable=step_type_var, values=["normal", "advanced", "key_press", "keyboard_move", "drag", "click_until_gone"], state="readonly", width=20).pack(side="left")
        ttk.Button(add_row, text="新增步骤", command=lambda: (detour_steps.append({"type": step_type_var.get() or "normal"}), refresh_list())).pack(side="left", padx=(8, 0))

        action_row = ttk.Frame(win)
        action_row.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(action_row, text="设置", command=lambda: self._configure_selected_detour_step(detour_steps, step_list)).pack(side="left", padx=(0, 8))
        ttk.Button(action_row, text="删除", command=lambda: (detour_steps.pop(step_list.curselection()[0]) if step_list.curselection() else None, refresh_list())).pack(side="left")

        def save():
            task["detour_enabled"] = bool(enabled_var.get())
            task["detour_steps"] = detour_steps
            task["detour_jump_to"] = jump_option_numbers.get(jump_var.get())
            task["detour_success_jump_to"] = jump_option_numbers.get(success_jump_var.get())
            self.save_current_tasks()
            self.refresh_task_list()
            self.load_task_to_form(self.selected_task_index)
            win.destroy()

        ttk.Button(win, text="保存迂回设置", command=save).pack(pady=(0, 12))

    def _configure_selected_detour_step(self, detour_steps, step_list):
        selection = step_list.curselection()
        if not selection:
            return
        detour_task = detour_steps[selection[0]]
        self.open_detour_step_config_editor(detour_task)
        step_list.delete(0, tk.END)
        for step in detour_steps:
            desc = step.get("description") or step.get("template") or step.get("type", "步骤")
            step_list.insert(tk.END, f"{step.get('type', 'normal')} - {desc}")

    def open_template_list_editor(self, title, task, key_name):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("360x320")
        win.transient(self.root)
        win.grab_set()

        current = task.get(key_name, [])
        if isinstance(current, str):
            current = [current]
        current = [str(item).strip() for item in current if str(item).strip()]

        ttk.Label(win, text=title, font=("Microsoft YaHei", 11, "bold")).pack(pady=(12, 8))
        template_list = tk.Listbox(win, height=10, exportselection=False)
        template_list.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        for item in current:
            template_list.insert(tk.END, item)

        def add_template():
            default_dir = os.path.join(os.getcwd(), "icons")
            file_path = filedialog.askopenfilename(
                title="选择要识别的图片",
                initialdir=default_dir,
                filetypes=[("PNG Images", "*.png"), ("All Files", "*.*")],
            )
            if not file_path:
                return
            template_name = os.path.splitext(os.path.basename(file_path))[0]
            existing = [template_list.get(i) for i in range(template_list.size())]
            if template_name in existing:
                return
            template_list.insert(tk.END, template_name)

        def remove_template():
            selection = template_list.curselection()
            if not selection:
                return
            idx = selection[0]
            template_list.delete(idx)

        action_row = ttk.Frame(win)
        action_row.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(action_row, text="新增图片", command=add_template).pack(side="left", padx=(0, 8))
        ttk.Button(action_row, text="删除选中", command=remove_template).pack(side="left")

        def save_list():
            values = [template_list.get(i) for i in range(template_list.size())]
            if not values:
                values = ["unfinished_stage"] if key_name == "stage_templates" else ["battle_ok"]
            task[key_name] = values
            if key_name == "stage_templates":
                task["template"] = values[0]
                task["stage_templates"] = values
            elif key_name == "next_templates":
                task["next_template"] = values[0] if values else None
                task["next_templates"] = values
            self.refresh_task_list()
            self.append_log(f"已保存模板列表: {values}")
            win.destroy()

        ttk.Button(win, text="保存", command=save_list).pack(pady=(0, 12))

    def open_stage_farm_settings(self):
        if not (0 <= self.selected_task_index < len(TASKS)):
            return

        task = TASKS[self.selected_task_index]
        win = tk.Toplevel(self.root)
        win.title("主线/活动关卡设置")
        win.geometry("430x440")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="主线/活动关卡高级设置", font=("Microsoft YaHei", 12, "bold")).pack(pady=(12, 8))

        form = ttk.Frame(win, padding=12)
        form.pack(fill="both", expand=True)

        task_type_var = tk.StringVar(value=task.get("type", "stage_farm"))
        drag_distance_var = tk.StringVar(value=str(task.get("drag_distance", 220)))
        drag_wait_var = tk.StringVar(value=str(task.get("drag_wait", 0.8)))
        max_rounds_var = tk.StringVar(value=str(task.get("max_rounds", 12)))
        direction_var = tk.StringVar(value=str(task.get("direction", 1)))
        stage_templates_value = task.get("stage_templates") or [task.get("template") or "unfinished_stage"]
        next_templates_value = task.get("next_templates") or ([task.get("next_template")] if task.get("next_template") else [])

        stage_row = ttk.Frame(form)
        stage_row.pack(fill="x", pady=4)
        ttk.Label(stage_row, text="未完成关卡模板:").pack(side="left", padx=(0, 8), anchor="w")
        ttk.Button(stage_row, text="编辑图片列表", command=lambda: self.open_template_list_editor("未完成关卡模板", task, "stage_templates")).pack(side="left")
        ttk.Label(stage_row, text=str(stage_templates_value)).pack(side="left", padx=(10, 0), anchor="w")

        next_row = ttk.Frame(form)
        next_row.pack(fill="x", pady=4)
        ttk.Label(next_row, text="二级模板:").pack(side="left", padx=(0, 8), anchor="w")
        ttk.Button(next_row, text="编辑图片列表", command=lambda: self.open_template_list_editor("二级界面模板", task, "next_templates")).pack(side="left")
        ttk.Label(next_row, text=str(next_templates_value)).pack(side="left", padx=(10, 0), anchor="w")

        fields = [
            ("任务类型", task_type_var, "combo", ["stage_farm"]),
            ("拖动距离", drag_distance_var, "entry", None),
            ("拖动后等待", drag_wait_var, "entry", None),
            ("最大轮数", max_rounds_var, "entry", None),
            ("拖动方向", direction_var, "combo", ["1", "-1"]),
        ]

        for label_text, var, kind, options in fields:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8), anchor="w")
            if kind == "combo":
                ttk.Combobox(row, textvariable=var, values=options, state="readonly", width=18).pack(side="left")
            else:
                ttk.Entry(row, textvariable=var, width=18).pack(side="left")

        def save_settings():
            task["type"] = task_type_var.get().strip() or "stage_farm"
            task["drag_distance"] = float(drag_distance_var.get() or 220)
            task["drag_wait"] = float(drag_wait_var.get() or 0.8)
            task["max_rounds"] = int(float(max_rounds_var.get() or 12))
            task["direction"] = int(direction_var.get() or 1)
            task["wait_for"] = "next_appear"
            task["description"] = task.get("description", "主线/活动关卡")
            task.setdefault("stage_templates", [task.get("template") or "unfinished_stage"])
            task.setdefault("next_templates", [task.get("next_template")] if task.get("next_template") else [])
            self.refresh_task_list()
            self.append_log(f"已保存关卡扫描设置: {', '.join(task.get('stage_templates', []))}")
            win.destroy()

        ttk.Button(form, text="保存", command=save_settings).pack(pady=(12, 0))

    def open_keyboard_move_settings(self, task):
        win = tk.Toplevel(self.root)
        win.title("每日移动操作设置")
        win.geometry("430x300")
        win.transient(self.root)
        win.grab_set()

        form = ttk.Frame(win, padding=12)
        form.pack(fill="both", expand=True)

        tk.Label(form, text="移动步骤", font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
        step_list = tk.Listbox(form, height=8, exportselection=False)
        step_list.pack(fill="x", pady=(6, 8))

        key_var = tk.StringVar(value="W")
        duration_var = tk.StringVar(value="1.0")
        delay_before_var = tk.StringVar(value=str(task.get("delay_before", 0.0)))
        after_wait_var = tk.StringVar(value=str(task.get("after_wait", 0.0)))
        ttk.Frame(form).pack(fill="x", pady=(0, 6))
        ttk.Label(form, text="按键:").pack(anchor="w")
        ttk.Combobox(form, textvariable=key_var, values=["W", "A", "S", "D", "Q", "E", "R", "F", "Z", "X", "C", "V"], state="readonly", width=10).pack(anchor="w")
        ttk.Label(form, text="持续时间(秒):").pack(anchor="w", pady=(6, 0))
        ttk.Entry(form, textvariable=duration_var, width=12).pack(anchor="w")
        timing_row = ttk.Frame(form)
        timing_row.pack(fill="x", pady=(8, 0))
        ttk.Label(timing_row, text="执行前延时(秒):").pack(side="left")
        ttk.Entry(timing_row, textvariable=delay_before_var, width=10).pack(side="left", padx=(8, 0))
        ttk.Label(timing_row, text="执行后等待(秒):").pack(side="left", padx=(12, 8))
        ttk.Entry(timing_row, textvariable=after_wait_var, width=10).pack(side="left")

        existing_steps = task.get("move_steps", [])
        for step in existing_steps:
            step_list.insert(tk.END, f"{step.get('key','W')} {step.get('duration', 1.0)}s")

        drag_index = {"value": None}

        def load_selected_step(_event=None):
            selection = step_list.curselection()
            if selection:
                parts = step_list.get(selection[0]).split()
                key_var.set(parts[0])
                duration_var.set(parts[1].rstrip("sS") if len(parts) > 1 else "1.0")

        def update_selected_step():
            selection = step_list.curselection()
            if not selection:
                return
            try:
                duration = float(duration_var.get() or 1.0)
            except ValueError:
                duration = 1.0
            step_list.delete(selection[0])
            step_list.insert(selection[0], f"{key_var.get()} {duration}s")
            step_list.selection_set(selection[0])

        def start_drag(event):
            drag_index["value"] = step_list.nearest(event.y)

        def drag_step(event):
            source = drag_index["value"]
            target = step_list.nearest(event.y)
            if source is None or target == source or target < 0 or target >= step_list.size():
                return
            value = step_list.get(source)
            step_list.delete(source)
            step_list.insert(target, value)
            step_list.selection_set(target)
            drag_index["value"] = target

        def finish_drag(_event):
            drag_index["value"] = None

        step_list.bind("<<ListboxSelect>>", load_selected_step)
        step_list.bind("<ButtonPress-1>", start_drag)
        step_list.bind("<B1-Motion>", drag_step)
        step_list.bind("<ButtonRelease-1>", finish_drag)

        def add_step():
            key = key_var.get().strip()
            try:
                duration = float(duration_var.get() or 1.0)
            except ValueError:
                duration = 1.0
            step_list.insert(tk.END, f"{key} {duration}s")
            task.setdefault("move_steps", []).append({"key": key, "duration": duration})

        def remove_step():
            if step_list.curselection():
                idx = step_list.curselection()[0]
                step_list.delete(idx)
                if "move_steps" in task and idx < len(task["move_steps"]):
                    del task["move_steps"][idx]

        button_row = ttk.Frame(form)
        button_row.pack(fill="x", pady=(10, 0))
        ttk.Button(button_row, text="添加步骤", command=add_step).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="更新选中步骤", command=update_selected_step).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="删除步骤", command=remove_step).pack(side="left")

        def save():
            steps = []
            for i in range(step_list.size()):
                parts = step_list.get(i).split()
                if len(parts) < 2:
                    continue
                try:
                    duration = float(parts[1].rstrip("sS"))
                except ValueError:
                    duration = 1.0
                steps.append({"key": parts[0], "duration": duration})
            task["move_steps"] = steps
            task["delay_before"] = max(0.0, float(delay_before_var.get() or 0.0))
            task["after_wait"] = max(0.0, float(after_wait_var.get() or 0.0))
            task.pop("trigger_action", None)
            task.pop("action_key", None)
            task.pop("exit_key", None)
            task.pop("exit_after", None)
            self.save_current_tasks()
            self.load_task_to_form(self.selected_task_index)
            self.append_log(f"已保存每日移动配置: {task.get('description', '移动操作')}")
            win.destroy()

        ttk.Button(form, text="保存", command=save).pack(pady=(12, 0))

    def open_drag_settings(self, task):
        win = tk.Toplevel(self.root)
        win.title("拖曳步骤设置")
        win.geometry("420x260")
        win.transient(self.root)
        win.grab_set()

        form = ttk.Frame(win, padding=12)
        form.pack(fill="both", expand=True)

        start_x_var = tk.StringVar(value=str(task.get("start_x", 0)))
        start_y_var = tk.StringVar(value=str(task.get("start_y", 0)))
        end_x_var = tk.StringVar(value=str(task.get("end_x", 100)))
        end_y_var = tk.StringVar(value=str(task.get("end_y", 100)))
        duration_var = tk.StringVar(value=str(task.get("duration", 0.25)))

        for label_text, var in [
            ("起点 X", start_x_var),
            ("起点 Y", start_y_var),
            ("终点 X", end_x_var),
            ("终点 Y", end_y_var),
            ("拖动时长(秒)", duration_var),
        ]:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=6)
            ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8))
            ttk.Entry(row, textvariable=var, width=18).pack(side="left")

        def save():
            task["start_x"] = float(start_x_var.get() or 0)
            task["start_y"] = float(start_y_var.get() or 0)
            task["end_x"] = float(end_x_var.get() or 100)
            task["end_y"] = float(end_y_var.get() or 100)
            task["duration"] = float(duration_var.get() or 0.25)
            task["template"] = "drag"
            task["description"] = task.get("description", "拖曳步骤")
            self.save_current_tasks()
            self.refresh_task_list()
            self.load_task_to_form(self.selected_task_index)
            self.append_log("已保存拖曳步骤配置")
            win.destroy()

        ttk.Button(form, text="保存", command=save).pack(pady=(12, 0))

    def open_key_press_settings(self, task):
        win = tk.Toplevel(self.root)
        win.title("按键步骤设置")
        win.geometry("360x220")
        win.transient(self.root)
        win.grab_set()

        form = ttk.Frame(win, padding=12)
        form.pack(fill="both", expand=True)

        key_var = tk.StringVar(value=str(task.get("key") or task.get("template") or "E"))
        delay_var = tk.StringVar(value=str(task.get("delay_before", 0.0)))
        hold_var = tk.StringVar(value=str(task.get("hold_time", 0.1)))
        after_wait_var = tk.StringVar(value=str(task.get("after_wait", 0.2)))
        for label_text, var in [("按键", key_var), ("执行前延时(秒)", delay_var), ("按住时长(秒)", hold_var), ("执行后等待(秒)", after_wait_var)]:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=6)
            ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8))
            ttk.Entry(row, textvariable=var, width=18).pack(side="left")

        def save():
            key_value = key_var.get().strip() or "E"
            task["key"] = key_value.upper()
            task["template"] = task["key"]
            task["delay_before"] = max(0.0, float(delay_var.get() or 0.0))
            task["hold_time"] = float(hold_var.get() or 0.1)
            task["after_wait"] = max(0.0, float(after_wait_var.get() or 0.0))
            self.save_current_tasks()
            self.refresh_task_list()
            self.load_task_to_form(self.selected_task_index)
            self.append_log(f"已保存按键步骤: {key_value}")
            win.destroy()

        ttk.Button(form, text="保存", command=save).pack(pady=(12, 0))

    def open_event_entry_settings(self, task):
        win = tk.Toplevel(self.root)
        win.title("活动入口步骤设置")
        win.geometry("400x240")
        win.transient(self.root)
        win.grab_set()

        form = ttk.Frame(win, padding=12)
        form.pack(fill="both", expand=True)

        template_var = tk.StringVar(value=task.get("template", "event_entry"))
        fields = [
            ("入口模板", template_var),
        ]

        for label_text, var in fields:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8))
            ttk.Entry(row, textvariable=var, width=22).pack(side="left")

        def save():
            task["template"] = template_var.get().strip() or "event_entry"
            task["description"] = task.get("description", "活动入口步骤")
            self.refresh_task_list()
            self.append_log(f"已保存活动入口配置: {task['template']}")
            win.destroy()

        ttk.Button(form, text="保存", command=save).pack(pady=(12, 0))

    def open_reward_claim_settings(self, task):
        win = tk.Toplevel(self.root)
        win.title("领取奖励步骤设置")
        win.geometry("430x330")
        win.transient(self.root)
        win.grab_set()

        form = ttk.Frame(win, padding=12)
        form.pack(fill="both", expand=True)

        entry_template = tk.StringVar(value=task.get("template", "weekly_reward"))
        confirm_template = tk.StringVar(value=task.get("reward_confirm_template", "claim_confirm"))
        back_template = tk.StringVar(value=task.get("back_to_menu_template", "back_to_main_menu"))
        labels = [
            ("入口模板", entry_template),
            ("确认领取模板", confirm_template),
            ("返回主菜单模板", back_template),
        ]

        for label_text, var in labels:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=f"{label_text}:").pack(side="left", padx=(0, 8))
            ttk.Entry(row, textvariable=var, width=22).pack(side="left")

        def save():
            task["template"] = entry_template.get().strip() or "weekly_reward"
            task["reward_confirm_template"] = confirm_template.get().strip() or "claim_confirm"
            task["back_to_menu_template"] = back_template.get().strip() or "back_to_main_menu"
            task["description"] = task.get("description", "领取奖励")
            self.refresh_task_list()
            self.append_log(f"已保存奖励领取配置: {task['template']}")
            win.destroy()

        ttk.Button(form, text="保存", command=save).pack(pady=(12, 0))

    def add_task(self):
        win = tk.Toplevel(self.root)
        win.title("新增步骤类型")
        win.geometry("320x180")
        win.transient(self.root)
        win.grab_set()

        ttk.Label(win, text="请选择步骤类型:", font=("Microsoft YaHei", 10, "bold")).pack(pady=(16, 8))
        task_type_var = tk.StringVar(value="normal")
        ttk.Combobox(
            win,
            textvariable=task_type_var,
            values=["normal", "advanced", "keyboard_move", "key_press", "drag", "click_until_gone"],
            state="readonly",
            width=20,
        ).pack()

        def confirm_add():
            task_type = task_type_var.get() or "normal"
            selected_group_id = str(self.selected_group_id) if self.selected_group_id is not None else None
            selected_group_name = self.group_names.get(selected_group_id, "默认分组") if selected_group_id else None
            selected = list(self.task_listbox.curselection())
            if selected:
                entry = self.task_display_map.get(selected[0])
                if entry and entry[0] == "group" and selected_group_id is None:
                    selected_group_id = entry[1]
                    selected_group_name = self.group_names.get(selected_group_id, "默认分组")
                elif entry and entry[0] == "task" and selected_group_id is None:
                    selected_task = TASKS[entry[1]]
                    selected_group_id = str(selected_task.get("group_id") or "group_default")
                    selected_group_name = selected_task.get("group_name") or self.group_names.get(selected_group_id, "默认分组")
            if task_type == "advanced":
                new_task = {
                    "type": "advanced",
                    "mode": "custom",
                    "template": "new_step",
                    "templates": ["new_step"],
                    "timeout": 5,
                    "click": True,
                    "offset": (0, 0),
                    "after_wait": 0.25,
                    "wait_for": "time",
                    "wait_timeout": 2.5,
                    "required": True,
                    "description": "新增高级步骤",
                }
            elif task_type == "keyboard_move":
                new_task = {
                    "type": "keyboard_move",
                    "mode": "custom",
                    "template": "rest_room_entry",
                    "description": "新增每日移动步骤",
                    "click": False,
                    "delay_before": 0.0,
                    "after_wait": 1.0,
                    "wait_for": "time",
                    "wait_timeout": 8,
                    "required": True,
                    "move_steps": [{"key": "W", "duration": 1.0}],
                }
            elif task_type == "key_press":
                new_task = {
                    "type": "key_press",
                    "mode": "custom",
                    "template": "E",
                    "key": "E",
                    "delay_before": 0.0,
                    "hold_time": 0.1,
                    "after_wait": 0.2,
                    "click": False,
                    "after_wait": 0.2,
                    "wait_for": "time",
                    "wait_timeout": 1.0,
                    "required": True,
                    "description": "新增按键步骤",
                }
            elif task_type == "drag":
                new_task = {
                    "type": "drag",
                    "mode": "custom",
                    "template": "drag",
                    "start_x": 0,
                    "start_y": 0,
                    "end_x": 100,
                    "end_y": 100,
                    "duration": 0.25,
                    "click": False,
                    "after_wait": 0.2,
                    "wait_for": "time",
                    "wait_timeout": 1.0,
                    "required": True,
                    "description": "新增拖曳步骤",
                }
            elif task_type == "click_until_gone":
                new_task = {
                    "type": "click_until_gone",
                    "mode": "custom",
                    "template": "",
                    "description": "新增持续点击直到识别步骤",
                    "click": True,
                    "click_interval": 0.5,
                    "stop_delay": 0.0,
                    "timeout": 30.0,
                    "continue_after_timeout": False,
                    "stop_on_change": False,
                    "required": True,
                }
            else:
                new_task = {
                    "type": "normal",
                    "mode": "custom",
                    "template": "new_step",
                    "timeout": 5,
                    "click": True,
                    "offset": (0, 0),
                    "after_wait": 0.25,
                    "wait_for": "time",
                    "wait_timeout": 2.5,
                    "required": True,
                    "description": "新增普通步骤",
                }

            selected_index = self.selected_task_index if isinstance(self.selected_task_index, int) and 0 <= self.selected_task_index < len(TASKS) else len(TASKS)
            if selected_group_id is None:
                selected_group_id = self._get_default_group_id()
            if selected_group_id:
                new_task["group_id"] = str(selected_group_id)
                new_task["group_name"] = selected_group_name or self.group_names.get(selected_group_id, "默认分组")
            if selected_index < len(TASKS):
                TASKS.insert(selected_index + 1, new_task)
                self.selected_task_index = selected_index + 1
            else:
                TASKS.append(new_task)
                self.selected_task_index = len(TASKS) - 1
            self.selected_group_id = None
            self.save_current_tasks()
            self.refresh_task_list()
            self.task_listbox.selection_set(self._find_display_index_for_task(self.selected_task_index))
            self.load_task_to_form(self.selected_task_index)
            self.append_log(f"新增步骤: {new_task['description']} ({new_task['type']})")
            win.destroy()

        ttk.Button(win, text="确定", command=confirm_add).pack(pady=(18, 0))

    def copy_task(self):
        if not (0 <= self.selected_task_index < len(TASKS)):
            return
        task = TASKS[self.selected_task_index]
        cloned = {}
        for key, value in task.items():
            if isinstance(value, list):
                cloned[key] = list(value)
            elif isinstance(value, tuple):
                cloned[key] = tuple(value)
            else:
                cloned[key] = value
        cloned["description"] = f"{task.get('description', task.get('template', 'step'))} (副本)"
        cloned["template"] = f"{task.get('template', 'new_step')}_copy"
        insert_index = self.selected_task_index + 1
        TASKS.insert(insert_index, cloned)
        self.save_current_tasks()
        self.selected_task_index = insert_index
        self.refresh_task_list()
        self.task_listbox.selection_set(self.selected_task_index)
        self.load_task_to_form(self.selected_task_index)
        self.append_log(f"已复制步骤: {cloned['description']}")

    def delete_task(self, task_index=None):
        if not TASKS:
            return
        if task_index is None:
            task_index = self.selected_task_index
        if not (0 <= task_index < len(TASKS)):
            return
        self.selected_task_index = task_index
        task_name = TASKS[task_index].get("description", TASKS[task_index].get("template", "步骤"))
        if not messagebox.askyesno("确认删除", f"确定删除步骤“{task_name}”吗？"):
            return
        del TASKS[task_index]
        self.save_current_tasks()
        self.selected_task_index = min(task_index, max(len(TASKS) - 1, 0))
        self.refresh_task_list()
        if TASKS:
            self.task_listbox.selection_set(self._find_display_index_for_task(self.selected_task_index))
            self.load_task_to_form(self.selected_task_index)
        else:
            self.clear_task_form()
        self.append_log("已删除当前步骤")

    def _get_sibling_entries_for_parent(self, parent_group_id):
        parent_group_id = str(parent_group_id or "group_default")
        entries = []
        for child_group_id in self.group_children.get(parent_group_id, []):
            if child_group_id in self.group_names:
                entries.append(("group", child_group_id))
        for task_index, task in enumerate(TASKS):
            if str(task.get("group_id") or "group_default") == parent_group_id:
                entries.append(("task", task_index))
        return entries

    def _apply_sibling_entries_for_parent(self, parent_group_id, ordered_entries):
        parent_group_id = str(parent_group_id or "group_default")
        ordered_group_ids = [value for kind, value in ordered_entries if kind == "group"]
        ordered_task_indices = [value for kind, value in ordered_entries if kind == "task"]

        self.group_children[parent_group_id] = ordered_group_ids

        parent_task_indices = [idx for idx, task in enumerate(TASKS) if str(task.get("group_id") or "group_default") == parent_group_id]
        if not parent_task_indices:
            return

        desired_task_indices = [idx for idx in ordered_task_indices if idx in parent_task_indices]
        if not desired_task_indices:
            return

        kept_tasks = [task for idx, task in enumerate(TASKS) if idx not in parent_task_indices]
        desired_tasks = [TASKS[idx] for idx in desired_task_indices]
        insertion_index = min(parent_task_indices)
        reordered = kept_tasks[:]
        reordered[insertion_index:insertion_index] = desired_tasks
        TASKS[:] = reordered

    def _collect_visible_entries(self):
        self._ensure_group_metadata()
        visible = []

        def walk_group(group_id, depth):
            group_id = str(group_id)
            if group_id not in self.group_names:
                return
            visible.append({"kind": "group", "group_id": group_id, "depth": depth})
            if not self.group_expanded.get(group_id, True):
                return

            for task_index, task in enumerate(TASKS):
                if str(task.get("group_id") or "group_default") == group_id:
                    visible.append({"kind": "task", "task_index": task_index, "depth": depth + 1, "group_id": group_id})

            for child_group_id in list(self.group_children.get(group_id, [])):
                if child_group_id in self.group_names:
                    walk_group(child_group_id, depth + 1)

        for group_id in list(self.group_order):
            walk_group(group_id, 0)

        return visible

    def refresh_task_list(self):
        self._ensure_group_metadata()
        self.task_listbox.delete(0, tk.END)
        self.task_display_map = {}

        visible_entries = self._collect_visible_entries()
        for row_index, entry in enumerate(visible_entries):
            depth = entry.get("depth", 0)
            indent = "  " * depth

            if entry["kind"] == "group":
                group_id = entry["group_id"]
                prefix = "▼" if self.group_expanded.get(group_id, True) else "▶"
                group_status = self._get_group_enabled_status(group_id)
                label = f"{indent}{prefix}  {self.group_names.get(group_id, '默认分组')} · {group_status}"
                self.task_display_map[row_index] = ("group", group_id)
                color = self.group_colors.get(group_id, "#eef3ff")
                bg = color
            else:
                task_index = entry["task_index"]
                task = TASKS[task_index]
                task["enabled"] = task.get("enabled", True)
                state = "✓" if task["enabled"] else " "
                task_type = task.get("type", "normal")
                check = "☑" if task.get("enabled", True) else "☐"
                detour_status = " · 已启用迂回" if task.get("detour_enabled") else ""
                label = f"{indent}  {check}  {task_index + 1}. {task_type} · {task.get('description', task.get('template', 'unknown'))}{detour_status}"
                self.task_display_map[row_index] = ("task", task_index)
                bg = "#ffffff" if row_index % 2 == 0 else "#f3f4f6"

                label = self._append_task_drag_handle(label)
            self.task_listbox.insert(tk.END, self._append_task_drag_handle(label) if entry["kind"] == "group" else label)
            self.task_listbox.itemconfigure(row_index, bg=bg, fg="#1f2937")

        if TASKS:
            if self.selected_group_id is not None:
                selection_target = next(
                    (row for row, entry in self.task_display_map.items() if entry == ("group", str(self.selected_group_id))),
                    None,
                )
                if selection_target is not None:
                    self.task_listbox.selection_set(selection_target)
                self.show_group_editor()
                return
            self.selected_task_index = min(self.selected_task_index if isinstance(self.selected_task_index, int) else 0, len(TASKS) - 1)
            selection_target = self._find_display_index_for_task(self.selected_task_index)
            if selection_target is not None:
                self.task_listbox.selection_set(selection_target)
            self.load_task_to_form(self.selected_task_index)
        else:
            self.selected_task_index = 0
            self.clear_task_form()

    def _append_task_drag_handle(self, label):
        """在任务行右侧显示专用拖动手柄。"""
        handle = "☰"
        try:
            font = tkfont.Font(font=self.task_listbox.cget("font"))
            space_width = max(1, font.measure(" "))
            available_width = max(0, self.task_listbox.winfo_width() - 24)
            padding = max(2, int((available_width - font.measure(label) - font.measure(handle)) / space_width))
            return f"{label}{' ' * padding}{handle}"
        except tk.TclError:
            return f"{label}   {handle}"

    def select_all_tasks(self):
        for task in TASKS:
            task["enabled"] = True
        self.refresh_task_list()

    def clear_tasks(self):
        for task in TASKS:
            task["enabled"] = False
        self.refresh_task_list()

    def reorder_tasks(self, from_index, to_index):
        if not (0 <= from_index < len(TASKS)):
            return None
        if to_index < 0:
            to_index = 0
        if to_index >= len(TASKS):
            to_index = len(TASKS) - 1
        if from_index == to_index:
            return to_index

        task = TASKS.pop(from_index)
        TASKS.insert(to_index, task)
        self.save_current_tasks()
        self.refresh_task_list()
        self.task_listbox.selection_set(to_index)
        self.selected_task_index = to_index
        return to_index

    def move_task_up(self):
        selection = list(self.task_listbox.curselection())
        if not selection:
            return
        pos = selection[0]
        if pos == 0:
            return
        self.reorder_tasks(pos, pos - 1)

    def move_task_down(self):
        selection = list(self.task_listbox.curselection())
        if not selection:
            return
        pos = selection[0]
        if pos == self.task_listbox.size() - 1:
            return
        self.reorder_tasks(pos, pos + 1)

    def _selected_display_entry(self):
        selection = list(self.task_listbox.curselection())
        return self.task_display_map.get(selection[0]) if selection else None

    def move_selected_item(self, direction):
        selection = list(self.task_listbox.curselection())
        if not selection:
            return
        current_display = selection[0]
        target_display = current_display + (1 if direction > 0 else -1)
        if target_display < 0 or target_display >= self.task_listbox.size():
            return
        current_entry = self.task_display_map.get(current_display)
        target_entry = self.task_display_map.get(target_display)
        if not current_entry or not target_entry:
            return
        if current_entry[0] == "task":
            self._commit_task_drop(current_entry[1], target_entry, insert_after=direction > 0)
        else:
            group_id = str(current_entry[1])
            siblings = self._get_group_siblings(group_id)
            if group_id not in siblings:
                return
            new_index = siblings.index(group_id) + (1 if direction > 0 else -1)
            if new_index < 0 or new_index >= len(siblings):
                return
            siblings.remove(group_id)
            siblings.insert(new_index, group_id)
            parent_id = self.group_parents.get(group_id)
            if parent_id is None:
                self.group_order = siblings
            else:
                self.group_children[parent_id] = siblings
            self.selected_group_id = group_id
            self.save_current_tasks()
            self.refresh_task_list()

    def copy_selected_item(self):
        entry = self._selected_display_entry()
        if not entry:
            return
        if entry[0] == "group":
            self.copy_group()
        else:
            self.copy_task()

    def delete_selected_item(self):
        selected_entries = [
            self.task_display_map[index]
            for index in self.task_listbox.curselection()
            if index in self.task_display_map
        ]
        if not selected_entries:
            return

        task_indices = set()
        group_ids = set()
        for kind, value in selected_entries:
            if kind == "task":
                task_indices.add(value)
            elif kind == "group":
                group_id = str(value)
                group_ids.add(group_id)
                group_ids.update(str(item) for item in self._get_group_descendants(group_id))

        removed_task_indices = {
            index
            for index, task in enumerate(TASKS)
            if index in task_indices or str(task.get("group_id") or "group_default") in group_ids
        }
        if not removed_task_indices and not group_ids:
            return

        selected_count = len(removed_task_indices)
        group_count = len(group_ids)
        message = f"确定删除选中的 {selected_count} 个步骤"
        if group_count:
            message += f"和 {group_count} 个组"
        message += "吗？"
        if not messagebox.askyesno("确认批量删除", message):
            return

        TASKS[:] = [
            task for index, task in enumerate(TASKS)
            if index not in removed_task_indices
            and str(task.get("group_id") or "group_default") not in group_ids
        ]
        for group_id in group_ids:
            self.group_names.pop(group_id, None)
            self.group_colors.pop(group_id, None)
            self.group_expanded.pop(group_id, None)
            self.group_children.pop(group_id, None)
            self.group_parents.pop(group_id, None)
            self.group_order = [item for item in self.group_order if item != group_id]
        for parent_id, children in list(self.group_children.items()):
            self.group_children[parent_id] = [child for child in children if child not in group_ids]

        self.selected_group_id = None
        self.selected_task_index = 0
        self.save_current_tasks()
        self.refresh_task_list()
        self.append_log(f"已批量删除 {selected_count} 个步骤和 {group_count} 个组。")

    def move_group_up(self):
        if not self.group_order:
            return
        selected = list(self.task_listbox.curselection())
        if not selected:
            return
        entry = self.task_display_map.get(selected[0])
        if not entry or entry[0] != "group":
            return
        group_id = entry[1]
        siblings = self._get_group_siblings(group_id)
        if group_id not in siblings:
            return
        group_index = siblings.index(group_id)
        if group_index <= 0:
            return
        siblings[group_index], siblings[group_index - 1] = siblings[group_index - 1], siblings[group_index]
        if self.group_parents.get(group_id) is None:
            self.group_order = siblings
        else:
            self.group_children[self.group_parents[group_id]] = siblings
        self.save_current_tasks()
        self.refresh_task_list()

    def move_group_down(self):
        if not self.group_order:
            return
        selected = list(self.task_listbox.curselection())
        if not selected:
            return
        entry = self.task_display_map.get(selected[0])
        if not entry or entry[0] != "group":
            return
        group_id = entry[1]
        siblings = self._get_group_siblings(group_id)
        if group_id not in siblings:
            return
        group_index = siblings.index(group_id)
        if group_index >= len(siblings) - 1:
            return
        siblings[group_index], siblings[group_index + 1] = siblings[group_index + 1], siblings[group_index]
        if self.group_parents.get(group_id) is None:
            self.group_order = siblings
        else:
            self.group_children[self.group_parents[group_id]] = siblings
        self.save_current_tasks()
        self.refresh_task_list()

    def _reparent_group(self, group_id, new_parent_id=None, insert_after_group_id=None):
        group_id = str(group_id)
        if new_parent_id is not None:
            new_parent_id = str(new_parent_id)
            if group_id == new_parent_id:
                return
        current_parent = self.group_parents.get(group_id)
        if current_parent is not None and group_id in self.group_children.get(current_parent, []):
            self.group_children[current_parent] = [child for child in self.group_children.get(current_parent, []) if child != group_id]
        if group_id in self.group_order:
            self.group_order = [gid for gid in self.group_order if gid != group_id]

        self.group_parents[group_id] = None if new_parent_id is None else new_parent_id
        target_parent = self.group_parents.get(group_id)
        if target_parent is None:
            siblings = [gid for gid in self.group_order if gid != group_id]
            if insert_after_group_id is not None:
                insert_after_group_id = str(insert_after_group_id)
                if insert_after_group_id in siblings:
                    idx = siblings.index(insert_after_group_id)
                    siblings.insert(idx + 1, group_id)
                else:
                    siblings.append(group_id)
            elif group_id not in siblings:
                siblings.append(group_id)
            self.group_order = siblings
            return

        self.group_children.setdefault(target_parent, [])
        siblings = [gid for gid in self.group_children.get(target_parent, []) if gid != group_id]
        if insert_after_group_id is not None:
            insert_after_group_id = str(insert_after_group_id)
            if insert_after_group_id in siblings:
                idx = siblings.index(insert_after_group_id)
                siblings.insert(idx + 1, group_id)
            else:
                siblings.append(group_id)
        elif group_id not in siblings:
            siblings.append(group_id)
        self.group_children[target_parent] = siblings

    def _commit_task_drop(self, source_index, target_entry, insert_after=False):
        if not (0 <= source_index < len(TASKS)) or not target_entry:
            return
        if target_entry[0] == "task" and target_entry[1] == source_index:
            return
        task = TASKS.pop(source_index)
        if target_entry[0] == "group":
            target_group_id = str(target_entry[1])
            task["group_id"] = target_group_id
            task["group_name"] = self.group_names.get(target_group_id, "默认分组")
            target_indices = [idx for idx, item in enumerate(TASKS) if str(item.get("group_id") or "group_default") == target_group_id]
            insert_at = max(target_indices) + 1 if target_indices else len(TASKS)
        else:
            target_index = target_entry[1]
            if target_index > source_index:
                target_index -= 1
            target_group_id = str(TASKS[target_index].get("group_id") or "group_default")
            task["group_id"] = target_group_id
            task["group_name"] = self.group_names.get(target_group_id, "默认分组")
            insert_at = target_index + (1 if insert_after else 0)
            insert_at = max(0, min(insert_at, len(TASKS)))
        TASKS.insert(insert_at, task)
        self.selected_task_index = TASKS.index(task)
        self.selected_group_id = None
        self.save_current_tasks()
        self.refresh_task_list()
        display_target = self._find_display_index_for_task(self.selected_task_index)
        if display_target is not None:
            self.task_listbox.selection_set(display_target)

    def _commit_group_drop(self, source_group_id, target_entry):
        source_group_id = str(source_group_id)
        if not target_entry:
            return
        if target_entry[0] == "group":
            target_group_id = str(target_entry[1])
            if target_group_id == source_group_id or target_group_id in self._get_group_descendants(source_group_id):
                return
            self._reparent_group(source_group_id, new_parent_id=target_group_id)
        else:
            target_group_id = str(TASKS[target_entry[1]].get("group_id") or "group_default")
            self._reparent_group(source_group_id, new_parent_id=self.group_parents.get(target_group_id), insert_after_group_id=target_group_id)
        self.selected_group_id = source_group_id
        self.selected_task_index = None
        self.save_current_tasks()
        self.refresh_task_list()

    def _show_drag_indicator(self, display_index):
        if self.drag_indicator is None:
            self.drag_indicator = tk.Frame(self.task_listbox, bg="#2563eb", height=2)
        bbox = self.task_listbox.bbox(display_index)
        if bbox:
            x, y, width, height = bbox
            self.drag_indicator.place(x=2, y=y, width=max(10, self.task_listbox.winfo_width() - 4), height=2)
            self.drag_indicator.lift()

    def _hide_drag_indicator(self):
        if self.drag_indicator is not None:
            self.drag_indicator.place_forget()

    def _is_valid_drag_target(self, target_display_index):
        if self.drag_kind is None or self.drag_target_display_index is None:
            return False
        target_entry = self.task_display_map.get(target_display_index)
        if not target_entry:
            return False
        if self.drag_kind == "task":
            return target_entry[0] == "task" and target_entry[1] != self.drag_index or target_entry[0] == "group"
        if self.drag_kind == "group":
            if target_entry[0] == "group":
                target_group_id = str(target_entry[1])
                return target_group_id != str(self.drag_group_id) and target_group_id not in self._get_group_descendants(self.drag_group_id)
            return target_entry[0] == "task"
        return False

    def _update_drag_indicator(self, target_display_index):
        if self._is_valid_drag_target(target_display_index):
            self._show_drag_indicator(target_display_index)
        else:
            self._hide_drag_indicator()

    def on_task_drag_start(self, event):
        if not TASKS:
            return
        if event.x < self.task_listbox.winfo_width() - self.drag_handle_width:
            self.drag_kind = None
            self.drag_index = None
            self.drag_group_id = None
            self._hide_drag_indicator()
            return
        display_index = self.task_listbox.nearest(event.y)
        if display_index < 0:
            return
        entry = self.task_display_map.get(display_index)
        if not entry:
            return
        self.drag_kind = entry[0]
        self.drag_group_id = None
        self.drag_index = None
        self.drag_last_display_index = display_index
        self.drag_target_display_index = display_index
        self.drag_autoscroll_job = self.root.after(80, self._drag_autoscroll)
        if entry[0] == "group":
            self.drag_group_id = entry[1]
            self.selected_task_index = self._resolve_display_selection(display_index) or 0
        elif entry[0] == "task":
            self.drag_index = entry[1]
            self.selected_task_index = entry[1]
        self.drag_target_display_index = display_index
        self._hide_drag_indicator()

    def on_task_drag_motion(self, event):
        if not TASKS:
            return
        target_display_index = self.task_listbox.nearest(event.y)
        if target_display_index < 0:
            return
        if getattr(self, "drag_last_display_index", None) == target_display_index:
            return
        self.drag_last_display_index = target_display_index

        entry = self.task_display_map.get(target_display_index)
        if not entry:
            return

        self.drag_target_display_index = target_display_index
        self._update_drag_indicator(target_display_index)
        list_height = self.task_listbox.winfo_height()
        if event.y < 24:
            self.task_listbox.yview_scroll(-1, "units")
        elif event.y > list_height - 24:
            self.task_listbox.yview_scroll(1, "units")

        if self.drag_kind == "task":
            return

        if self.drag_kind == "group":
            return

        if self.drag_index is None:
            return

        return

    def on_task_drag_release(self, event):
        if self.drag_kind == "task" and self.drag_index is not None:
            target_entry = self.task_display_map.get(self.drag_target_display_index)
            self._commit_task_drop(self.drag_index, target_entry)
        elif self.drag_kind == "group" and self.drag_group_id is not None:
            target_entry = self.task_display_map.get(self.drag_target_display_index)
            self._commit_group_drop(self.drag_group_id, target_entry)
        self._hide_drag_indicator()
        if self.drag_autoscroll_job is not None:
            self.root.after_cancel(self.drag_autoscroll_job)
            self.drag_autoscroll_job = None
        self.drag_index = None
        self.drag_kind = None
        self.drag_group_id = None
        self.drag_last_display_index = None
        self.drag_target_display_index = None

    def _drag_autoscroll(self):
        if self.drag_kind is None:
            return
        pointer_y = self.task_listbox.winfo_pointery() - self.task_listbox.winfo_rooty()
        height = self.task_listbox.winfo_height()
        if pointer_y < 28:
            self.task_listbox.yview_scroll(-1, "units")
        elif pointer_y > height - 28:
            self.task_listbox.yview_scroll(1, "units")
        display_index = self.task_listbox.nearest(pointer_y)
        if 0 <= display_index < self.task_listbox.size():
            self.drag_target_display_index = display_index
            self._update_drag_indicator(display_index)
        self.drag_autoscroll_job = self.root.after(80, self._drag_autoscroll)

    def get_selected_tasks(self):
        enabled_tasks = []
        for task_index, task in enumerate(TASKS):
            if task.get("enabled", True):
                runtime_task = dict(task)
                runtime_task["_outer_step_number"] = task_index + 1
                enabled_tasks.append(runtime_task)
        if enabled_tasks:
            return enabled_tasks
        return []

    def append_log(self, message):
        self.log_box.configure(state="normal")
        self.log_box.insert(tk.END, message + "\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state="disabled")

    def start_script(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return

        mode = self.mode_var.get() or "custom"
        selected_tasks = self.get_selected_tasks()

        window_title = self.window_var.get().strip()
        config.TARGET_WINDOW_TITLE = window_title or None
        config.USE_WINDOW_MODE = bool(window_title)

        self.stop_event.clear()
        self.status_var.set("运行中")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.append_log(f"脚本启动... 当前功能: {mode}，任务路径: {[task.get('description', task.get('template', 'unknown')) for task in selected_tasks]}")
        if window_title:
            self.append_log(f"目标窗口: {window_title}")
        else:
            self.append_log("未指定窗口标题，默认使用当前活动窗口或全屏区域")

        self.worker_thread = threading.Thread(
            target=self.run_worker,
            args=(selected_tasks,),
            daemon=True,
        )
        self.worker_thread.start()

    def stop_script(self):
        self.stop_event.set()
        self.status_var.set("停止中")
        self.append_log("正在停止脚本...")
        self.stop_btn.config(state="disabled")

    def run_worker(self, selected_tasks):
        try:
            run_task_queue(
                selected_tasks,
                loop=self.loop_var.get(),
                stop_flag=self.stop_event,
                log_callback=self.append_log,
            )
            self.status_var.set("已停止" if self.stop_event.is_set() else "已完成")
            self.append_log("脚本执行结束。")
        except Exception as exc:
            self.status_var.set("异常")
            self.append_log(f"脚本异常: {exc}")
        finally:
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")


if __name__ == "__main__":
    root = tk.Tk()
    app = AutoScriptGUI(root)
    root.mainloop()
