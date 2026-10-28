# -*- coding: utf-8 -*-
"""模板匹配工具：读取 PNG 模板并在画面中查找目标。

识别增强（2026-10-28）：
1. 多尺度模板缓存：同一模板的灰度和各缩放版本只计算一次；
2. 上次命中位置优先：先在上次位置附近找，未命中再退回全屏；
3. 透明模板 alpha 掩膜 + 命中后颜色二次校验，减少误匹配；
4. 候选去重 + 确定性选择（不再同分随机），配合执行层的多帧确认；
5. 模板匹配失败时可用 ORB 特征匹配兜底（缩放/分辨率变化）。
"""
import hashlib
import json
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
# 每个模板上次命中时对应的图片内容签名；图片换了 -> 签名不同 -> 命中点失效
_LAST_HIT_SIGNATURES = {}
# 模板首次命中时的屏幕高度：用于分辨率归一化（换分辨率时自动缩放）
_LAST_HIT_SCREEN_HEIGHTS = {}
_SIGNATURE_CACHE = {}
# 持久化文件路径（None = 只在内存里，不写盘；GUI/主程序启动时会设置）
_LAST_HITS_PATH = None
_MASK_ALPHA_THRESHOLD = 8


def clear_template_cache():
    """模板重新加载/绑定后清空图像缓存（命中点保留，靠内容签名判断是否失效）。"""
    _GRAY_CACHE.clear()
    _SCALED_CACHE.clear()
    _ORB_CACHE.clear()
    _SIGNATURE_CACHE.clear()


def _template_signature(tpl):
    """模板图片的内容签名（形状 + 像素），用于判断图片是否被换过。"""
    key = (id(tpl), tpl.shape)
    cached = _SIGNATURE_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(tpl.shape).encode("ascii"))
    digest.update(np.ascontiguousarray(tpl).tobytes())
    value = digest.hexdigest()
    _SIGNATURE_CACHE[key] = value
    return value


def set_last_hits_path(path):
    """设置上次命中点的持久化文件路径；传 None 关闭写盘。"""
    global _LAST_HITS_PATH
    _LAST_HITS_PATH = str(path) if path else None
    return _LAST_HITS_PATH


def clear_last_hits():
    """清空内存中的命中点（不删除文件）。"""
    _LAST_HITS.clear()
    _LAST_HIT_SIGNATURES.clear()
    _LAST_HIT_SCREEN_HEIGHTS.clear()


def save_last_hits():
    """把命中点原子写入 JSON；未配置路径时不做任何事。"""
    if not _LAST_HITS_PATH:
        return False
    payload = {
        "version": 1,
        "hits": {
            name: {"center": [int(center[0]), int(center[1])],
                   "signature": _LAST_HIT_SIGNATURES.get(name, ""),
                   "screen_height": int(_LAST_HIT_SCREEN_HEIGHTS.get(name) or 0)}
            for name, center in _LAST_HITS.items()
            if _LAST_HIT_SIGNATURES.get(name)
        },
    }
    try:
        from tasks import dump_json_atomic
        directory = Path(_LAST_HITS_PATH).parent
        directory.mkdir(parents=True, exist_ok=True)
        dump_json_atomic(str(_LAST_HITS_PATH), payload)
        return True
    except (OSError, ValueError):
        return False


def load_last_hits(path=None):
    """从 JSON 读取命中点；没有签名/格式不对的条目直接丢弃。"""
    if path is not None:
        set_last_hits_path(path)
    clear_last_hits()
    if not _LAST_HITS_PATH:
        return 0
    try:
        data = json.loads(Path(_LAST_HITS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    hits = data.get("hits", data)
    if not isinstance(hits, dict):
        return 0
    for name, item in hits.items():
        if not isinstance(item, dict):
            continue
        try:
            center = (int(item["center"][0]), int(item["center"][1]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        signature = item.get("signature")
        if not isinstance(signature, str) or not signature:
            continue
        _LAST_HITS[str(name)] = center
        _LAST_HIT_SIGNATURES[str(name)] = signature
        try:
            height = int(item.get("screen_height") or 0)
        except (TypeError, ValueError):
            height = 0
        if height > 0:
            _LAST_HIT_SCREEN_HEIGHTS[str(name)] = height
    return len(_LAST_HITS)


def prune_invalid_last_hits(templates):
    """模板重新加载后调用：模板被删除或图片内容变了 -> 清掉对应命中点。"""
    removed = 0
    for name in list(_LAST_HITS.keys()):
        tpl = templates.get(name)
        if tpl is None or _LAST_HIT_SIGNATURES.get(name) != _template_signature(tpl):
            _LAST_HITS.pop(name, None)
            _LAST_HIT_SIGNATURES.pop(name, None)
            _LAST_HIT_SCREEN_HEIGHTS.pop(name, None)
            removed += 1
    if removed:
        save_last_hits()
    return removed


def _get_last_hit(name, tpl):
    """取模板的上次命中点；图片内容已变化时立即失效并返回 None。"""
    center = _LAST_HITS.get(name)
    if center is None:
        return None
    if _LAST_HIT_SIGNATURES.get(name) != _template_signature(tpl):
        _LAST_HITS.pop(name, None)
        _LAST_HIT_SIGNATURES.pop(name, None)
        _LAST_HIT_SCREEN_HEIGHTS.pop(name, None)
        save_last_hits()
        return None
    return center


def _record_last_hit(name, tpl, center, screen_height=None):
    """记录命中点与首次命中时的屏幕高度（同位置不重复写盘）。"""
    signature = _template_signature(tpl)
    center = (int(center[0]), int(center[1]))
    height = int(screen_height or 0)
    height_changed = height > 0 and name not in _LAST_HIT_SCREEN_HEIGHTS
    if (_LAST_HITS.get(name) == center and _LAST_HIT_SIGNATURES.get(name) == signature
            and not height_changed):
        return
    _LAST_HITS[name] = center
    _LAST_HIT_SIGNATURES[name] = signature
    if height > 0 and name not in _LAST_HIT_SCREEN_HEIGHTS:
        # 只记录首次命中时的高度，作为模板的“原始分辨率”基准；后续不覆盖
        _LAST_HIT_SCREEN_HEIGHTS[name] = height
    save_last_hits()


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


def _template_mask(tpl, green_mask=False):
    """模板掩膜：alpha 透明区域 + 可选的纯绿 RGB(0,255,0) 区域。"""
    cache_key = (id(tpl), tpl.shape, bool(green_mask))
    cached = _GRAY_CACHE.get(("mask",) + cache_key)
    if cached is not None:
        return cached
    bgr, _gray, alpha_mask = _split_template(tpl)
    mask = alpha_mask
    if green_mask:
        tolerance = int(getattr(config, "GREEN_MASK_TOLERANCE", 40))
        blue, green, red = cv2.split(bgr)
        green_pixels = ((green >= 255 - tolerance) & (blue <= tolerance) & (red <= tolerance))
        keep = np.where(green_pixels, 0, 255).astype(np.uint8)
        mask = keep if mask is None else cv2.bitwise_and(mask, keep)
    if mask is not None and int(mask.min()) == 255:
        mask = None
    _GRAY_CACHE[("mask",) + cache_key] = mask
    return mask


def _scaled_template(tpl, scale, green_mask=False):
    """按比例缩放模板（灰/掩膜），带缓存。"""
    cache_key = (id(tpl), tpl.shape, round(float(scale), 4), bool(green_mask))
    cached = _SCALED_CACHE.get(cache_key)
    if cached is not None:
        return cached
    _bgr, gray, _alpha = _split_template(tpl)
    mask = _template_mask(tpl, green_mask)
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


def _color_check(tpl_bgr, tpl_mask, screen_bgr, left, top, width, height, patch_mask=None):
    tolerance = float(getattr(config, "COLOR_CHECK_TOLERANCE", 48.0))
    if tolerance <= 0:
        return True
    patch = screen_bgr[top:top + height, left:left + width]
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


_MATCH_METHODS = {
    "ccoeff": cv2.TM_CCOEFF_NORMED,
    "ccoeff_normed": cv2.TM_CCOEFF_NORMED,
    5: cv2.TM_CCOEFF_NORMED,
    "ccorr": cv2.TM_CCORR_NORMED,
    "ccorr_normed": cv2.TM_CCORR_NORMED,
    3: cv2.TM_CCORR_NORMED,
    "sqdiff": cv2.TM_SQDIFF_NORMED,
    "sqdiff_normed": cv2.TM_SQDIFF_NORMED,
    1: cv2.TM_SQDIFF_NORMED,
}


def resolve_match_method(method):
    """把任务字段/配置的 method 转成 cv2 模式；默认 CCOEFF_NORMED。"""
    if method is None or method == "":
        return cv2.TM_CCOEFF_NORMED
    try:
        key = int(method)
    except (TypeError, ValueError):
        key = str(method).strip().lower()
    return _MATCH_METHODS.get(key, cv2.TM_CCOEFF_NORMED)


def _match_one_region(screen_bgr, screen_gray, tpl, name, scales, threshold,
                      offset_x, offset_y, area_filter=None, allow_mask=True,
                      green_mask=False, method=None, coord_scale=1.0):
    """在给定区域里匹配单个模板；返回 (native candidates, best_score)。"""
    bgr, _gray, _alpha = _split_template(tpl)
    mask = _template_mask(tpl, green_mask) if allow_mask else None
    if mask is not None and not getattr(config, "USE_TEMPLATE_MASK", True):
        mask = None
    candidates = []
    best_val = -1.0
    requested_method = resolve_match_method(method)
    # 掩膜只对 SQDIFF/CCORR 类方法可靠；请求 CCOEFF 时用 SQDIFF_NORMED 并在分数上取反
    masked_method = (cv2.TM_CCORR_NORMED if requested_method == cv2.TM_CCORR_NORMED
                     else cv2.TM_SQDIFF_NORMED)
    passes = [True, False] if mask is not None else [False]

    for masked in passes:
        for scale in scales:
            scaled_gray, scaled_mask = _scaled_template(tpl, scale, green_mask)
            height, width = scaled_gray.shape[:2]
            if height > screen_gray.shape[0] or width > screen_gray.shape[1]:
                continue
            if masked and scaled_mask is not None:
                result = cv2.matchTemplate(screen_gray, scaled_gray, masked_method,
                                           mask=scaled_mask)
                scores = (1.0 - result) if masked_method == cv2.TM_SQDIFF_NORMED else result
            else:
                result = cv2.matchTemplate(screen_gray, scaled_gray, requested_method)
                scores = ((1.0 - result) if requested_method == cv2.TM_SQDIFF_NORMED
                          else result)
            ys, xs = np.where(scores >= threshold)
            for y, x in zip(ys, xs):
                conf = float(scores[y, x])
                center_x = int(round((x + offset_x + width / 2.0) / coord_scale))
                center_y = int(round((y + offset_y + height / 2.0) / coord_scale))
                if area_filter is not None and not area_filter(center_x, center_y):
                    continue
                if getattr(config, "COLOR_CHECK_ENABLED", True):
                    if not _color_check(bgr, mask, screen_bgr, x, y, width, height,
                                        patch_mask=scaled_mask):
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


def _scale_rect(rect, scale):
    left, top, right, bottom = rect
    return (int(round(left * scale)), int(round(top * scale)),
            int(round(right * scale)), int(round(bottom * scale)))


def _reference_height(templates, native_height):
    """参考高度：优先 config，其次模板首次命中时的高度（取众数），否则当前屏幕高。"""
    requested = int(getattr(config, "MATCH_REFERENCE_HEIGHT", 0) or 0)
    if requested > 0:
        return max(1, min(requested, native_height))
    heights = [_LAST_HIT_SCREEN_HEIGHTS.get(name) for name in templates]
    heights = [int(h) for h in heights if h]
    if not heights:
        return native_height
    return max(1, min(max(set(heights), key=heights.count), native_height))


def _order_candidates(candidates, order_by):
    order = str(order_by or "confidence").strip().lower()
    if order in ("horizontal", "left", "leftmost"):
        return sorted(candidates, key=lambda item: (item[0][1] // 20, item[0][0]))
    if order in ("vertical", "top", "topmost"):
        return sorted(candidates, key=lambda item: (item[0][0] // 20, item[0][1]))
    return sorted(candidates, key=lambda item: (-item[1], item[0][1], item[0][0]))


def match_all_templates(screen_img, templates, threshold=THRESHOLD,
                        use_multi_scale=USE_MULTI_SCALE,
                        scale_range=SCALE_RANGE,
                        scale_step=SCALE_STEP,
                        search_center=None,
                        search_radius=None,
                        search_rect=None,
                        stop_flag=None,
                        allow_feature_fallback=False,
                        green_mask=False,
                        method=None,
                        order_by="confidence",
                        match_index=0):
    """在画面中匹配所有模板，返回 {模板名: ((x,y) 或 None, 最高分)}。

    P0 增强（默认兼容旧行为）：
    - 分辨率归一化：按模板首次命中时的屏幕高度自动缩放画面/模板；
    - method（ccoeff/ccorr/sqdiff）、green_mask（纯绿背景忽略）、
      order_by + match_index（多个相同目标时取第 N 个）；
    - 上次命中点优先 -> 框选区域/全屏；多尺度缓存、颜色校验、特征兜底保留。
    """
    native_height, native_width = screen_img.shape[:2]
    reference_height = _reference_height(templates, native_height)
    coord_scale = reference_height / float(native_height)
    if abs(coord_scale - 1.0) > 1e-3:
        work_screen = cv2.resize(
            screen_img,
            (max(1, int(round(native_width * coord_scale))), reference_height),
            interpolation=cv2.INTER_AREA,
        )
    else:
        work_screen = screen_img
    screen_bgr = work_screen
    screen_gray = cv2.cvtColor(work_screen, cv2.COLOR_BGR2GRAY)

    base_scales = ([1.0] if not use_multi_scale else
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
    norm_width = screen_gray.shape[1]
    norm_height = screen_gray.shape[0]

    for name, tpl in templates.items():
        if stop_flag is not None and stop_flag.is_set():
            raise StopExecution()
        bgr, _gray, _mask = _split_template(tpl)

        # 目标在当前画面里的比例：模板基准高度 h0，参考高度 ref ->
        # 模板在当前画面中的大小 = 原尺寸 * (ref / h0) * (当前高 / ref)
        template_height = _LAST_HIT_SCREEN_HEIGHTS.get(name)
        base_scale = (reference_height / template_height) if template_height else 1.0
        scales = [scale * base_scale for scale in base_scales]

        radius = int(getattr(config, "PREFERRED_SEARCH_RADIUS", 220))
        regions = []
        preferred = _get_last_hit(name, tpl)
        if preferred is not None:
            pad = int(max(radius, 1.0) + max(bgr.shape[:2]) / 2)
            regions.append((_scale_rect((preferred[0] - pad, preferred[1] - pad,
                                         preferred[0] + pad, preferred[1] + pad),
                                        coord_scale), None))
        if explicit_region:
            if search_rect is not None:
                regions.append((_scale_rect((rect_left, rect_top, rect_right, rect_bottom),
                                            coord_scale), is_in_search_area))
            else:
                search_radius_value = float(search_radius) if search_radius is not None else 0.0
                pad = int(max(search_radius_value, 1.0) + max(bgr.shape[:2]) / 2)
                regions.append((_scale_rect((center_x - pad, center_y - pad,
                                             center_x + pad, center_y + pad),
                                            coord_scale), is_in_search_area))
        else:
            regions.append(((0, 0, norm_width, norm_height), None))

        candidates = []
        best_val = -1.0
        coarse_enabled = bool(getattr(config, "COARSE_TO_FINE", True))
        coarse_min_area = int(getattr(config, "COARSE_MIN_AREA", 900000))
        coarse_factor = float(getattr(config, "COARSE_FACTOR", 0.5) or 0.5)
        for region, area_filter in regions:
            crop, (off_x, off_y) = _crop_screen(screen_bgr, region)
            if crop is None:
                continue
            crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

            # 粗到细：大区域先半分辨率找候选，再在原分辨率候选附近精修；
            # 粗搜没找到时仍会退回原来的全区域全尺度匹配，不影响正确性。
            if (coarse_enabled and 0.3 <= coarse_factor < 1.0
                    and crop.shape[0] * crop.shape[1] >= coarse_min_area):
                coarse_crop = cv2.resize(crop, None, fx=coarse_factor, fy=coarse_factor,
                                         interpolation=cv2.INTER_AREA)
                coarse_gray = cv2.cvtColor(coarse_crop, cv2.COLOR_BGR2GRAY)
                coarse_scales = [scale * coarse_factor for scale in scales]
                coarse_threshold = max(0.5, float(threshold) - 0.05)
                coarse_found, coarse_score = _match_one_region(
                    coarse_crop, coarse_gray, tpl, name, coarse_scales, coarse_threshold,
                    off_x * coarse_factor, off_y * coarse_factor, area_filter,
                    green_mask=green_mask, method=method,
                    coord_scale=coord_scale * coarse_factor,
                )
                best_val = max(best_val, coarse_score)
                if coarse_found:
                    coarse_center = _order_candidates(
                        _dedupe_candidates(coarse_found), order_by)[0][0]
                    pad = int(max(24, max(bgr.shape[:2]) * max(1.0, base_scale) * 2.5))
                    refine_rect = (coarse_center[0] * coord_scale - pad,
                                   coarse_center[1] * coord_scale - pad,
                                   coarse_center[0] * coord_scale + pad,
                                   coarse_center[1] * coord_scale + pad)
                    refine_crop, (refine_x, refine_y) = _crop_screen(screen_bgr, refine_rect)
                    if refine_crop is not None:
                        refine_gray = cv2.cvtColor(refine_crop, cv2.COLOR_BGR2GRAY)
                        found, score = _match_one_region(
                            refine_crop, refine_gray, tpl, name, scales, threshold,
                            refine_x, refine_y, area_filter, green_mask=green_mask,
                            method=method, coord_scale=coord_scale,
                        )
                        best_val = max(best_val, score)
                        candidates.extend(found)
                        if candidates:
                            break

            found, score = _match_one_region(
                crop, crop_gray, tpl, name, scales, threshold,
                off_x, off_y, area_filter, green_mask=green_mask,
                method=method, coord_scale=coord_scale,
            )
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
            ordered = _order_candidates(_dedupe_candidates(candidates), order_by)
            index = int(match_index or 0)
            if index < 0:
                index += len(ordered)
            if 0 <= index < len(ordered):
                chosen = ordered[index]
                results[name] = (chosen[0], float(chosen[1]))
                _record_last_hit(name, tpl, chosen[0], native_height)
            else:
                results[name] = (None, float(best_val))
        else:
            results[name] = (None, float(best_val))

    return results
