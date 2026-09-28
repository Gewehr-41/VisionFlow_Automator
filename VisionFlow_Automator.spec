# -*- mode: python ; coding: utf-8 -*-
r"""VisionFlow Automator 的 PyInstaller 规格（onedir + 无控制台窗口）。

构建（规格里不写死输出路径，由命令行给）：

    pyinstaller --noconfirm --clean \
        --workpath <临时目录>\vf_build --distpath <输出目录>\vf_dist \
        VisionFlow_Automator.spec

三条不显然、但改动前必须先读的实测结论
--------------------------------------
1) ``datas=[('icons', 'icons')]`` 是必需的：
   ``config.ICON_DIR`` 由 ``paths.resource_root()`` 推导，打包后等于 ``sys._MEIPASS``，
   即 ``<dist>\VisionFlow_Automator\_internal``，所以 icons 必须落在
   ``_internal\icons``。少了它 → 模板库全空、识别全失效。

2) **不要排除 tkinter**：``pyautogui`` 在**导入期**就 ``import mouseinfo``，
   而 ``mouseinfo/__init__.py`` 在 Windows 分支的 ``import tkinter`` **没有**
   try/except 保护（``pymsgbox`` 那个有保护，容易被误判为安全）。
   一旦排除 tkinter，``core/input.py`` 的 ``import pyautogui`` 直接
   ModuleNotFoundError → 程序启动即失败。

3) **不要排除 PySide6.QtSvg**：``gui_pyside6.py`` 的 QSS 用
   ``checkbox_checked.svg`` / ``checkbox_indeterminate.svg`` 画勾选框图标，
   需要 ``plugins\imageformats\qsvg.dll``，该插件又依赖 ``Qt6Svg.dll``。
   构建后请确认这两个文件确实在 ``_internal`` 里（PyInstaller 不保证自动收集）。

数据文件落点
------------
4 个 ``saved_*.json`` 由 ``paths.data_dir()`` 决定：打包后是 **exe 所在目录**
（可用环境变量 ``VF_DATA_DIR`` 覆盖）。因此 exe 必须放在**可写目录**，
不要放进 ``C:\Program Files`` 这类需要管理员权限的位置。

排除项
------
只排除实测未使用的 Qt 模块：本机 Qt6*.dll 共 316.2 MB，其中与本项目无关的
115 个模块占 277.5 MB（``Qt6WebEngineCore.dll`` 单个就有 194.0 MB）。

二次裁剪（``_DROP``）
---------------------
``excludes`` 只能挡住 **Python 绑定**，挡不住 PyInstaller 通过二进制依赖分析
带进来的 **C++ DLL**。所以下面再按文件名裁一层，每一项都有依赖图证据：

* ``Qt6VirtualKeyboard.dll`` → ``Qt6Quick.dll`` → ``Qt6Qml.dll`` → ``Qt6Network.dll``
  （Qt 虚拟键盘与声明式 UI，本项目只用 QtWidgets，零使用）
* ``plugins\\imageformats\\qpdf.dll`` → ``Qt6Pdf.dll``（把 PDF 当图片读，零使用）
* ``plugins\\generic\\qtuiotouchplugin.dll`` → ``Qt6Network.dll``（TUIO 触摸协议，零使用）
* ``opencv_videoio_ffmpeg4120_64.dll``（27.0 MB）：全项目 **0 处** ``VideoCapture`` /
  ``VideoWriter``；cvv 的 API 面只有 imread/imdecode/cvtColor/resize/matchTemplate/
  minMaxLoc，且该 DLL 没有任何 PE 静态依赖（OpenCV 仅在用到视频时动态加载）

**故意保留**：``Qt6Svg.dll``（复选框 SVG 样式）、``opengl32sw.dll``（19.7 MB，
GPU 驱动不可用时的软件 OpenGL 兜底，删了在虚拟机/远程桌面上可能黑窗）、
``PIL/_avif*.pyd``（7.5 MB，AVIF 解码插件，风险极低但收益也小）。
"""
import os

# 实测零使用、且依赖图已确认无其它需求方的 C++ 模块（键为小写文件名）
_DROP = {
    "qt6virtualkeyboard.dll",
    "qt6quick.dll",
    "qt6qml.dll",
    "qt6qmlmeta.dll",
    "qt6qmlmodels.dll",
    "qt6qmlworkerscript.dll",
    "qt6network.dll",
    "qt6pdf.dll",
    "qpdf.dll",
    "qtuiotouchplugin.dll",
    "opencv_videoio_ffmpeg4120_64.dll",
}


a = Analysis(
    ['gui_pyside6.py'],
    pathex=[],
    binaries=[],
    datas=[('icons', 'icons')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtNetwork', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQuickWidgets', 'PySide6.QtQuickControls2', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput', 'PySide6.Qt3DLogic', 'PySide6.Qt3DAnimation', 'PySide6.Qt3DExtras', 'PySide6.QtCharts', 'PySide6.QtGraphs', 'PySide6.QtDataVisualization', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtDesigner', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtSensors', 'PySide6.QtSerialPort', 'PySide6.QtPositioning', 'PySide6.QtLocation', 'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtRemoteObjects', 'PySide6.QtScxml', 'PySide6.QtSpatialAudio', 'PySide6.QtTextToSpeech', 'PySide6.QtWebSockets', 'PySide6.QtWebChannel', 'PySide6.QtHelp', 'PySide6.QtUiTools', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtConcurrent', 'PyQt5', 'PyQt6', 'PySide2', 'matplotlib', 'pandas', 'scipy', 'IPython', 'pytest', 'docutils', 'setuptools'],
    noarchive=False,
    optimize=0,
)

# 见文件头「二次裁剪」：excludes 管不到 C++ DLL，这里按文件名再过滤一层
_dropped = sorted(os.path.basename(b[0]) for b in a.binaries
                  if os.path.basename(b[0]).lower() in _DROP)
a.binaries = [b for b in a.binaries if os.path.basename(b[0]).lower() not in _DROP]
print("[spec] 二次裁剪掉 %d 个文件: %s" % (len(_dropped), ", ".join(_dropped)))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VisionFlow_Automator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='VisionFlow_Automator',
)
