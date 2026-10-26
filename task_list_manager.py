# -*- coding: utf-8 -*-
"""任务列表：渲染、选中、启用切换、复制/粘贴、删除、移动顺序。

从 `gui_pyside6.py` 的 `PySide6ScriptWindow` 整体外移出来的内聚职责块（13 个方法）。
窗口类通过 `TaskListMixin` 混入，`self.refresh_task_list()` 之类的既有调用方式不变。

注意：`_capture_task_list()` 是 `CaptureOverlayMixin` 要求的访问器钩子，
放在这里是为了让"任务列表"这一块的实现集中；MRO 上必须在 `CaptureOverlayMixin`
之前，否则会被那里的空实现盖住（窗口类声明已按此顺序排列）。
"""
import uuid
from copy import deepcopy

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMessageBox, QTreeWidgetItem

from nodes import detour_active, remap_jump_targets
from tasks import TASKS
from ui_common import _jump_index_map


class TaskListMixin:
    """任务列表相关方法；依赖宿主窗口提供 task_list / mode_tasks / append_log 等。"""

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
            detour_status = " · 已启用迂回" if detour_active(task) else ""
            ocr_status = " · 文字识别" if task.get("ocr_enabled") else ""
            item = QTreeWidgetItem([f"{index + 1:02d}. [{task_type}] {description}{ocr_status}{detour_status}"])
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

        # 按元数据顺序重排组（上移/下移、拖拽改变父子关系后界面要跟着变）
        self._apply_group_meta_order(group_metadata)

        self.task_list.blockSignals(False)
        if TASKS:
            target_index = min(max(self.current_task_index, 0), len(TASKS) - 1)
            self.select_task_index(target_index)
        else:
            self.clear_editor()

    def select_task_index(self, index):
        item = self._find_task_item(index)
        if item is None:
            return
        # QTreeView 为了让"当前项"可见，会在 setCurrentItem 内部调用 scrollTo，
        # 把**被折叠的祖先组展开**。这有两个后果：
        #   ① 用户手动折叠的组会自己弹开（所有者 2026-10-26 反馈：点第 4 组里的步骤，
        #      第 1 组每点一次就自动展开）；
        #   ② 这次展开会触发 itemExpanded → 把"展开"写回分组元数据并落盘，
        #      也就是折叠状态被**真的改坏**了，不只是看起来变了。
        # 所以选中前后各记一次展开状态：期间禁止写回元数据，选完按快照恢复。
        snapshot = self._snapshot_group_expansion()
        self._suppress_group_expand_sync = True
        try:
            self.task_list.setCurrentItem(item)
        finally:
            self._suppress_group_expand_sync = False
        self._restore_group_expansion(snapshot)

    def _apply_group_meta_order(self, group_metadata):
        """按元数据把「组」排成 order / children 里的顺序。

        主界面的树按创建顺序拼出来，分组上移/下移、拖拽改变父子之后必须按元数据重排，
        否则界面顺序和保存的数据对不上。只重排**组项**，步骤项的相对位置不动。
        """
        if not isinstance(group_metadata, dict):
            return
        order = [str(gid) for gid in (group_metadata.get("order") or [])]
        children = group_metadata.get("children") or {}

        def is_tree(container):
            return hasattr(container, "topLevelItemCount")

        def level_count(container):
            return container.topLevelItemCount() if is_tree(container) else container.childCount()

        def level_item(container, index):
            return container.topLevelItem(index) if is_tree(container) else container.child(index)

        def level_take(container, index):
            return container.takeTopLevelItem(index) if is_tree(container) else container.takeChild(index)

        def level_insert(container, index, item):
            if is_tree(container):
                container.insertTopLevelItem(index, item)
            else:
                container.insertChild(index, item)

        def sort_level(container, ranked):
            ranked_index = {group_id: position for position, group_id in enumerate(ranked)}
            items = [level_item(container, index) for index in range(level_count(container))]
            group_positions = [index for index, item in enumerate(items)
                               if item.data(0, Qt.UserRole) == "group"]
            if len(group_positions) < 2:
                return
            group_items = [items[index] for index in group_positions]
            current_ids = [str(item.data(0, Qt.UserRole + 1)) for item in group_items]
            desired = [gid for gid in ranked if gid in current_ids]
            desired += [gid for gid in current_ids if gid not in desired]
            if current_ids == desired:
                # 已经有序就不要 take/insert：从树里取出再放回会把展开状态弄乱
                return
            expanded_state = {gid: item.isExpanded() for gid, item in zip(current_ids, group_items)}
            for position in reversed(group_positions):
                level_take(container, position)
            ordered_items = [group_items[current_ids.index(gid)] for gid in desired]
            for position, item in zip(group_positions, ordered_items):
                level_insert(container, position, item)
            # 重排后恢复每组的展开/折叠状态（setExpanded 期间信号已被 refresh 阻塞）
            for position in group_positions:
                item = level_item(container, position)
                gid = str(item.data(0, Qt.UserRole + 1))
                if gid in expanded_state:
                    item.setExpanded(expanded_state[gid])

        sort_level(self.task_list, order)
        for group_id, item in self._group_items().items():
            if item is not None:
                sort_level(item, [str(child) for child in (children.get(group_id) or [])])

    def _group_items(self):
        """当前树里所有组项：group_id -> QTreeWidgetItem（含嵌套）。"""
        found = {}
        stack = [self.task_list.topLevelItem(i) for i in range(self.task_list.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            if item.data(0, Qt.UserRole) == "group":
                found[str(item.data(0, Qt.UserRole + 1))] = item
            stack.extend(item.child(i) for i in range(item.childCount()))
        return found

    def _snapshot_group_expansion(self):
        return {group_id: item.isExpanded() for group_id, item in self._group_items().items()}

    def _restore_group_expansion(self, snapshot):
        """按快照恢复折叠状态（不写回元数据）；返回是否真的改过。"""
        if not snapshot:
            return False
        changed = False
        was_blocked = self.task_list.blockSignals(True)
        try:
            for group_id, item in self._group_items().items():
                want = snapshot.get(group_id)
                if want is not None and item.isExpanded() != want:
                    item.setExpanded(want)
                    changed = True
        finally:
            self.task_list.blockSignals(was_blocked)
        return changed

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

    def _capture_task_list(self):
        return TASKS
    @Slot()
    def copy_selected_item(self):
        """复制选中的步骤 / 分组。

        - 多选步骤：全部复制（此前只复制最后一个）
        - 选中分组：组内全部步骤（含子组）+ 分组结构一起复制，生成新组 id、名称加「副本」，
          嵌套关系在副本内部保持，父组在复制范围外时副本落到根层
        - 复制范围内部的跳转改指向副本，指向范围外的跳转清除
        """
        indices = self._selected_task_indices()
        group_ids = self._selected_group_ids()
        if not indices and not group_ids:
            return
        mode = self.mode_combo.currentText() or "custom"
        metadata = self.mode_group_metadata.setdefault(mode, {})
        names = metadata.setdefault("names", {})
        colors = metadata.setdefault("colors", {})
        parents = metadata.setdefault("parents", {})
        children = metadata.setdefault("children", {})
        order = metadata.setdefault("order", [])

        affected_groups = []
        for group_id in group_ids:
            for child in self._group_descendants(metadata, group_id):
                if str(child) not in affected_groups:
                    affected_groups.append(str(child))
        source_indices = set(indices)
        for index, task in enumerate(TASKS):
            if str(task.get("group_id") or "group_default") in affected_groups:
                source_indices.add(index)
        ordered = sorted(source_indices)
        if not ordered:
            return

        copies = []
        id_map = {}
        index_map = {}
        for position, index in enumerate(ordered):
            cloned = deepcopy(TASKS[index])
            old_id = str(cloned.get("id"))
            new_id = str(uuid.uuid4())
            id_map[old_id] = new_id
            index_map[index] = position
            cloned["id"] = new_id
            cloned["description"] = f"{cloned.get('description', '步骤')} 副本"
            copies.append(cloned)

        group_id_map = {group_id: f"group_{uuid.uuid4().hex[:8]}" for group_id in affected_groups}
        for cloned in copies:
            old_group = str(cloned.get("group_id") or "group_default")
            new_group = group_id_map.get(old_group)
            if new_group:
                cloned["group_id"] = new_group
                cloned["group_name"] = ""       # 建好组元数据后再填名字
        # 复制范围内部的 flow_next / 跳转到副本，范围外的清掉
        for cloned in copies:
            flow_target = cloned.get("flow_next")
            if flow_target is not None:
                mapped = id_map.get(str(flow_target))
                if mapped is None:
                    cloned.pop("flow_next", None)
                else:
                    cloned["flow_next"] = mapped
        remap_jump_targets(copies, index_map)

        insert_at = ordered[-1] + 1
        # 插入点之后现有步骤的跳转整体后移
        remap_jump_targets(TASKS, {
            old_index: (old_index + len(copies) if old_index >= insert_at else old_index)
            for old_index in range(len(TASKS))
        })
        TASKS[insert_at:insert_at] = copies

        # 复制分组的元数据（父组在复制范围外 → 副本落到根层）
        for old_group, new_group in group_id_map.items():
            names[new_group] = f"{names.get(old_group, old_group)} 副本"
            colors[new_group] = str(colors.get(old_group) or self._group_default_color())
            old_parent = parents.get(old_group)
            new_parent = group_id_map.get(str(old_parent)) if old_parent else None
            parents[new_group] = new_parent
            children.setdefault(new_group, [])
            if new_parent:
                children.setdefault(new_parent, [])
                if new_group not in children[new_parent]:
                    children[new_parent].append(new_group)
            else:
                order.append(new_group)
        for cloned in copies:
            new_group = cloned.get("group_id")
            if new_group in names:
                cloned["group_name"] = names[new_group]

        if group_id_map:
            self._save_presets()
        self.current_task_index = insert_at
        self.save_current_tasks()
        self.refresh_task_list()
        self.select_task_index(insert_at)
        group_note = f"，{len(group_id_map)} 个分组" if group_id_map else ""
        self.append_log(f"已复制 {len(copies)} 个步骤{group_note}。")
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
