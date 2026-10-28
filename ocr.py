# -*- coding: utf-8 -*-
"""OCR 文字识别后端（可选能力）。

为什么做"可选"
--------------
识别一张画面要 1.2~2.0 秒（CPU，实测 RapidOCR 1.4.4：520×90 全图 1.6~2.1s，
裁小区域 1.2~1.8s），比模板匹配慢 1~2 个数量级。所以 OCR 只作为**部分步骤的替代识别
方式**：普通的图找图步骤继续走模板匹配，只有"界面上没有稳定图标、只能靠文字"的步骤
才勾选 OCR。

后端选择
--------
目前接的是 `rapidocr_onnxruntime`（onnxruntime + 内置中文模型，pip 装包 14.9 MB，
无需联网下载模型）。没装时**不报错、不崩**：`available()` 返回 False，
引擎遇到勾了 OCR 的步骤会抛出带安装命令的可读错误，体检也会提前拦下来。

为什么不用 Windows 自带 OCR
---------------------------
`Windows.Media.Ocr` 体积更小、系统里已有 zh-cn 语言包（实测 `C:\\Windows\\OCR\\zh-cn`），
但要依赖 `winrt-*` 绑定且 PyInstaller 打包这些 WinRT 组件比较麻烦；先留作后续可插拔后端。
"""
import cv2
import numpy as np

import config

BACKEND_NAME = "rapidocr"
INSTALL_COMMAND = "pip install rapidocr-onnxruntime"

_detected = None          # None=未探测；""=不可用；"rapidocr"=可用
_engine = None
# 最近一次 OCR 结果缓存：key=(图像指纹, 形状, 偏移) -> [TextBlock]
_LAST_RESULT_KEY = None
_LAST_RESULT_BLOCKS = None


class TextBlock:
    """一块识别结果：文字、置信度、(x, y) 中心点（画面坐标）。"""

    __slots__ = ("text", "score", "center", "box")

    def __init__(self, text, score, center, box=None):
        self.text = str(text)
        self.score = float(score)
        self.center = center
        self.box = box

    def __repr__(self):
        return f"TextBlock({self.text!r}, {self.score:.2f}, {self.center})"


def _detect_backend():
    """探测可用后端；返回 "rapidocr" 或 ""。"""
    global _detected
    if _detected is None:
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except Exception:
            _detected = ""
        else:
            _detected = BACKEND_NAME
    return _detected


def available():
    return bool(_detect_backend())


def backend_name():
    return _detect_backend() or None


def install_hint():
    """给用户看的可执行提示（会被写进日志/弹出错误）。"""
    return (f"本机没有 OCR 后端（需要 {BACKEND_NAME}）。请在工程目录运行：{INSTALL_COMMAND}")

def _get_engine():
    """懒加载识别引擎（首次调用约 0.6 秒，只初始化一次）。"""
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _crop(image_bgr, rect):
    """按 (left, top, right, bottom) 裁剪，返回 (子图, 偏移)。"""
    height, width = image_bgr.shape[:2]
    left, top, right, bottom = (int(value) for value in rect)
    left, right = max(0, min(left, right)), min(width, max(left, right))
    top, bottom = max(0, min(top, bottom)), min(height, max(top, bottom))
    if right - left < 2 or bottom - top < 2:
        return None, (0, 0)
    return image_bgr[top:bottom, left:right], (left, top)


def _image_signature(image_bgr):
    """图像快速指纹：缩成 32x32 灰度后取字节（约 1ms），用于复用同一画面的 OCR 结果。"""
    try:
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY) if image_bgr.ndim == 3 else image_bgr
        small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
        return (image_bgr.shape, small.tobytes())
    except cv2.error:
        return None


def _blocks_from_text_rec(texts, offset, scale, target_bgr):
    """把 text_rec 的单行结果包装成 TextBlock（中心取 ROI 中心）。"""
    height, width = target_bgr.shape[:2]
    center_x = width / 2.0 / scale + offset[0]
    center_y = height / 2.0 / scale + offset[1]
    blocks = []
    for item in texts or []:
        if len(item) < 2:
            continue
        text, score = item[0], item[1]
        blocks.append(TextBlock(text, score, (int(round(center_x)), int(round(center_y)))))
    return blocks


def recognize(image_bgr, rect=None):
    """识别整图或指定区域，返回 [TextBlock]（画面坐标）。

    速度优化（不改步骤字段）：
    - 大图先缩到 OCR_MAX_WIDTH，再检测/识别；
    - 单行小 ROI 直接用 rec 模型（约 0.2s，跳过大图检测）；
    - 同一画面 + 同一 ROI 的 OCR 结果直接复用（等待静态画面时不重复识别）。

    失败时抛 RuntimeError（带后端提示），由引擎包装成这一步的错误信息。
    """
    global _LAST_RESULT_KEY, _LAST_RESULT_BLOCKS
    if image_bgr is None:
        return []
    if not available():
        raise RuntimeError(install_hint())
    offset = (0, 0)
    target = image_bgr
    if rect is not None:
        target, offset = _crop(image_bgr, rect)
        if target is None:
            return []

    signature = _image_signature(target) if getattr(config, "OCR_CACHE", True) else None
    if signature is not None:
        cache_key = (signature, offset)
        if cache_key == _LAST_RESULT_KEY:
            return list(_LAST_RESULT_BLOCKS or [])

    height, width = target.shape[:2]
    # 单行小 ROI：跳过检测模型，直接 rec
    if (getattr(config, "OCR_FAST_LINE", True)
            and height <= int(getattr(config, "OCR_FAST_LINE_MAX_HEIGHT", 80))
            and width >= height * float(getattr(config, "OCR_FAST_LINE_MIN_ASPECT", 2.2))):
        try:
            texts, _elapsed = _get_engine().text_rec([np.ascontiguousarray(target)])
        except Exception:
            texts = None
        if texts:
            blocks = _blocks_from_text_rec(texts, offset, 1.0, target)
            if signature is not None:
                _LAST_RESULT_KEY = (signature, offset)
                _LAST_RESULT_BLOCKS = blocks
            return blocks

    # 大图缩放到 OCR_MAX_WIDTH，降低检测模型输入尺寸
    max_width = int(getattr(config, "OCR_MAX_WIDTH", 1280) or 0)
    scale = 1.0
    if max_width > 0 and width > max_width:
        scale = max_width / float(width)
        target = cv2.resize(target, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        height, width = target.shape[:2]

    # 小区域放大 2 倍：OCR 对小字/低分辨率裁剪的识别率更好
    if getattr(config, "OCR_PREPROCESS", True):
        if width < 800 or height < 160:
            scale *= 2.0
            target = cv2.resize(target, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    target = np.ascontiguousarray(target)

    try:
        # use_cls=False：跳过方向分类模型，实测能省一点时间（界面文字不会倒着放）
        result, _elapsed = _get_engine()(target, use_cls=False)
    except Exception as exc:
        raise RuntimeError(f"OCR 识别失败：{exc}") from exc

    blocks = []
    for item in result or []:
        try:
            box, text, score = item[0], item[1], item[2]
        except (TypeError, IndexError):
            continue
        points = [tuple(float(value) for value in point) for point in box]
        if not points:
            continue
        center_x = sum(point[0] for point in points) / len(points) / scale + offset[0]
        center_y = sum(point[1] for point in points) / len(points) / scale + offset[1]
        blocks.append(TextBlock(text, score, (int(round(center_x)), int(round(center_y))), points))
    if signature is not None:
        _LAST_RESULT_KEY = (signature, offset)
        _LAST_RESULT_BLOCKS = blocks
    return blocks
