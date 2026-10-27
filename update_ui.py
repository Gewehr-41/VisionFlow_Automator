# -*- coding: utf-8 -*-
"""自动更新检查的界面部分：后台线程检查 + 结果提示。

为什么放这里而不是 gui_pyside6.py
--------------------------------
`gui_pyside6.py` 已经 2000 行；更新检查是"可选旁路功能"，出错也不能影响主流程，
单独一个模块既方便测（不构造主窗口就能验证提示逻辑），也不会让主窗口继续膨胀。

线程模型
--------
检查在 `threading.Thread(daemon=True)` 里跑（网络请求不能卡 GUI 线程），结果通过
`UpdateCheckRunner.finished` 信号回到 GUI 线程——Qt 的跨线程信号默认排队投递，
所以在主线程里弹对话框是安全的。
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

import update_check

UPDATE_EXE = "VisionFlow_Automator.exe"      # 与打包产物保持一致
UPDATE_BACKUP_PREFIX = "_backup_update_"     # 自动更新前的程序备份目录（不含用户数据）


class UpdateCheckRunner(QObject):
    """在后台线程里跑一次检查，完成后发 `finished(UpdateInfo)`。"""

    finished = Signal(object)

    def start(self, current=None, **kwargs):
        def _work():
            try:
                result = update_check.check_for_update(current=current, **kwargs)
            except Exception as exc:      # 双保险：检查失败绝不能带崩界面
                result = update_check.UpdateInfo(
                    False, "", current or "", "", "", f"检查更新失败：{exc}")
            self.finished.emit(result)

        threading.Thread(target=_work, daemon=True, name="vf-update-check").start()


def can_auto_update(result):
    """是否支持一键自动更新：打包版（frozen）且 Release 里有 zip 资产。"""
    return bool(getattr(sys, "frozen", False) and getattr(result, "asset_url", ""))


def ask_update_choice(parent, result):
    """发现新版本时的三选一：自动更新 / 打开发布页 / 稍后。"""
    box = QMessageBox(parent)
    box.setWindowTitle("发现新版本")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(f"发现新版本 {result.remote_version}（当前 {result.current_version}）。\n\n"
                "可以自动下载并安装更新，也可以打开发布页手动查看。")
    auto_button = box.addButton("自动更新", QMessageBox.ButtonRole.AcceptRole)
    page_button = box.addButton("打开发布页", QMessageBox.ButtonRole.ActionRole)
    box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(auto_button)
    box.exec()
    clicked = box.clickedButton()
    if clicked is auto_button:
        return "auto"
    if clicked is page_button:
        return "page"
    return None


class UpdateDownloadRunner(QObject):
    """后台下载更新 zip，不阻塞界面。"""

    progress = Signal(int, int)
    finished = Signal(object)      # {"path": str|None, "error": str|None}

    def start(self, url, destination, proxy=None):
        def _work():
            try:
                path = update_check.download_asset(
                    url, destination, proxy=proxy,
                    progress=lambda downloaded, total: self.progress.emit(downloaded, total),
                    max_bytes=2048 * 1024 * 1024,
                )
                self.finished.emit({"path": path, "error": None})
            except Exception as exc:
                self.finished.emit({"path": None, "error": str(exc)})

        threading.Thread(target=_work, daemon=True, name="vf-update-download").start()


def locate_update_root(extract_root):
    """更新 zip 解压后定位程序根目录（zip 可能带一层顶层文件夹）。"""
    root = Path(extract_root)
    if (root / UPDATE_EXE).is_file():
        return root
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / UPDATE_EXE).is_file():
            return child
    raise RuntimeError(f"更新包结构不正确（找不到 {UPDATE_EXE}）")


def extract_update_zip(zip_path, extract_root):
    """校验并解压更新 zip；返回程序根目录。失败抛 RuntimeError。"""
    target_root = Path(extract_root)
    target_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as package:
            bad = package.testzip()
            if bad:
                raise RuntimeError(f"更新包损坏（{bad}）")
            for name in package.namelist():
                normalized = name.replace("\\", "/")
                parts = normalized.split("/")
                if normalized.startswith("/") or ".." in parts or ":" in parts[0]:
                    raise RuntimeError(f"更新包包含非法路径：{name}")
            package.extractall(target_root)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"更新包不是有效 zip：{exc}") from exc
    root = locate_update_root(target_root)
    if not (root / "_internal").is_dir():
        raise RuntimeError("更新包缺少 _internal 运行库目录")
    return root


def build_update_helper(install_dir, staged_dir, pid, log_dir=None):
    """生成"退出后替换并重启"的批处理助手，返回 .bat 路径。

    只替换 exe 和 _internal，**不动 saved_*.json 用户数据**；替换前把旧程序移到
    `_backup_update_*` 备份，失败会自动回滚。
    """
    install = Path(install_dir).resolve()
    staged = Path(staged_dir).resolve()
    log_dir = Path(log_dir or tempfile.gettempdir()).resolve()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = install / f"{UPDATE_BACKUP_PREFIX}{stamp}"
    bat = log_dir / f"vf_update_{stamp}_{pid}.bat"
    log = log_dir / f"vf_update_{stamp}_{pid}.log"

    def quoted(expr):
        return '"' + expr + '"'

    dst_exe = quoted("%DST%" + "\\" + UPDATE_EXE)
    backup_exe = quoted("%BACKUP%" + "\\" + UPDATE_EXE)
    dst_internal = quoted("%DST%" + "\\_internal")
    backup_internal = quoted("%BACKUP%" + "\\_internal")
    dst_internal_dir = "%DST%" + "\\_internal"
    backup_internal_dir = "%BACKUP%" + "\\_internal"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "SRC=' + str(staged) + '"',
        'set "DST=' + str(install) + '"',
        'set "BACKUP=' + str(backup) + '"',
        'set "LOG=' + str(log) + '"',
        'echo [update] wait for app exit >> "%LOG%"',
        ":wait",
        'tasklist /FI "PID eq ' + str(pid) + '" 2>nul | find " ' + str(pid) + ' " >nul',
        "if not errorlevel 1 (",
        "  timeout /t 1 /nobreak >nul",
        "  goto wait",
        ")",
        'echo [update] backup old program >> "%LOG%"',
        'if not exist "%BACKUP%" mkdir "%BACKUP%"',
        "move /y " + dst_exe + " \"%BACKUP%\" >> \"%LOG%\" 2>&1",
        'if exist ' + dst_internal + ' move /y ' + dst_internal + ' "%BACKUP%" >> "%LOG%" 2>&1',
        'echo [update] copy new program >> "%LOG%"',
        'robocopy "%SRC%" "%DST%" /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >> "%LOG%" 2>&1',
        "if errorlevel 8 goto restore",
        'if not exist "%DST%\\' + UPDATE_EXE + '" goto restore',
        'start "" ' + dst_exe,
        'rmdir /s /q "%SRC%"',
        "exit /b 0",
        ":restore",
        'echo [update] copy failed, restoring backup >> "%LOG%"',
        "move /y " + backup_exe + " \"%DST%\" >> \"%LOG%\" 2>&1",
        'if exist ' + backup_internal + ' move /y ' + backup_internal + ' "%DST%" >> "%LOG%" 2>&1',
        'start "" ' + dst_exe,
        "exit /b 1",
        "",
    ]
    # 用 write_bytes 避免 Windows 文本模式把 \n 再翻译一次（生成空行/\r\r\n）
    bat.write_bytes(("\r\n".join(lines)).encode("utf-8"))
    return str(bat)


def launch_update_helper(helper_path):
    """脱离当前进程启动更新助手（当前程序随后退出）。"""
    creationflags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= getattr(subprocess, name, 0)
    subprocess.Popen(["cmd.exe", "/c", str(helper_path)],
                     cwd=str(Path(helper_path).parent),
                     creationflags=creationflags,
                     close_fds=True)


def update_message(result):
    """拼提示文案（抽出来便于测试断言）。"""
    if result.error:
        return ""
    return (f"发现新版本 {result.remote_version}（当前版本 {result.current_version}）。\n\n"
            f"是否打开下载页面？\n{result.download_url}")


def notify_update(parent, result):
    """有新版本时询问是否打开下载页面；返回 True 表示打开了浏览器。"""
    if not result.has_update or not result.download_url:
        return False
    message = update_message(result)
    if not message:
        return False
    answer = QMessageBox.question(
        parent, "发现新版本", message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return False
    QDesktopServices.openUrl(QUrl(result.download_url))
    return True


def report_check_failure(parent, result):
    """手动检查失败时的提示（自动检查不弹，只在日志里留一句）。"""
    QMessageBox.information(parent, "检查更新失败",
                            f"{result.error}\n\n"
                            "如果本机访问 GitHub 需要代理，可点主界面底部状态栏的「设置」填写代理地址"
                            "（留空=自动探测环境变量 / Windows 系统代理 / 直连），保存后再试。")
