# -*- coding: utf-8 -*-
"""模板匹配工具：读取 PNG 模板并在画面中查找目标。

识别增强（2026-10-28）：
1. 多尺度模板缓存：同一模板的灰度和各缩放版本只计算一次；
2. 上次命中位置优先：先在上次位置附近找，未命中再退回全屏；
3. 透明模板 alpha 掩膜 + 命中后颜色二次校验，减少误匹配；
4. 候选去重 + 确定性选择（不再同分随机），配合执行层的多帧确认；
5. 模板匹配失败时可用 ORB 特征匹配兜底（缩放/分辨率变化）。
"""
import random
from pathlib import Path

import cv2
import numpy as np

import config
from config import THRESHOLD, USE_MULTI_SCALE, SCALE_RANGE, SCALE_STEP
from diagnostics import warn


class StopExecution(Exception):
    """模板匹配期间收到停止信号时抛出，用于中止识别/匹配循环。"""


# 缓存：id(模板数组) -> 灰度图 / 缩放版本 / ORB 特征
_GRAY_CACHE = {}
_SCALED_CACHE = {}
_ORB_CACHE = {}
# 最近一次命中位置：模板名 -> (x, y)。用于「上次位置优先搜索」。
_LAST_HITS = {}
_MASK_ALPHA_THRESHOLD = 8


def clear_template_cache():
    """模板重新加载/绑定后清空缓存。"""
    _GRAY_CACHE.clear()
    _SCALED_CACHE.clear()
    _ORB_CACHE.clear()
    _LAST_HITS.clear()


def load_templates(icon_dir):
    """读取模板图，返回 {模板名: 图像数组}。

    使用 IMREAD_UNCHANGED 以便保留透明 PNG 的 alpha 通道（用于掩膜匹配）；
    普通截图仍是 3 通道，行为不变。
    """
    templates = {}
    icon_path = Path(icon_dir)
    if not icon_path.exists():
        warn(f"警告: 图标目录不存在: {icon_dir}"
             "（识别功能暂时不可用，界面仍可正常打开）")
        return templates

    for file in sorted(icon_path.glob("*.png")):
        name = file.stem
        # cv2.imread 在 Windows 中文路径下可能无法读取文件
        file_data = np.fromfile(str(file), dtype=np.uint8)
        img = cv2.imdecode(file_data, cv2.IMREAD_UNCHANGED)
        if img is None:
            warn(f"警告: 无法读取模板文件 {file.name}（该模板本次不可用）")
            continue
        if img.ndim == 3 and img.shape[2] == 4:
            templates[name] = img                    # BGRA
        elif img.ndim == 3 and img.shape[2] == 3:
            templates[name] = img
        elif img.ndim == 2:
            templates[name] = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        else:
            warn(f"警告: 无法读取模板文件 {file.name}（不支持的通道数）")

    clear_template_cache()
    if not templates:
        warn(f"警告: {icon_dir} 里没有可用的模板图片（PNG）。"
             "（识别功能暂时不可用，界面仍可正常打开）")
    return templates


def _split_template(tpl):
    """模板 → (BGR, 灰度, alpha 掩膜或 None)。"""
    cache_key = (id(tpl), tpl.shape)
    cached = _GRAY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if tpl.ndim == 2:
        bgr = cv2.cvtColor(tpl, cv2.COLOR_GRAY2BGR)
        gray = tpl
        mask = None
    elif tpl.shape[2] == 4:
        bgr = tpl[:, :, :3]
        alpha = tpl[:, :, 3]
        mask = np.where(alpha > _MASK_ALPHA_THRESHOLD, 255, 0).astype(np.uint8)
        if int(mask.min()) == 255:
            mask = None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    else:
        bgr = tpl
        gray = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
        mask = None
    result = (bgr, gray, mask)
    _GRAY_CACHE[cache_key] = result
    return result


def _scaled_template(tpl, scale):
    """按比例缩放模板（灰/彩色/掩膜），带缓存。"""
    cache_key = (id(tpl), tpl.shape, round(float(scale), 4))
    cached = _SCALED_CACHE.get(cache_key)
    if cached is not None:
        return cached
    _bgr, gray, mask = _split_template(tpl)
    if abs(scale - 1.0) < 1e-6:
        result = (gray, mask)
    else:
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        scaled_gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interpolation)
        scaled_mask = None
        if mask is not None:
            scaled_mask = cv2.resize(mask, (scaled_gray.shape[1], scaled_gray.shape[0]),
                                     interpolation=cv2.INTER_NEAREST)
        result = (scaled_gray, scaled_mask)
    _SCALED_CACHE[cache_key] = result
    return result


def _color_distance(tpl_bgr, tpl_mask, patch_bgr, patch_mask=None):
    """模板与命中区域的平均颜色差（0~255，越小越像）。"""
    if patch_bgr is None or patch_bgr.size == 0:
        return 255.0
    try:
        tpl_mean = cv2.mean(tpl_bgr, mask=tpl_mask)[:3]
        patch_mean = cv2.mean(patch_bgr, mask=patch_mask)[:3]
    except cv2.error:
        return 0.0
    diff = np.sqrt(np.mean((np.array(tpl_mean) - np.array(patch_mean)) ** 2))
    return float(diff)


def _color_check(tpl_bgr, tpl_mask, screen_bgr, left, top, width, height):
    tolerance = float(getattr(config, "COLOR_CHECK_TOLERANCE", 48.0))
    if tolerance <= 0:
        return True
    patch = screen_bgr[top:top + height, left:left + width]
    patch_mask = tpl_mask
    if patch_mask is not None and patch_mask.shape[:2] != patch.shape[:2]:
        patch_mask = None
    return _color_distance(tpl_bgr, tpl_mask, patch, patch_mask) <= tolerance


def _crop_screen(screen_bgr, rect):
    """按 (left, top, right, bottom) 裁剪；返回 (裁剪, 偏移, 有效)。"""
    height, width = screen_bgr.shape[:2]
    left, top, right, bottom = (int(v) for v in rect)
    left, right = max(0, min(left, right)), min(width, max(left, right))
    top, bottom = max(0, min(top, bottom)), min(height, max(top, bottom))
    if right - left < 2 or bottom - top < 2:
        return None, (0, 0)
    return screen_bgr[top:bottom, left:right], (left, top)


def _match_one_region(screen_bgr, screen_gray, tpl, name, scales, threshold,
                      offset_x, offset_y, is_in_search_area, allow_mask=True):
    """在给定区域里匹配单个模板；返回 (candidates, best_score)。"""
    bgr, _gray, mask = _split_template(tpl)
    candidates = []
    best_val = -1.0
    use_mask = bool(mask is not None and allow_mask and getattr(config, "USE_TEMPLATE_MASK", True))
    passes = [True, False] if use_mask else [False]

    for masked in passes:
        for scale in scales:
            if masked and scale != scales[0]:
                continue          # 掩膜方式只跑一个代表尺度，避免翻倍；后续有兜底
            scaled_gray, scaled_mask = _scaled_template(tpl, scale)
            height, width = scaled_gray.shape[:2]
            if height > screen_gray.shape[0] or width > screen_gray.shape[1]:
                continue
            if masked and scaled_mask is not None:
                result = cv2.matchTemplate(screen_gray, scaled_gray, cv2.TM_SQDIFF_NORMED,
                                           mask=scaled_mask)
                scores = 1.0 - result
            else:
                result = cv2.matchTemplate(screen_gray, scaled_gray, cv2.TM_CCOEFF_NORMED)
                scores = result
            ys, xs = np.where(scores >= threshold)
            for y, x in zip(ys, xs):
                conf = float(scores[y, x])
                left = int(x + offset_x)
                top = int(y + offset_y)
                center_x = left + width // 2
                center_y = top + height // 2
                if not is_in_search_area(center_x, center_y):
                    continue
                if getattr(config, "COLOR_CHECK_ENABLED", True):
                    # 颜色校验用**裁剪区域内的相对坐标**（screen_bgr 是裁剪后的图，不是整屏）；
                    # 之前传绝对坐标，在「上次位置优先」的小窗口里会取错 patch，导致把正确命中拒掉。
                    if not _color_check(bgr, scaled_mask, screen_bgr, x, y, width, height):
                        continue
                candidates.append(((center_x, center_y), conf))
                best_val = max(best_val, conf)
            if not candidates:
                _, max_val, _, _ = cv2.minMaxLoc(scores)
                best_val = max(best_val, float(max_val))
        if candidates:
            break
    return candidates, best_val


def _dedupe_candidates(candidates):
    """去重：同分/相邻结果只保留最好的一个（确定性选择，不再随机）。"""
    ordered = sorted(candidates, key=lambda item: (-item[1], item[0][1], item[0][0]))
    kept = []
    for center, conf in ordered:
        if any(abs(center[0] - other[0]) <= 2 and abs(center[1] - other[1]) <= 2
               for other, _ in kept):
            continue
        kept.append((center, conf))
    return kept


def _orb_features(tpl_bgr, scale=1.0):
    cache_key = (id(tpl_bgr), tpl_bgr.shape, round(float(scale), 3))
    cached = _ORB_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if abs(scale - 1.0) > 1e-6:
        tpl_bgr = cv2.resize(tpl_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY) if tpl_bgr.ndim == 3 else tpl_bgr
    orb = cv2.ORB_create(nfeatures=700)
    keypoints, descriptors = orb.detectAndCompute(gray, None)
    if descriptors is None or len(keypoints) < 4:
        result = (None, None, None)
    else:
        result = (orb, keypoints, descriptors)
    _ORB_CACHE[cache_key] = result
    return result


def feature_match_templates(screen_img, templates, threshold=0.0, min_matches=None,
                            region=None, stop_flag=None):
    """ORB 特征匹配兜底：返回 {模板名: ((x,y), 置信度)}。

    模板会尝试多个缩放版本（分辨率/DPI 变化时更稳），要求足够内点，避免误匹配。
    """
    if min_matches is None:
        min_matches = int(getattr(config, "FEATURE_MIN_MATCHES", 10))
    if region is not None:
        screen, (off_x, off_y) = _crop_screen(screen_img, region)
        if screen is None:
            return {}
    else:
        screen, off_x, off_y = screen_img, 0, 0
    screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY) if screen.ndim == 3 else screen
    orb = cv2.ORB_create(nfeatures=1500)
    screen_kp, screen_desc = orb.detectAndCompute(screen_gray, None)
    results = {}
    if screen_desc is None or len(screen_kp) < min_matches:
        return results
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    scales = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4)
    for name, tpl in templates.items():
        if stop_flag is not None and stop_flag.is_set():
            raise StopExecution()
        bgr, _gray, _mask = _split_template(tpl)
        best = None
        for scale in scales:
            _orb, tpl_kp, tpl_desc = _orb_features(bgr, scale)
            if tpl_desc is None:
                continue
            try:
                pairs = matcher.knnMatch(tpl_desc, screen_desc, k=2)
            except cv2.error:
                continue
            good = []
            for pair in pairs:
                if len(pair) < 2:
                    continue
                first, second = pair
                if first.distance < 0.75 * second.distance:
                    good.append(first)
            if len(good) < min_matches:
                continue
            src_pts = np.float32([tpl_kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst_pts = np.float32([screen_kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            homography, inlier_mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            if homography is None or inlier_mask is None:
                continue
            inliers = int(inlier_mask.sum())
            if inliers < min_matches:
                continue
            height, width = bgr.shape[:2]
            center = np.float32([[[width / 2.0 * scale, height / 2.0 * scale]]])
            try:
                projected = cv2.perspectiveTransform(center, homography)[0][0]
            except cv2.error:
                continue
            confidence = min(1.0, inliers / max(20.0, min_matches * 2.0))
            if best is None or inliers > best[0]:
                best = (inliers, (int(projected[0] + off_x), int(projected[1] + off_y)), confidence)
        if best is not None:
            results[name] = (best[1], best[2])
    return results


def match_all_templates(screen_img, templates, threshold=THRESHOLD,
                        use_multi_scale=USE_MULTI_SCALE,
                        scale_range=SCALE_RANGE,
                        scale_step=SCALE_STEP,
                        search_center=None,
                        search_radius=None,
                        search_rect=None,
                        stop_flag=None,
                        allow_feature_fallback=False):
    """在画面中匹配所有模板，返回 {模板名: ((x,y) 或 None, 最高分)}。

    - 识别区域（search_rect）/中心+半径优先，保持旧语义；
    - 未显式限制时，先用上次命中位置附近的小窗口（PREFERRED_SEARCH_RADIUS），
      未命中再退回全屏；
    - 多尺度模板带缓存；透明模板可用 alpha 掩膜；
    - 命中后颜色二次校验；结果去重并按分数确定性选择。
    """
    screen_bgr = screen_img
    screen_gray = cv2.cvtColor(screen_img, cv2.COLOR_BGR2GRAY)
    scales = ([1.0] if not use_multi_scale else
              list(np.arange(scale_range[0], scale_range[1] + scale_step / 2.0, scale_step)))
    results = {}

    rect_left = rect_top = rect_right = rect_bottom = None
    if search_rect is not None:
        values = tuple(map(int, search_rect))
        if len(values) >= 4:
            rect_left, rect_top, rect_right, rect_bottom = values[:4]
            if rect_right < rect_left:
                rect_left, rect_right = rect_right, rect_left
            if rect_bottom < rect_top:
                rect_top, rect_bottom = rect_bottom, rect_top

    center_x = center_y = None
    radius_sq = None
    if search_center is not None:
        center_x, center_y = map(int, search_center)
        radius = float(search_radius) if search_radius is not None else 0.0
        radius_sq = radius * radius

    def is_in_search_area(x, y):
        if rect_left is not None:
            return rect_left <= x <= rect_right and rect_top <= y <= rect_bottom
        if center_x is None or radius_sq is None:
            return True
        dx = x - center_x
        dy = y - center_y
        return (dx * dx + dy * dy) <= radius_sq

    explicit_region = (search_rect is not None) or (search_center is not None)
    screen_height, screen_width = screen_gray.shape[:2]

    for name, tpl in templates.items():
        if stop_flag is not None and stop_flag.is_set():
            raise StopExecution()
        bgr, _gray, _mask = _split_template(tpl)

        if explicit_region:
            if search_rect is not None:
                regions = [(rect_left, rect_top, rect_right, rect_bottom)]
            else:
                radius = float(search_radius) if search_radius is not None else 0.0
                pad = int(max(radius, 1.0) + max(bgr.shape[:2]) / 2)
                regions = [(center_x - pad, center_y - pad, center_x + pad, center_y + pad)]
        else:
            regions = []
            preferred = _LAST_HITS.get(name)
            radius = int(getattr(config, "PREFERRED_SEARCH_RADIUS", 220))
            if preferred is not None:
                pad = int(max(radius, 1.0) + max(bgr.shape[:2]) / 2)
                regions.append((preferred[0] - pad, preferred[1] - pad,
                                preferred[0] + pad, preferred[1] + pad))
            regions.append((0, 0, screen_width, screen_height))

        candidates = []
        best_val = -1.0
        for region in regions:
            crop, (off_x, off_y) = _crop_screen(screen_bgr, region)
            if crop is None:
                continue
            crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            found, score = _match_one_region(crop, crop_gray, tpl, name, scales, threshold,
                                             off_x, off_y, is_in_search_area)
            best_val = max(best_val, score)
            candidates.extend(found)
            if candidates:
                break

        if not candidates and allow_feature_fallback and getattr(config, "FEATURE_FALLBACK", False):
            region = None
            if search_rect is not None:
                region = (rect_left, rect_top, rect_right, rect_bottom)
            features = feature_match_templates(screen_img, {name: tpl}, region=region,
                                               stop_flag=stop_flag)
            feature_result = features.get(name)
            if feature_result is not None:
                feature_center, feature_conf = feature_result
                if is_in_search_area(feature_center[0], feature_center[1]):
                    candidates.append((feature_center, feature_conf))
                    best_val = max(best_val, feature_conf)

        if candidates:
            candidates = _dedupe_candidates(candidates)
            best_candidate = candidates[0]
            results[name] = (best_candidate[0], float(best_candidate[1]))
            _LAST_HITS[name] = best_candidate[0]
        else:
            results[name] = (None, float(best_val))

    return results
