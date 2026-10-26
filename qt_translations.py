# -*- coding: utf-8 -*-
"""加载 Qt 官方中文翻译：标准控件的文字（是/否/确定/取消、「显示详细信息」…）。

为什么需要
----------
`QMessageBox.setDetailedText()` 自动补出来的「展开/收起」按钮，以及 QMessageBox 的
是/否/确定/取消、QFileDialog、QInputDialog 等**标准文字**，全部来自 Qt 自带的翻译
文件（`qtbase_zh_CN.qm`）。不加载翻译器，它们永远是英文——所有者 2026-10-26 反馈的
"执行前体检弹窗的展开/收起详细选项是英文"就是这个原因。

打包注意
--------
- 源码运行：翻译文件在 `site-packages/PySide6/translations`（由 QLibraryInfo 给出）
- 打包运行：在 `_internal/PySide6/translations`（`VisionFlow_Automator.spec` 显式收集
  了 `qt_zh_CN.qm` 与 `qtbase_zh_CN.qm` 两个文件）
所以这里按「先资源目录、再 QLibraryInfo 默认路径」的顺序找；两处都找不到时**静默回退
英文**并只在诊断里留一条警告，绝不影响启动。
"""
import os

from PySide6.QtCore import QLibraryInfo, QTranslator

import diagnostics
import paths

# 必须持有引用：QTranslator 被垃圾回收后翻译会失效（Qt 文档也要求保持存活）
_TRANSLATORS = []


def translation_dirs():
    """候选翻译目录（打包目录优先，其次是 PySide6 自带路径）。"""
    candidates = []
    try:
        candidates.append(os.path.join(paths.resource_root(), "PySide6", "translations"))
    except Exception:      # 路径探测失败不应影响启动
        pass
    try:
        candidates.append(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    except Exception:
        pass
    seen = set()
    result = []
    for directory in candidates:
        if directory and directory not in seen:
            seen.add(directory)
            result.append(directory)
    return result


def install_qt_translations(app):
    """给 QApplication 装上 Qt 中文翻译，返回成功加载的翻译文件数。

    0 表示没找到翻译文件（界面仍可用，只是标准按钮回退英文）。
    """
    if app is None:
        return 0
    loaded = 0
    for directory in translation_dirs():
        if not os.path.isdir(directory):
            continue
        # qt_zh_CN 是汇总包；qtbase_zh_CN 覆盖核心控件文字。两个都装，后者优先生效。
        for name in ("qt_zh_CN", "qtbase_zh_CN"):
            translator = QTranslator(app)
            if translator.load(name, directory):
                app.installTranslator(translator)
                _TRANSLATORS.append(translator)
                loaded += 1
        if loaded:
            break
    if not loaded:
        diagnostics.warn(
            "警告: 未能加载 Qt 中文翻译（标准对话框按钮会显示英文）；"
            f"已尝试目录: {', '.join(translation_dirs()) or '（无）'}"
        )
    return loaded
