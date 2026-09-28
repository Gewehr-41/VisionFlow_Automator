# -*- coding: utf-8 -*-
"""资源与数据路径的**唯一**来源。

为什么单独一个模块
------------------
此前路径分散在三处、且都用 `os.path.dirname(__file__)` 推导：
  · `config.ICON_DIR`（模板库）
  · `tasks.TASKS_FILE` / `PRESETS_FILE` / `BLUEPRINT_LAYOUT_FILE` / `BLUEPRINT_GRAPH_FILE`
  · `gui_pyside6.py` 里两个 Qt 样式 SVG
后果有两个：一是打包后 `__file__` 指向 PyInstaller 的临时解压目录，四个 JSON 会被写进去、
退出即丢；二是"写盘代码搬到别的模块"这件事没有统一入口可依赖。

现在把两类路径分开：
  · **只读资源**（`icons/`）：随程序分发。打包后位于 `sys._MEIPASS`，源码运行时是项目根。
  · **可写数据**（4 个 JSON）：放在"程序目录"——打包后是 exe 所在目录，源码运行时是项目根。
    可用环境变量 **`VF_DATA_DIR`** 覆盖（便携部署、测试隔离都用它）。

兼容性：`config.ICON_DIR` 与 `tasks.*_FILE` 仍然是可导入的模块级名字，
既有代码与测试里的 `tasks.PRESETS_FILE = <临时目录>` 这类补丁继续有效。
"""
import os
import sys

# ---- 程序目录（数据落在这里）与资源目录（只读素材在这里）----


def _frozen():
    return bool(getattr(sys, "frozen", False))


def app_root():
    """可写数据所在目录：打包后是 exe 所在目录，源码运行时是项目根。"""
    if _frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_root():
    """只读资源所在目录：打包后是 PyInstaller 的解压目录，源码运行时是项目根。"""
    if _frozen():
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            return os.path.abspath(bundle)
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """数据目录：环境变量 `VF_DATA_DIR` 优先（便携部署 / 测试隔离）。"""
    override = os.environ.get("VF_DATA_DIR")
    if override:
        return os.path.abspath(override)
    return app_root()


# ---- 对外常量（在导入时定下；改环境变量请在导入之前设置）----

ICON_DIR = os.path.join(resource_root(), "icons")

TASKS_FILE = os.path.join(data_dir(), "saved_tasks.json")
PRESETS_FILE = os.path.join(data_dir(), "saved_presets.json")
BLUEPRINT_LAYOUT_FILE = os.path.join(data_dir(), "saved_blueprint_layouts.json")
BLUEPRINT_GRAPH_FILE = os.path.join(data_dir(), "saved_blueprint_graphs.json")

DATA_FILE_NAMES = (
    "saved_tasks.json",
    "saved_presets.json",
    "saved_blueprint_layouts.json",
    "saved_blueprint_graphs.json",
)


def describe():
    """一行说明路径现状，便于排障与打包后确认（不含敏感信息）。"""
    mode = "打包运行" if _frozen() else "源码运行"
    return (f"[{mode}] 资源目录={resource_root()}；数据目录={data_dir()}"
            + ("（来自环境变量 VF_DATA_DIR）" if os.environ.get("VF_DATA_DIR") else ""))
