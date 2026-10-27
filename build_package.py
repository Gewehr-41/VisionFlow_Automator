#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键打包：构建 → 部署到分发目录 → 校验（可选出 zip）。

为什么要有这个脚本
------------------
打包这条流程有 4 个"实测踩过"的坑，每一步都不是想当然：

1. **构建中间物不能落进工程目录**：`build/`、`dist/`、`*.spec` 里的临时产物会被
   `git status` 当成新文件，也会混进分发内容。脚本一律把中间物放在 `%TEMP%` 下。
2. **分发目录里有用户数据**（4 个 `saved_*.json` 就在 exe 同级）：整体覆盖式部署会把
   它们删掉。脚本**只替换程序项**（exe、`_internal`），并在部署前后逐个比对哈希。
3. **`.spec` 的裁剪名单里藏着"必须保留"的东西**：`Qt6Svg.dll` + `qsvg.dll`（勾选框
   样式，缺了勾选框变空白）、`opengl32sw.dll`（软件 OpenGL 兜底，缺了在虚拟机/远程
   桌面里黑窗）、`tkinter`（`pyautogui` 导入期依赖，缺了**程序直接起不来**）。
   脚本事后逐个检查，避免"打包成功但一启动就死"。
4. **分发版正在运行时替换文件会半途失败**，留下残缺目录。脚本先检测进程并拒绝执行。

校验不通过时返回非 0 退出码，可直接接到自动化里。

用法
----
    python build_package.py                # 构建 + 部署（保留分发目录里的数据文件）
    python build_package.py --zip          # 再打一个 zip，便于发给别人
    python build_package.py --sync-data    # 把工程里的 4 个数据文件同步到分发目录（先备份）
    python build_package.py --build-only   # 只构建，不动分发目录
    python build_package.py --keep-work    # 保留构建中间目录，便于排障

双击 `打包.bat` 等价于 `python build_package.py`。
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import paths

ROOT = Path(paths.app_root()).resolve()
SPEC = ROOT / "VisionFlow_Automator.spec"
PRODUCT = "VisionFlow_Automator"
EXE_NAME = f"{PRODUCT}.exe"
# 分发目录与工程**同级**（不要放进工程目录，否则会被 git 看到、也可能被误提交）
TARGET_DIR = ROOT.parent / f"{PRODUCT}_打包版"
DATA_FILE_NAMES = tuple(paths.DATA_FILE_NAMES)

# 控制台可能是 GBK（直接运行）或 UTF-8（打包.bat 里 chcp 65001 + PYTHONIOENCODING）；
# 两种情况都不要因为个别字符编不出来就崩掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

# 打包产物里"必须有"的东西：改 `.spec` 的 `excludes` / `_DROP` 之前先读这一份。
# 这些路径**逐个对照过真实产物**（2026-10-10，1201 个程序文件 / 202.8 MB 的那份）：
# 注意 Qt 的 DLL 与 opengl32sw.dll 都在 `_internal\PySide6\` 下，不在 `_internal\` 根上
# —— 一开始把 opengl32sw.dll 写成 `_internal\opengl32sw.dll`，结果对好包误报"缺少"。
REQUIRED_ENTRIES = (
    (os.path.join("_internal", "PySide6", "Qt6Svg.dll"),
     "Qt 的 SVG 支持：勾选框样式依赖它，缺了勾选框会变成空白"),
    (os.path.join("_internal", "PySide6", "plugins", "imageformats", "qsvg.dll"),
     "Qt 的 SVG 图片插件：同上"),
    (os.path.join("_internal", "PySide6", "opengl32sw.dll"),
     "软件 OpenGL 兜底：虚拟机 / 远程桌面里缺了会黑窗"),
    (os.path.join("_internal", "_tk_data"),
     "pyautogui 在导入期依赖 tkinter，缺了程序直接起不来"),
    (os.path.join("_internal", "PySide6", "translations", "qtbase_zh_CN.qm"),
     "Qt 官方中文翻译：标准对话框按钮（显示详细信息 / 是 / 否 / 取消）靠它本地化"),
    (os.path.join("_internal", "icons"),
     "模板库（识别用），随程序分发"),
    (os.path.join("_internal", "PySide6", "plugins", "platforms", "qwindows.dll"),
     "Qt 平台插件：缺了任何窗口都创建不出来"),
)

# 分发版的运行库要能 import 成功，否则构建出来的东西是坏的
REQUIRED_MODULES = ("PySide6", "cv2", "numpy", "mss", "pyautogui", "PIL", "pygetwindow")

EXIT_OK = 0
EXIT_PREFLIGHT = 2
EXIT_BUILD = 3
EXIT_VERIFY = 4


def log(message=""):
    print(_console_safe(message), flush=True)


def _console_safe(text):
    """GBK 控制台画不出 ✓ / ✗，就地退化成 ASCII 标记（中文本身能正常显示）。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except (UnicodeEncodeError, LookupError, TypeError):
        return text.replace("✓", "[ok]").replace("✗", "[!!]")


def rule(title):
    log()
    log("=" * 68)
    log(title)
    log("=" * 68)


def hash_file(path, chunk=1 << 20):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def data_files(base_dir):
    """目录里的 4 个数据文件（不存在的跳过）。"""
    base = Path(base_dir)
    return [base / name for name in DATA_FILE_NAMES if (base / name).exists()]


def snapshot_data(base_dir):
    """数据文件名 → 哈希。部署前拍快照，部署后比对，确保一个字节都没动。"""
    return {path.name: hash_file(path) for path in data_files(base_dir)}


def program_entries(dist_dir):
    """dist 顶层里属于"程序"的项（排除数据文件，双保险）。"""
    return [item for item in sorted(Path(dist_dir).iterdir())
            if item.name not in DATA_FILE_NAMES]


def dir_stats(base_dir):
    base = Path(base_dir)
    files = [p for p in base.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    return len(files), total


def count_icons(base_dir):
    icon_dir = Path(base_dir) / "_internal" / "icons"
    if not icon_dir.is_dir():
        return 0, 0
    png = sum(1 for p in icon_dir.glob("*.png"))
    svg = sum(1 for p in icon_dir.glob("*.svg"))
    return png, svg


def process_running(image_name=EXE_NAME):
    """分发版是否正在运行（占着文件就没法替换）。"""
    # 用临时文件接 stdout：`capture_output=True` 会起读取线程，在已加载 Qt/大量 GUI 测试的
    # 进程里偶发线程创建失败（Windows fast-fail 0xC0000409）。重定向到文件后只需等待，更稳。
    try:
        with tempfile.TemporaryFile() as stdout_file:
            subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV"],
                           stdout=stdout_file, stderr=subprocess.DEVNULL, timeout=20)
            stdout_file.seek(0)
            text = stdout_file.read().decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    return image_name.lower() in text.lower()


def missing_modules(modules=REQUIRED_MODULES):
    import importlib.util
    return [name for name in modules if importlib.util.find_spec(name) is None]


def ensure_pyinstaller():
    """确认 PyInstaller 可用；缺失就装到当前解释器里。返回版本号或 None。

    所有外部调用都带超时：网络卡住或目标目录不可写时，宁可干脆失败，
    也不要让"一键打包"在没人看着的时候永久挂着。
    """
    def probe():
        try:
            result = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                                    capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    version = probe()
    if version:
        return version
    log("未检测到 PyInstaller，正在安装：python -m pip install pyinstaller")
    try:
        install = subprocess.run([sys.executable, "-m", "pip", "install", "--no-input",
                                  "--disable-pip-version-check", "pyinstaller"],
                                 cwd=str(ROOT), timeout=600)
    except subprocess.TimeoutExpired:
        log("  pip 安装超过 10 分钟，已放弃")
        return None
    if install.returncode != 0:
        log("  pip 安装失败（常见原因：目标 site-packages 不可写，或没有网络）")
        return None
    return probe()


def existing_bundle(path):
    """校验 `--from-dist` 给的目录确实是一个构建产物，返回它或 None。"""
    bundle = Path(path)
    if bundle.is_dir() and (bundle / EXE_NAME).exists():
        return bundle
    return None


def run_build(work_dir, dist_dir):
    """按规格构建，返回 onedir 产物目录（dist/<PRODUCT>）。"""
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--workpath", str(work_dir), "--distpath", str(dist_dir), str(SPEC)]
    log("$ " + " ".join(command))
    started = time.time()
    result = subprocess.run(command, cwd=str(ROOT))
    log(f"构建耗时 {time.time() - started:.1f} 秒，退出码 {result.returncode}")
    if result.returncode != 0:
        return None
    bundle = Path(dist_dir) / PRODUCT
    return bundle if bundle.is_dir() else None


def deploy(dist_dir, target_dir, log_callback=log):
    """把构建产物里的程序项替换到分发目录；数据文件与其它文件一律不动。"""
    target = Path(target_dir)
    fresh = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    replaced = []
    for item in program_entries(dist_dir):
        destination = target / item.name
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)
        replaced.append(item.name)
        log_callback(f"  已替换 {item.name}")
    return {"target": target, "replaced": replaced, "fresh": fresh}


def verify(dist_dir, target_dir, before_hashes, strict=True):
    """返回问题列表；空列表表示校验通过。

    三类检查：① 数据文件逐字节不变 ② 程序项与构建产物一致 ③ 该有的运行库都在。
    """
    problems = []
    target = Path(target_dir)
    dist = Path(dist_dir)

    # ① 数据文件
    for name, digest in before_hashes.items():
        path = target / name
        if not path.exists():
            problems.append(f"数据文件丢失：{name}")
        elif hash_file(path) != digest:
            problems.append(f"数据文件被改动：{name}")

    # ② 程序项与构建产物一致
    for item in program_entries(dist):
        destination = target / item.name
        if not destination.exists():
            problems.append(f"程序项缺失：{item.name}")
        elif item.is_file() and hash_file(destination) != hash_file(item):
            problems.append(f"程序项与构建产物不一致：{item.name}")

    # ③ 必须存在的运行库 / 资源
    for relative, why in REQUIRED_ENTRIES:
        if not (target / relative).exists():
            problems.append(f"缺少 {relative} —— {why}")

    png, svg = count_icons(target)
    if png == 0:
        problems.append("模板库是空的（_internal/icons 里没有 PNG）")

    # 分发目录里多出来的顶层项：不是程序项也不是数据文件，提醒一下（不算失败）
    known = {item.name for item in program_entries(dist)} | set(DATA_FILE_NAMES)
    extras = [item.name for item in sorted(target.iterdir()) if item.name not in known]
    if extras and strict:
        log(f"  提示：分发目录里还有这些额外内容，未被本次部署改动：{extras}")
    return problems


def sync_data(target_dir, source_dir, log_callback=log):
    """把工程里的数据文件拷进分发目录（先备份分发目录里原有的那份）。"""
    target = Path(target_dir)
    source = Path(source_dir)
    backup = target / f"_data_backup_{time.strftime('%Y%m%d_%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    copied = []
    for path in data_files(target):
        shutil.copy2(path, backup / path.name)
    for path in data_files(source):
        shutil.copy2(path, target / path.name)
        copied.append(path.name)
    return {"backup": backup, "copied": copied}


def make_zip(target_dir):
    target = Path(target_dir)
    archive = target.parent / f"{target.name}_{time.strftime('%Y%m%d')}"
    return Path(shutil.make_archive(str(archive), "zip",
                                    root_dir=str(target.parent), base_dir=target.name))


def preflight(target_dir):
    """返回 (ok, 消息列表)。任何一条不过就不该继续。"""
    problems = []
    if not SPEC.exists():
        problems.append(f"找不到打包规格：{SPEC}")
    for name in ("gui_pyside6.py", "paths.py", "requirements.txt"):
        if not (ROOT / name).exists():
            problems.append(f"工程文件缺失：{name}（请在工程根目录运行本脚本）")
    if sys.version_info[:2] != (3, 10):
        log(f"  提示：当前 Python {sys.version.split()[0]}，打包时用的是 3.10（requirements.txt 锁定）")
    missing = missing_modules()
    if missing:
        problems.append("这些依赖不可用，先按 requirements.txt 装好：" + "、".join(missing))
    if process_running():
        problems.append(f"分发版正在运行（{EXE_NAME}），请先关掉再打包（否则文件被占用会半途失败）")
    if Path(ROOT) == Path(target_dir):
        problems.append("分发目录不能是工程目录本身")
    return (not problems), problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="一键打包：构建 → 部署 → 校验")
    parser.add_argument("--zip", action="store_true", help="部署后打一个 zip，便于分发")
    parser.add_argument("--sync-data", action="store_true",
                        help="把工程里的 4 个数据文件同步到分发目录（会先备份原有那份）")
    parser.add_argument("--build-only", action="store_true", help="只构建，不动分发目录")
    parser.add_argument("--keep-work", action="store_true", help="保留构建中间目录")
    parser.add_argument("--target", default=str(TARGET_DIR), help="分发目录（默认：工程同级）")
    parser.add_argument("--from-dist", default=None,
                        help="跳过构建，直接部署这个已有的产物目录（只更新分发目录时用）")
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()

    rule("1/5 预检")
    log(f"  工程目录：{ROOT}")
    log(f"  分发目录：{target}")
    log(f"  Python  ：{sys.version.split()[0]}（{sys.executable}）")
    ok, problems = preflight(target)
    if not ok:
        for problem in problems:
            log("  ✗ " + problem)
        return EXIT_PREFLIGHT
    log("  ✓ 工程文件、依赖与进程状态正常")

    rule("2/5 构建")
    work_root = None
    if args.from_dist:
        bundle = existing_bundle(args.from_dist)
        if bundle is None:
            log(f"  ✗ --from-dist 指向的不是构建产物（目录里要有 {EXE_NAME}）：{args.from_dist}")
            return EXIT_PREFLIGHT
        log(f"  （--from-dist）跳过构建，使用已有产物：{bundle}")
    else:
        version = ensure_pyinstaller()
        if not version:
            log("  ✗ PyInstaller 不可用；请先执行 python -m pip install pyinstaller")
            return EXIT_PREFLIGHT
        log(f"  ✓ PyInstaller {version}")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        work_root = Path(tempfile.mkdtemp(prefix=f"vf_build_{stamp}_"))
        work_dir = work_root / "build"
        dist_dir = work_root / "dist"
        log(f"  中间物目录：{work_root}")
        bundle = run_build(work_dir, dist_dir)
        if bundle is None:
            log("  ✗ 构建失败")
            return EXIT_BUILD
    files, size = dir_stats(bundle)
    png, svg = count_icons(bundle)
    log(f"  ✓ 产物就绪：{files} 个程序文件 / {size / 1024 / 1024:.1f} MB，模板 {png} 张 PNG + {svg} 张 SVG")

    if args.build_only:
        log(f"  （--build-only：产物保留在 {bundle}）")
        return EXIT_OK

    rule("3/5 部署")
    before = snapshot_data(target)
    if args.sync_data:
        if not data_files(ROOT):
            log("  提示：工程目录里没有数据文件，跳过同步")
        else:
            result = sync_data(target, ROOT)
            log(f"  已同步 {len(result['copied'])} 个数据文件；原有那份备份在 {result['backup']}")
            before = snapshot_data(target)   # 同步之后再拍快照，作为"部署不得改动"的基准
    result = deploy(bundle, target)
    if result["fresh"]:
        log("  这是首次部署：分发目录里没有数据文件，程序首次启动会用内置默认步骤")
    else:
        log(f"  已保留分发目录里原有的 {len(before)} 个数据文件")

    rule("4/5 校验")
    problems = verify(bundle, target, before)
    if problems:
        for problem in problems:
            log("  ✗ " + problem)
        log(f"  校验未通过（{len(problems)} 项）；分发目录可能不可用")
        return EXIT_VERIFY
    files, size = dir_stats(target)
    png, svg = count_icons(target)
    log(f"  ✓ 数据文件逐字节未变（{len(before)} 个）")
    log(f"  ✓ 程序项与构建产物一致，{files} 个文件 / {size / 1024 / 1024:.1f} MB")
    log(f"  ✓ 模板 {png} 张 PNG + {svg} 张 SVG，SVG 插件 / 软件 OpenGL / tkinter 均在位")

    rule("5/5 收尾")
    if args.zip:
        archive = make_zip(target)
        log(f"  ✓ 已打包：{archive}（{archive.stat().st_size / 1024 / 1024:.1f} MB）")
    if work_root is None:
        log("  （--from-dist：本次没有构建中间物）")
    elif args.keep_work:
        log(f"  保留构建中间物：{work_root}")
    else:
        shutil.rmtree(work_root, ignore_errors=True)
        log(f"  已清理构建中间物：{work_root}")
    log()
    log(f"完成。分发目录：{target}")
    log(f"双击运行：{target / EXE_NAME}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
