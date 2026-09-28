# -*- coding: utf-8 -*-
"""预设管理（新建 / 改名 / 复制 / 删除 / 导出 / 导入 + 预设文件落盘）。

从 `gui_pyside6.py` 的 `PySide6ScriptWindow` 整体外移出来的内聚职责块（8 个方法）。
窗口类通过 `PresetManagerMixin` 混入这些方法，`self.xxx()` 的既有调用方式不变。

为什么能安全外移：这些方法只通过 `tasks.save_*` / `tasks.*_FILE` 写盘 ——
路径常量在调用时读取，所以测试里「把 `tasks.PRESETS_FILE` 等重定向到临时目录」
的隔离在跨模块时依然有效（见 `tests/test_editor_roundtrip.py` 的
`DataIsolationSeamTests` 元守卫）。
"""
import json
import os
import zipfile
from copy import deepcopy

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

import config
from main import reload_templates
from tasks import (
    TASK_PRESETS,
    TASKS,
    save_blueprint_graphs,
    save_blueprint_layouts,
    save_presets,
)


class PresetManagerMixin:
    """预设管理相关方法；依赖宿主窗口提供 mode_tasks / mode_combo / append_log 等。"""

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
