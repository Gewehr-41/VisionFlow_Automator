# -*- coding: utf-8 -*-
"""蓝图窗口的分组：渲染组框、增删改、收拢/展开、菜单、组索引与元数据。

从 `gui_pyside6.py` 的 `BlueprintWindow` 整体外移出来的内聚职责块（23 个方法）。
窗口类通过 `BlueprintGroupMixin` 混入，`self.add_group(...)` 之类的调用方式不变。

与 `CaptureOverlayMixin` / `BlueprintInteractionMixin` **没有同名方法**，
所以 MRO 上放在它们之前即可；行为等价性由分组操作基线保证。
"""
import uuid

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog, QInputDialog, QMenu, QMessageBox

from blueprint_canvas import BlueprintGroupItem, BlueprintNodeItem


class BlueprintGroupMixin:
    """分组相关方法；依赖宿主窗口提供 scene / tasks / group_metadata / layout_data 等。"""

    def _group_metadata_for_current_mode(self):
        return self.group_metadata

    def _group_default_color(self):
        return "#38bdf8"

    def _group_color_button_text_color(self):
        return "#ffffff"

    def _group_color_dialog_title(self):
        return "选择组颜色"

    def _group_hide_editor_groups(self):
        self._clear_special_form()
        self.click_until_group.setVisible(False)

    def _group_after_editor_applied(self, group_id, name):
        self._push_history()
        self.refresh()
        self.save_layout()

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

    def toggle_group(self, group_id):
        self._push_history()
        if group_id in self.collapsed_groups:
            self.collapsed_groups.remove(group_id)
        else:
            self.collapsed_groups.add(group_id)
        self.refresh()

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
