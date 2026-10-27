# -*- coding: utf-8 -*-
"""蓝图界面的片段（Blender 式群组）支持：保存选中为片段 / 插入片段成为群组节点。

设计原则（与主窗口共用一套数据，不另建系统）：
  * 片段存储复用 `snippets.py` 的 v2 格式（`saved_presets.json` 的 `__snippets__`）；
  * 保存用 `extract_snippet()`，插入用 `insert_snippet()`；
  * 蓝图窗口只通过回调访问片段库与保存动作（`snippets_provider` / `snippets_save`），
    由主窗口在 `open_blueprint()` 时注入，避免蓝图持有第二份片段数据；
  * 插入后默认把整个片段装进一个**新的外层组**（名字=片段名），成为画布上可双击进入的
    "群组节点"；片段内部原有的分组嵌套原样保留；
  * 插入/保存都走蓝图自己的撤销栈（`_push_history`）与保存回调（`save_layout`）。
"""

import uuid

from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox

import snippets as snippet_store
from blueprint_canvas import BlueprintGroupItem, BlueprintNodeItem


class BlueprintSnippetMixin:
    """蓝图窗口的片段菜单/保存/插入逻辑（mixin，供 BlueprintWindow 继承）。"""

    DEFAULT_GROUP_COLOR = "#eab308"

    # ---------- 片段库访问 ----------

    def _snippet_store(self):
        provider = getattr(self, "snippets_provider", None)
        if callable(provider):
            try:
                store = provider()
            except Exception:
                return {}
            # 注意：空 dict 是假值，不能写 `provider() or {}`，否则会拿到新 dict、
            # 保存片段时写进临时对象里丢掉（"保存成功但片段列表为空"）。
            return store if isinstance(store, dict) else {}
        store = getattr(self, "snippets", None)
        return store if isinstance(store, dict) else {}

    def _save_snippet_store(self):
        saver = getattr(self, "snippets_save", None)
        if callable(saver):
            saver()

    # ---------- 选中 → 片段 ----------

    def _selected_snippet_indices(self):
        """选中节点 + 选中组（含子组）的步骤下标并集。"""
        indices = {item.index for item in self.scene.selectedItems()
                   if isinstance(item, BlueprintNodeItem)}
        for item in self.scene.selectedItems():
            if isinstance(item, BlueprintGroupItem):
                indices.update(self._group_all_node_indices(item.group_id))
        return sorted(index for index in indices if 0 <= index < len(self.tasks))

    def _selected_snippet_group_ids(self):
        return [item.group_id for item in self.scene.selectedItems()
                if isinstance(item, BlueprintGroupItem)]

    def save_selection_as_snippet(self):
        """把当前选中的步骤/分组保存为片段；返回片段名（取消/失败返回 None）。"""
        indices = self._selected_snippet_indices()
        if not indices:
            QMessageBox.information(self, "没有可保存的步骤",
                                    "请先在蓝图中选中要保存为片段的步骤或分组。")
            return None
        group_ids = self._selected_snippet_group_ids()
        payload = snippet_store.extract_snippet(self.tasks, indices, self.group_metadata, group_ids)
        if not payload.get("tasks") and not payload.get("groups"):
            QMessageBox.information(self, "没有可保存的内容", "选中的节点没有可保存的步骤。")
            return None
        name, accepted = QInputDialog.getText(self, "保存为片段", "片段名称：", text="片段")
        name = (name or "").strip()
        if not accepted or not name:
            return None
        store = self._snippet_store()
        if name in store:
            answer = QMessageBox.question(self, "覆盖片段", f"片段「{name}」已存在，是否覆盖？")
            if answer != QMessageBox.StandardButton.Yes:
                return None
        payload = self._strip_groups_for_single_step(payload, group_ids)
        store[name] = payload
        self._save_snippet_store()
        self._blueprint_status(
            f"已把 {len(payload.get('tasks') or [])} 个步骤、{len(payload.get('groups') or {})} 个分组保存为片段「{name}」。")
        return name

    @staticmethod
    def _strip_groups_for_single_step(payload, group_ids):
        """单个步骤 + 没有显式选组：保存成"纯步骤"片段，不携带所在分组。

        否则从组里挑一步存片段、再插入时会自动多出一个只有一步的组。
        选中组/多步骤仍保留分组结构（组+步骤片段是另一项功能）。
        """
        tasks = payload.get("tasks") or []
        if group_ids or len(tasks) != 1:
            return payload
        for task in tasks:
            task.pop("group_id", None)
            task.pop("group_name", None)
            task.pop("group_color", None)
        payload["groups"] = {}
        payload["group_order"] = []
        return payload

    def save_group_as_snippet(self, group_id):
        """把某个组（含子组与组内步骤）保存为片段。"""
        group_id = str(group_id)
        indices = sorted(self._group_all_node_indices(group_id))
        if not indices:
            QMessageBox.information(self, "没有可保存的步骤", "这个分组里还没有步骤。")
            return None
        name, accepted = QInputDialog.getText(self, "保存组为片段", "片段名称：", text="片段")
        name = (name or "").strip()
        if not accepted or not name:
            return None
        payload = snippet_store.extract_snippet(self.tasks, indices, self.group_metadata, (group_id,))
        store = self._snippet_store()
        if name in store:
            answer = QMessageBox.question(self, "覆盖片段", f"片段「{name}」已存在，是否覆盖？")
            if answer != QMessageBox.StandardButton.Yes:
                return None
        store[name] = payload
        self._save_snippet_store()
        self._blueprint_status(f"已把分组「{self._group_names().get(group_id, group_id)}」保存为片段「{name}」。")
        return name

    # ---------- 片段 → 画布（Blender 式群组节点） ----------

    def insert_snippet_into_blueprint(self, name, scene_position=None):
        """在画布上插入片段：新建外层组包住片段（保留内部嵌套），并自动排布。"""
        snippet = self._snippet_store().get(name)
        if not snippet:
            return None
        snippet_tasks, snippet_groups, _order = snippet_store.normalize_payload(snippet)
        if not snippet_tasks and not snippet_groups:
            return None
        focus = getattr(self, "focus_group", None)
        parent_group = str(focus) if focus else None
        new_tasks, new_groups = snippet_store.insert_snippet(
            self.tasks, snippet, position=len(self.tasks), parent_group_id=parent_group)
        inserted = len(new_tasks) - len(self.tasks)
        self._inserted_tasks = new_tasks[len(self.tasks):]
        self._push_history()
        self.tasks[:] = new_tasks
        # 注意：**不再自动包一层"片段组"**——片段里原本没有分组时，插入后就是独立步骤；
        # 片段自带分组时只保留它自己的分组结构。
        self._register_inserted_groups(new_groups, parent_group)
        self._inserted_tasks = None
        self._place_inserted_nodes(inserted, scene_position)
        self.refresh()
        self.save_layout()
        self._select_inserted_nodes(inserted)
        group_note = f"，{len(new_groups)} 个分组" if new_groups else ""
        target_note = "到当前组里" if parent_group else "到画布"
        self._blueprint_status(f"已插入片段「{name}」：{inserted} 个步骤{group_note}{target_note}。")
        return None

    def _unique_group_name(self, base_name, taken_names):
        candidate = str(base_name or "片段")
        if candidate in taken_names:
            index = 2
            while f"{candidate} {index}" in taken_names:
                index += 1
            candidate = f"{candidate} {index}"
        taken_names.add(candidate)
        return candidate

    def _register_inserted_groups(self, new_groups, parent_group_id=None):
        """把 insert_snippet 返回的新分组登记进当前预设的分组元数据（不建外层包装组）。

        片段自带的分组按原样登记（父组在片段内部已重映射）；片段里没有分组的步骤保持
        根层（聚焦子图里插入时归入当前组，避免插到外面看不见）。
        """
        metadata = self.group_metadata
        names = metadata.setdefault("names", {})
        colors = metadata.setdefault("colors", {})
        parents = metadata.setdefault("parents", {})
        children = metadata.setdefault("children", {})
        order = metadata.setdefault("order", [])
        taken = set(str(value) for value in names.values())
        parent_group_id = str(parent_group_id) if parent_group_id else None
        for group in new_groups:
            group_id = str(group["id"])
            names[group_id] = self._unique_group_name(group.get("name"), taken)
            colors[group_id] = group.get("color") or self.DEFAULT_GROUP_COLOR
            parent = group.get("parent")
            parent = str(parent) if parent else parent_group_id
            parents[group_id] = parent
            children.setdefault(group_id, [])
            if parent:
                children.setdefault(parent, []).append(group_id)
            else:
                order.append(group_id)
        # 片段里没有分组的步骤：在聚焦子图里插入时归入当前组
        # （只处理本次插入的步骤，不能把画布上原有的根步骤也吸进聚焦组）
        if parent_group_id:
            focus_name = names.get(parent_group_id, parent_group_id)
            for task in self._inserted_tasks:
                if task.get("group_id") is None:
                    task["group_id"] = parent_group_id
                    task["group_name"] = focus_name
        # 步骤上的 group_name 与登记后的组名同步
        for task in self.tasks:
            group_id = task.get("group_id")
            if group_id is not None and str(group_id) in names:
                task["group_name"] = names[str(group_id)]

    def _place_inserted_nodes(self, inserted, scene_position):
        """新节点排布在插入点附近（默认在画布已有内容下方），避免叠在一起。"""
        if inserted <= 0:
            return
        positions = self.layout_data.setdefault("positions", {})
        if scene_position is None:
            scene_position = self._empty_area_position()
        base_x, base_y = float(scene_position[0]), float(scene_position[1])
        first_index = len(self.tasks) - inserted
        for offset in range(inserted):
            index = first_index + offset
            positions[str(index)] = (base_x + (offset % 3) * 280.0,
                                     base_y + (offset // 3) * 140.0)

    def _empty_area_position(self):
        positions = self.layout_data.get("positions") or {}
        max_y = 40.0
        for value in positions.values():
            if isinstance(value, (list, tuple)) and len(value) == 2:
                max_y = max(max_y, float(value[1]) + 160.0)
        return 80.0, max_y

    def _select_inserted_nodes(self, inserted):
        if inserted <= 0:
            return
        first_index = len(self.tasks) - inserted
        self._refreshing = True
        try:
            for index in range(first_index, len(self.tasks)):
                item = self._node_items.get(index)
                if item is not None:
                    item.setSelected(True)
        finally:
            self._refreshing = False

    # ---------- 菜单 ----------

    def _add_snippet_menu(self, menu, scene_position=None):
        """在画布右键菜单里挂「片段」子菜单（插入/保存）。"""
        snippet_menu = menu.addMenu("片段（群组）")
        names = sorted(self._snippet_store())
        insert_menu = snippet_menu.addMenu("插入片段")
        insert_menu.setEnabled(bool(names))
        for name in names:
            action = insert_menu.addAction(name)
            action.triggered.connect(
                lambda checked=False, snippet_name=name: self.insert_snippet_into_blueprint(
                    snippet_name, scene_position))
        save_action = snippet_menu.addAction("保存选中为片段…")
        save_action.triggered.connect(self.save_selection_as_snippet)
        return snippet_menu

    def _add_group_snippet_menu(self, menu, group_id):
        """分组右键里的片段入口：保存该组 / 插入片段到该组下。"""
        save_action = menu.addAction("保存该组为片段…")
        save_action.triggered.connect(lambda checked=False, gid=group_id: self.save_group_as_snippet(gid))
        names = sorted(self._snippet_store())
        insert_menu = menu.addMenu("插入片段到该组")
        insert_menu.setEnabled(bool(names))
        for name in names:
            action = insert_menu.addAction(name)
            action.triggered.connect(
                lambda checked=False, snippet_name=name: self.insert_snippet_into_blueprint(
                    snippet_name, None))
        return insert_menu

    # ---------- 其它 ----------

    def _blueprint_status(self, message):
        """蓝图窗口没有日志面板，状态提示写到状态栏；方便用户看到操作结果。"""
        try:
            self.statusBar().showMessage(message, 6000)
        except Exception:
            pass
