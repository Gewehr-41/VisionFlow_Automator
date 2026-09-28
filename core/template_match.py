# 模板匹配工具：加载 PNG 模板并使用 OpenCV 查找屏幕中的目标。
import random
from pathlib import Path

import cv2
import numpy as np

from config import THRESHOLD, USE_MULTI_SCALE, SCALE_RANGE, SCALE_STEP
from diagnostics import warn


class StopExecution(Exception):
    """模板匹配期间收到停止信号时抛出，用于立即中断识别/匹配循环。"""


def load_templates(icon_dir):
    """加载模板图，返回 {模板名: 图像对象}。"""
    templates = {}
    icon_path = Path(icon_dir)
    if not icon_path.exists():
        raise FileNotFoundError(f"图标目录不存在: {icon_dir}")

    for file in sorted(icon_path.glob("*.png")):
        name = file.stem
        # cv2.imread 在 Windows 中文路径下可能无法打开文件。
        file_data = np.fromfile(str(file), dtype=np.uint8)
        img = cv2.imdecode(file_data, cv2.IMREAD_COLOR)
        if img is not None:
            templates[name] = img
        else:
            # 走可见告警通道：启动器是隐藏窗口，裸 print 用户看不到。
            # （此前每加载一张还打印一行"加载模板: …"，绑定图片后每次都刷 60 多行，
            #   在隐藏窗口下毫无意义，已去掉。）
            warn(f"警告: 无法读取模板文件 {file.name}（该模板本次不可用）")

    if not templates:
        raise ValueError(f"没有在 {icon_dir} 中找到模板图，请检查目录和文件名。")

    return templates


def match_all_templates(screen_img, templates, threshold=THRESHOLD,
                        use_multi_scale=USE_MULTI_SCALE,
                        scale_range=SCALE_RANGE,
                        scale_step=SCALE_STEP,
                        search_center=None,
                        search_radius=None,
                        search_rect=None,
                        stop_flag=None):
    """对所有模板做匹配，返回 {模板名: ((x, y), confidence)}。

    支持三种区域限制：
    - search_center + search_radius：只在中心点附近半径内查找；
    - search_rect：只在框选矩形区域内查找；
    - 无限制：全屏扫描。
    如果同一模板在画面中出现多个位置，则随机选一个位置作为命中结果，
    避免相同元素被卡在首个匹配点上。
    """
    match_screen = cv2.cvtColor(screen_img, cv2.COLOR_BGR2GRAY)
    results = {}
    center_x = None
    center_y = None
    radius_sq = None
    rect_left = None
    rect_top = None
    rect_right = None
    rect_bottom = None
    search_offset_x = 0
    search_offset_y = 0

    if search_rect is not None:
        rect_values = tuple(map(int, search_rect))
        if len(rect_values) >= 4:
            rect_left, rect_top, rect_right, rect_bottom = rect_values[:4]
            if rect_right < rect_left:
                rect_left, rect_right = rect_right, rect_left
            if rect_bottom < rect_top:
                rect_top, rect_bottom = rect_bottom, rect_top

            search_offset_x = max(0, rect_left)
            search_offset_y = max(0, rect_top)
            crop_right = min(match_screen.shape[1], rect_right)
            crop_bottom = min(match_screen.shape[0], rect_bottom)
            if crop_right > search_offset_x and crop_bottom > search_offset_y:
                match_screen = match_screen[search_offset_y:crop_bottom, search_offset_x:crop_right]
            else:
                match_screen = match_screen[0:0, 0:0]

    if search_center is not None:
        center_x, center_y = map(int, search_center)
        radius = float(search_radius) if search_radius is not None else 0.0
        radius_sq = radius * radius

    def is_in_search_area(x, y):
        if rect_left is not None and rect_top is not None and rect_right is not None and rect_bottom is not None:
            return rect_left <= x <= rect_right and rect_top <= y <= rect_bottom
        if center_x is None or center_y is None or radius_sq is None:
            return True
        dx = x - center_x
        dy = y - center_y
        return (dx * dx + dy * dy) <= radius_sq

    for name, tpl in templates.items():
        if stop_flag is not None and stop_flag.is_set():
            raise StopExecution()
        match_template = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
        candidates = []
        best_val = -1.0

        if use_multi_scale:
            scales = np.arange(scale_range[0], scale_range[1] + scale_step, scale_step)
            for scale in scales:
                if stop_flag is not None and stop_flag.is_set():
                    raise StopExecution()
                resized = cv2.resize(match_template, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                rh, rw = resized.shape[:2]
                if rh > match_screen.shape[0] or rw > match_screen.shape[1]:
                    continue

                result = cv2.matchTemplate(match_screen, resized, cv2.TM_CCOEFF_NORMED)
                ys, xs = np.where(result >= threshold)
                for y, x in zip(ys, xs):
                    conf = float(result[y, x])
                    center_x_match = x + rw // 2 + search_offset_x
                    center_y_match = y + rh // 2 + search_offset_y
                    if not is_in_search_area(center_x_match, center_y_match):
                        continue
                    candidates.append(((center_x_match, center_y_match), conf))
                    if conf > best_val:
                        best_val = conf

                if not candidates:
                    _, max_val, _, _ = cv2.minMaxLoc(result)
                    if max_val > best_val:
                        best_val = float(max_val)
        else:
            # 单尺度模式：模板比截图还大时 cv2.matchTemplate 会抛异常，
            # 这里与多尺度分支保持一致，跳过该模板而不是让整个识别崩掉。
            template_height, template_width = match_template.shape[:2]
            if template_height > match_screen.shape[0] or template_width > match_screen.shape[1]:
                results[name] = (None, -1.0)
                continue

            result = cv2.matchTemplate(match_screen, match_template, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(result >= threshold)
            for y, x in zip(ys, xs):
                conf = float(result[y, x])
                center_x_match = x + match_template.shape[1] // 2 + search_offset_x
                center_y_match = y + match_template.shape[0] // 2 + search_offset_y
                if not is_in_search_area(center_x_match, center_y_match):
                    continue
                candidates.append(((center_x_match, center_y_match), conf))
                if conf > best_val:
                    best_val = conf

            if not candidates:
                _, best_val, _, _ = cv2.minMaxLoc(result)
                best_val = float(best_val)

        if candidates:
            best_candidate_conf = max(conf for _, conf in candidates)
            best_candidates = [candidate for candidate in candidates if candidate[1] == best_candidate_conf]
            random_center, random_conf = random.choice(best_candidates)
            results[name] = (random_center, float(random_conf))
        else:
            results[name] = (None, float(best_val))

    return results
