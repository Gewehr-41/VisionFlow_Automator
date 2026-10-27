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


def group_inside_focus(group_id, focus_group, parents):
    """该组是否位于当前聚焦的子图内部（含聚焦组自己）。纯函数，便于单测。"""
    if not focus_group:
        return False
    current = str(group_id)
    seen = set()
    while current and current not in seen:
        if current == str(focus_group):
            return True
        seen.add(current)
        parent = parents.get(current)
        current = str(parent) if parent else ""
    return False


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

    def _group_box_rect(self, group_id):
        """组在画布上的包围盒（**按折叠后的实际呈现**计算）。

        关键点：折叠的子组只占一张 COLLAPSED_WIDTH×COLLAPSED_HEIGHT 的卡片，
        不能再用它里面隐藏节点的最大/最小坐标撑开父组——否则父组/根组会空出一大片。
        递归规则：折叠组 → 卡片矩形；展开组 → 自己直属节点的范围 ∪ 各子组的包围盒。
        """
        group_id = str(group_id)
        if group_id == "group_default":
            return None
        children = self._group_children()
        group_positions = self.layout_data.get("group_positions", {})
        if not isinstance(group_positions, dict):
            group_positions = {}
        focus = getattr(self, "focus_group", None)
        collapsed = self.collapsed_groups
        cache = {}

        def empty_group_rect(gid):
            pos = group_positions.get(str(gid))
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                x, y = float(pos[0]), float(pos[1])
            else:
                x, y = 80.0, 80.0 + 40.0 * len(group_positions)
            return (x, y, x + 220, y + 18)

        def card_rect(gid):
            """折叠组的卡片矩形：锚在组内节点的左上角，固定卡片尺寸。"""
            indices = self._group_all_node_indices(gid)
            nodes = [self._node_items[i] for i in indices if i in self._node_items]
            if not nodes:
                return empty_group_rect(gid)
            left = min(node.x() for node in nodes) - 18
            top = min(node.y() for node in nodes) - 36
            return (left, top,
                    left + BlueprintGroupItem.COLLAPSED_WIDTH,
                    top + BlueprintGroupItem.COLLAPSED_HEIGHT)

        def rect_for(gid):
            gid = str(gid)
            if gid in cache:
                return cache[gid]
            is_focus = bool(focus) and gid == str(focus)
            if gid in collapsed and not is_focus:
                rect = card_rect(gid)
            else:
                boxes = []
                for index in self._group_node_indices(gid):
                    node = self._node_items.get(index)
                    if node is None:
                        continue
                    boxes.append((node.x() - 18, node.y() - 36,
                                  node.x() + node.rect().width() + 18,
                                  node.y() + node.rect().height() + 18))
                for child in children.get(gid, []):
                    child_rect = rect_for(child)
                    if child_rect is not None:
                        boxes.append(child_rect)
                if not boxes:
                    rect = empty_group_rect(gid)
                else:
                    rect = (min(box[0] for box in boxes), min(box[1] for box in boxes),
                            max(box[2] for box in boxes), max(box[3] for box in boxes))
            cache[gid] = rect
            return rect

        return rect_for(group_id)

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
            # 统一由 _group_box_rect 计算：折叠的子组只占卡片大小，
            # 父组不会再被隐藏节点的最大坐标撑开到"空出一大片"
            return self._group_box_rect(group_id)

        def has_collapsed_ancestor(group_id):
            parents = self._group_parents()
            current = parents.get(str(group_id))
            seen = set()
            while current and str(current) not in seen and str(current) != "group_default":
                seen.add(str(current))
                if str(current) in self.collapsed_groups:
                    return True
                current = parents.get(str(current))
            return False

        focus = getattr(self, "focus_group", None)
        parents_map = self._group_parents()

        def inside_focus(group_id):
            return group_inside_focus(group_id, focus, parents_map)

        def render(group_id):
            group_id = str(group_id)
            if group_id == "group_default" or group_id in rendered:
                return
            if has_collapsed_ancestor(group_id) and not inside_focus(group_id):
                # 上级组已收起成"群组节点"时，子组框不该再画出来（否则根层会多一个空框）；
                # 但聚焦在这个折叠组内部时必须渲染，否则"看不到组内的组"。
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
                box.collapsed = str(group_id) in self.collapsed_groups
                box.set_release_callback(self.save_layout)
                box.set_move_callback(self._on_empty_group_moved)
                self.scene.addItem(box)
                self._group_items[group_id] = box
            for child in children.get(group_id, []):
                render(child)

        for group_id in order:
            render(group_id)
        known = {str(task.get("group_id") or "group_default") for task in self.tasks}
        for group_id in known:
            render(group_id)

    # ---------- 子图聚焦（F14）：进入某个组内部，只看它（含子组）的节点 ----------

    def _focus_ancestors(self, group_id):
        """返回 [自己, 父组, 祖父组…]（不含隐式根层「默认分组」）。"""
        parents = self._group_parents()
        chain = []
        current = str(group_id)
        seen = set()
        while current and current != "group_default" and current not in seen:
            seen.add(current)
            chain.append(current)
            parent = parents.get(current)
            current = str(parent) if parent else None
        return chain

    def focus_path(self):
        """当前聚焦的路径（根 → 自己）。未聚焦时返回空列表。"""
        focus = getattr(self, "focus_group", None)
        if not focus:
            return []
        return list(reversed(self._focus_ancestors(focus)))

    def _focus_visible_indices(self):
        """聚焦时可见的节点下标集合；None 表示"未聚焦，全部可见"。"""
        focus = getattr(self, "focus_group", None)
        if not focus:
            return None
        return set(self._group_all_node_indices(focus))

    def enter_group(self, group_id):
        """进入某个组内部（子图视图）。"""
        if group_id is None:
            self.focus_root()
            return
        group_id = str(group_id)
        if group_id == "group_default":
            self.focus_root()
            return
        if getattr(self, "focus_group", None) == group_id:
            return
        self.focus_group = group_id
        self.refresh()

    def leave_group(self):
        """回到上一级（父组；已在根层则不动）。"""
        focus = getattr(self, "focus_group", None)
        if not focus:
            return
        chain = self._focus_ancestors(focus)
        self.focus_group = chain[1] if len(chain) > 1 else None
        self.refresh()

    def focus_root(self):
        if getattr(self, "focus_group", None) is None:
            return
        self.focus_group = None
        self.refresh()

    def _update_breadcrumb(self):
        """更新面包屑文字与按钮可用状态（子图视图的导航条）。"""
        if getattr(self, "breadcrumb_bar", None) is None:
            return
        focus = getattr(self, "focus_group", None)
        self.back_button.setEnabled(bool(focus))
        self.root_button.setEnabled(bool(focus))
        if not focus:
            self.breadcrumb_label.setText("视图：根层（所有步骤）")
            return
        names = self._group_names()
        path = self.focus_path()
        crumbs = " › ".join(str(names.get(group_id, group_id)) for group_id in path) or str(focus)
        count = len(self._group_all_node_indices(focus))
        self.breadcrumb_label.setText(f"视图：根层 › {crumbs}（{count} 步）")

    def _collapsed_group_for(self, index):
        task = self.tasks[index]
        group_id = str(task.get("group_id") or "group_default")
        parents = self._group_parents()
        focus = getattr(self, "focus_group", None)
        current = group_id
        seen = set()
        while current and current != "group_default" and current not in seen:
            seen.add(current)
            if focus and str(current) == str(focus):
                return None
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
            # 与 _render_groups 同一套算法：折叠的子组按卡片大小计入父组范围
            bounds = self._group_box_rect(group_id)
            if bounds is None:
                continue
            left, top, right, bottom = bounds
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
        enter_action = menu.addAction("进入该组内部（子图）")
        enter_action.setEnabled(getattr(self, "focus_group", None) != group_id)
        enter_action.triggered.connect(lambda: self.enter_group(group_id))
        toggle_action = menu.addAction("收起组" if group_id not in self.collapsed_groups else "展开组")
        edit_action = menu.addAction("编辑组设置")
        add_task_action = menu.addAction("新建步骤到此组")
        delete_action = menu.addAction("删除组")
        add_group_action.triggered.connect(lambda: self.add_group(group_id, (scene_position.x(), scene_position.y())))
        toggle_action.triggered.connect(lambda: self.toggle_group(group_id))
        edit_action.triggered.connect(lambda: self._edit_group(group_id))
        add_task_action.triggered.connect(lambda: self.add_task(group_id, (scene_position.x(), scene_position.y())))
        delete_action.triggered.connect(lambda: self._delete_group(group_id))
        if hasattr(self, "_add_group_snippet_menu"):
            menu.addSeparator()
            self._add_group_snippet_menu(menu, group_id)
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
        # 删掉的正是当前聚焦的子图（或它的祖先）时，回根层，避免聚焦到一个不存在的组
        focus = getattr(self, "focus_group", None)
        if focus:
            doomed = {str(group_id)} | {str(g) for g in self._group_descendants(group_id)}
            if str(focus) in doomed:
                self.focus_group = None
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
