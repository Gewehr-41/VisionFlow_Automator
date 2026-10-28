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


def recognize(image_bgr, rect=None):
    """识别整图或指定区域，返回 [TextBlock]（画面坐标）。

    失败时抛 RuntimeError（带后端提示），由引擎包装成这一步的错误信息。
    """
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
    scale = 1.0
    if getattr(config, "OCR_PREPROCESS", True):
        # 小区域放大 2 倍：OCR 对小字/低分辨率裁剪的识别率明显更好
        height, width = target.shape[:2]
        if width < 800 or height < 160:
            scale = 2.0
            target = cv2.resize(target, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
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
    return blocks
