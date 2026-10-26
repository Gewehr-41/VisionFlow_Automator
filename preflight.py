# -*- coding: utf-8 -*-
"""执行前体检：开跑前把「必然失败」或「静默不生效」的数据配置查出来。

为什么需要
----------
引擎里已有 `nodes.TaskNode.validate()` / `NodeGraph.validate()`，但这两条只校验
"节点有没有 id/type"和"不能连到自身"。真正会让脚本跑飞或悄悄不生效的问题
（模板文件不存在、`next_appear` 没配 `next_template`、跳转目标越界/指向已禁用步骤）
都发生在执行到那一步才暴露；而打包版是 `-noconsole` 启动，用户看不到任何信息，
只看到"脚本跑飞了"。

本模块只做**纯数据检查**：不截屏、不点击、不建 QWidget，所以能在任意线程与测试里跑。

判据来源（避免第二个真相）
--------------------------
- 跳转字段一律取自 `nodes.CONNECTION_OUTPUT_FIELDS`（蓝图连接 ↔ 任务字段的唯一映射）
- 模板可用性以 `config.ICON_DIR` 下的 `*.png` 文件名为准
- 运行时会执行的步骤 = `enabled` 为真的步骤（与 `selected_tasks()` 一致）

用法
----
    issues = check_tasks(TASKS)
    for issue in issues: ...
    apply_safe_fixes(TASKS)      # 只修复"必定是坏数据"的两类项，返回修复说明
"""
from dataclasses import dataclass
from pathlib import Path

import config
from nodes import CONNECTION_OUTPUT_FIELDS

FATAL = "fatal"       # 会导致执行失败 / 中断
WARNING = "warning"   # 不会崩，但该配置在运行时会被忽略或永远不生效

# 真正依赖模板识别的步骤类型（与 main.execute_task 的分支保持一致）
_RECOGNITION_TYPES = ("normal", "advanced", "click_until_gone")
# 白名单之外还允许"持续点击"用画面变化判据，此时可以没有模板
_TEMPLATE_OPTIONAL_TYPES = ("click_until_gone",)
# 需要 next_template 才有意义的等待方式（引擎会直接 raise）
_WAIT_NEEDS_NEXT = ("next_appear", "change_then_appear")
# 跳转字段：来自唯一映射表，output 是 id 寻址（flow_next），其余是 1-based 步骤号
_JUMP_FIELDS = tuple(
    field for port, field in CONNECTION_OUTPUT_FIELDS.items() if port != "output"
)


@dataclass
class Issue:
    level: str          # FATAL / WARNING
    code: str           # 稳定标识，测试与文案都用它
    step: str           # "步骤 3「描述」" 或 "步骤 3"
    message: str
    fixable: bool = False

    def describe(self):
        return f"[{'必错' if self.level == FATAL else '警告'}] {self.step}：{self.message}"


def _step_label(number, task):
    description = str(task.get("description") or task.get("template") or "未命名步骤")
    return f"步骤 {number}「{description}」"


def _template_names(value):
    """把 template / templates 字段规整成模板名列表（与引擎的解析口径一致）。"""
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    return [item.strip() for item in str(value).replace("，", ",").split(",") if item.strip()]


def _available_templates(icon_dir):
    try:
        return {path.stem for path in Path(icon_dir).glob("*.png")}
    except OSError:
        return set()


def _detect_screen_size():
    """当前监视器尺寸；探测失败返回 None（此时跳过坐标越界检查）。"""
    try:
        import mss

        with mss.mss() as sct:
            monitor = sct.monitors[config.MONITOR_INDEX]
            return int(monitor["width"]), int(monitor["height"])
    except Exception:
        return None


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def check_tasks(tasks, icon_dir=None, screen_size=None):
    """返回问题列表（按步骤顺序）；空列表表示"没查出问题"。

    `tasks` 传**完整任务列表**（含禁用步骤）：跳转目标是否"指向已禁用步骤"要靠它判断；
    只有 `enabled` 为真的步骤才会做"执行期"检查。
    """
    tasks = list(tasks or [])
    icon_dir = config.ICON_DIR if icon_dir is None else icon_dir
    if screen_size is None:
        screen_size = _detect_screen_size()
    available = _available_templates(icon_dir)
    issues = []

    runnable = [
        (position + 1, task)
        for position, task in enumerate(tasks)
        if isinstance(task, dict) and task.get("enabled", True)
    ]
    runnable_numbers = {number for number, _ in runnable}

    # 重复 id：graph.id_to_index 会互相覆盖，flow_next 可能指到错的步骤
    seen_ids = {}
    for number, task in runnable:
        task_id = str(task.get("id") or "")
        if task_id and task_id in seen_ids:
            issues.append(Issue(WARNING, "duplicate_id", _step_label(number, task),
                                f"id 与「步骤 {seen_ids[task_id]}」重复，蓝图连线可能连到错误步骤"))
        elif task_id:
            seen_ids[task_id] = number
    runnable_ids = {str(task.get("id")) for _, task in runnable if task.get("id")}

    for number, task in runnable:
        step = _step_label(number, task)
        task_type = str(task.get("type", "normal"))

        # ---------- 模板 ----------
        if task_type in _RECOGNITION_TYPES:
            names = _template_names(task.get("templates") or task.get("template"))
            # 「持续点击」勾了"画面变化视为成功"时，引擎允许不配模板（见 execute_click_until_gone_task）
            allow_untemplated = task_type in _TEMPLATE_OPTIONAL_TYPES and bool(task.get("stop_on_change", False))
            if not names and not allow_untemplated:
                issues.append(Issue(FATAL, "template_unset", step,
                                    "没有配置任何模板图片，引擎会直接报错「步骤至少需要配置一张绑定图片」"))
            for name in names:
                if name in ("None", "null", "new_step"):
                    issues.append(Issue(WARNING, "template_placeholder", step,
                                        f"模板名是占位值 '{name}'，识别永远不会命中"))
                elif name not in available:
                    issues.append(Issue(WARNING, "template_missing", step,
                                        f"模板 '{name}' 在图标目录里找不到（{Path(icon_dir) / (name + '.png')}）"))

        # ---------- 等待方式与下一模板 ----------
        wait_for = str(task.get("wait_for", "time"))
        next_template = task.get("next_template")
        if wait_for in _WAIT_NEEDS_NEXT:
            if not next_template or not str(next_template).strip():
                issues.append(Issue(FATAL, "next_template_missing", step,
                                    f"等待方式为 '{wait_for}'，但没配「下一模板」，引擎会直接报错"))
            elif str(next_template).strip() in ("None", "null"):
                issues.append(Issue(WARNING, "next_template_placeholder", step,
                                    f"「下一模板」是占位值 '{next_template}'，等待必然超时", fixable=True))
            elif str(next_template).strip() not in available:
                issues.append(Issue(WARNING, "next_template_missing_file", step,
                                    f"「下一模板」'{next_template}' 在图标目录里找不到"))

        # ---------- 跳转目标 ----------
        for field_name in _JUMP_FIELDS:
            raw = task.get(field_name)
            if raw is None:
                continue
            target = _to_int(raw)
            if target is None:
                issues.append(Issue(WARNING, "jump_not_a_number", step,
                                    f"跳转字段 {field_name} 的值 {raw!r} 不是数字，运行时会被忽略"))
                continue
            if target == number:
                issues.append(Issue(WARNING, "jump_to_self", step,
                                    f"{field_name} 指向自己，运行时会为避免死循环而忽略"))
            elif target < 1 or target > len(tasks):
                issues.append(Issue(WARNING, "jump_out_of_range", step,
                                    f"{field_name} 指向步骤 {target}，超出列表范围（1~{len(tasks)}），运行时会被忽略"))
            elif target not in runnable_numbers:
                issues.append(Issue(WARNING, "jump_to_disabled", step,
                                    f"{field_name} 指向步骤 {target}，但该步骤已禁用，运行时会被忽略"))

        cases = task.get("switch_cases")
        for case_value, case_target in (cases.items() if isinstance(cases, dict) else ()):
            target = _to_int(case_target)
            if target is not None and target not in runnable_numbers:
                issues.append(Issue(WARNING, "jump_to_disabled", step,
                                    f"switch_cases['{case_value}'] 指向步骤 {target}，但该步骤未启用或不存在"))

        # ---------- 蓝图连线（id 寻址） ----------
        flow_next = task.get("flow_next")
        if flow_next is not None and str(flow_next) not in runnable_ids:
            issues.append(Issue(WARNING, "flow_next_missing", step,
                                "flow_next 指向的步骤不在本次要执行的列表里，连线不会生效", fixable=True))

        # ---------- 点击坐标 ----------
        click_x, click_y = task.get("click_x"), task.get("click_y")
        if click_x is not None and click_y is not None:
            x, y = _to_int(click_x), _to_int(click_y)
            if x is None or y is None:
                issues.append(Issue(WARNING, "coord_not_a_number", step,
                                    f"点击坐标 {click_x!r},{click_y!r} 不是数字"))
            elif screen_size is not None:
                width, height = screen_size
                if x < 0 or y < 0 or x >= width or y >= height:
                    issues.append(Issue(WARNING, "coord_out_of_screen", step,
                                        f"点击坐标 ({x}, {y}) 超出当前屏幕 {width}×{height}，"
                                        "窗口移动或换了分辨率后可能点空"))

        # ---------- 识别区域 ----------
        for rect in (task.get("match_rects") or []):
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                issues.append(Issue(WARNING, "match_rect_invalid", step,
                                    f"识别区域 {rect!r} 不是 [left, top, right, bottom] 四元组"))
                continue
            left, top, right, bottom = (_to_int(value) for value in rect)
            if None in (left, top, right, bottom) or left >= right or top >= bottom:
                issues.append(Issue(WARNING, "match_rect_invalid", step,
                                    f"识别区域 {list(rect)!r} 边界非法（要求 left<right 且 top<bottom）"))

    return issues


def apply_safe_fixes(tasks, icon_dir=None):
    """只修复"必定是坏数据"的两类项，返回修复说明列表。

    刻意保守：不猜用户意图、不删除步骤、不改坐标。
      1. `wait_for` 需要下一模板，但 `next_template` 是占位值 'None'/'' → 清掉并改回按时间等待
      2. `flow_next` 指向不存在的步骤 → 清掉连线并标记 flow_next_disabled（与画布删除连线同义）
    """
    fixes = []
    tasks = [task for task in (tasks or []) if isinstance(task, dict)]
    for position, task in enumerate(tasks):
        number = position + 1
        step = _step_label(number, task)
        next_template = task.get("next_template")
        if (str(task.get("wait_for", "time")) in _WAIT_NEEDS_NEXT
                and next_template is not None
                and str(next_template).strip() in ("None", "null", "")):
            task.pop("next_template", None)
            task.pop("next_templates", None)
            task["wait_for"] = "time"
            fixes.append(f"{step}：清掉占位的「下一模板」，等待方式改回按时间等待")

    runnable_ids = {str(task.get("id")) for task in tasks if task.get("id")}
    for position, task in enumerate(tasks):
        number = position + 1
        flow_next = task.get("flow_next")
        if flow_next is not None and str(flow_next) not in runnable_ids:
            step = _step_label(number, task)
            task.pop("flow_next", None)
            task["flow_next_disabled"] = True
            fixes.append(f"{step}：清掉指向不存在步骤的连线（flow_next）")
    return fixes
