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
import fnmatch
import os
import shutil
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


def build_update_helper(install_dir, staged_dir, pid, log_dir=None, zip_path=None):
    """生成"退出后替换并重启"的 PowerShell 助手，返回 .ps1 路径。

    - 用 `Get-Process` 轮询等待旧进程退出（带 120 秒超时，不会无限卡住）；
    - 只替换 exe 和 _internal，**不动 saved_*.json 用户数据**；
    - 替换前把旧程序移到 `_backup_update_*`，失败自动回滚；
    - 整个过程由隐藏的 PowerShell 窗口执行，用户不会看到终端。
    """
    install = Path(install_dir).resolve()
    staged = Path(staged_dir).resolve()
    log_dir = Path(log_dir or tempfile.gettempdir()).resolve()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = install / f"{UPDATE_BACKUP_PREFIX}{stamp}"
    script = log_dir / f"vf_update_{stamp}_{pid}.ps1"
    log = log_dir / f"vf_update_{stamp}_{pid}.log"
    downloaded_zip = str(zip_path) if zip_path else ""

    def ps(value):
        return "'" + str(value).replace("'", "''") + "'"

    d = "$DST"
    b = "$BACKUP"
    src = "$SRC"
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$PID_TO_WAIT = {int(pid)}",
        f"$SRC = {ps(staged)}",
        f"$DST = {ps(install)}",
        f"$BACKUP = {ps(backup)}",
        f"$LOG = {ps(log)}",
        f"$ZIP = {ps(downloaded_zip)}",
        "function Write-UpLog([string]$text) {",
        "  Add-Content -LiteralPath $LOG -Value ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $text)",
        "}",
        "Write-UpLog 'wait for app exit'",
        "$deadline = (Get-Date).AddSeconds(120)",
        "while ($true) {",
        "  $proc = Get-Process -Id $PID_TO_WAIT -ErrorAction SilentlyContinue",
        "  if (-not $proc) { break }",
        "  if ((Get-Date) -gt $deadline) {",
        "    Write-UpLog 'timeout waiting for app exit; abort update'",
        "    exit 2",
        "  }",
        "  Start-Sleep -Milliseconds 500",
        "}",
        "Write-UpLog 'backup old program'",
        "New-Item -ItemType Directory -Path $BACKUP -Force | Out-Null",
        "function Move-Into-Backup([string]$name) {",
        "  $source = Join-Path $DST $name",
        "  if (Test-Path -LiteralPath $source) {",
        "    Move-Item -LiteralPath $source -Destination $BACKUP -Force -ErrorAction Stop",
        "  }",
        "}",
        "function Restore-From-Backup([string]$name) {",
        "  $source = Join-Path $BACKUP $name",
        "  if (Test-Path -LiteralPath $source) {",
        "    Move-Item -LiteralPath $source -Destination $DST -Force -ErrorAction SilentlyContinue",
        "  }",
        "}",
        f"try {{",
        f"  Move-Into-Backup {ps(UPDATE_EXE)}",
        f"  Move-Into-Backup {ps('_internal')}",
        "  Write-UpLog 'copy new program'",
        f"  & robocopy $SRC {d} /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP *>> $LOG",
        "  if ($LASTEXITCODE -ge 8) { throw ('robocopy failed: ' + $LASTEXITCODE) }",
        f"  if (-not (Test-Path -LiteralPath (Join-Path $DST {ps(UPDATE_EXE)}))) {{ throw 'new exe missing after copy' }}",
        "  Write-UpLog 'restart updated app'",
        f"  Start-Process -FilePath (Join-Path $DST {ps(UPDATE_EXE)}) -WorkingDirectory $DST",
        f"  Remove-Item -LiteralPath $SRC -Recurse -Force -ErrorAction SilentlyContinue",
        f"  if ($ZIP -and (Test-Path -LiteralPath $ZIP)) {{ Remove-Item -LiteralPath $ZIP -Force -ErrorAction SilentlyContinue }}",
        "  exit 0",
        "} catch {",
        "  Write-UpLog ('update failed: ' + $_)",
        f"  Remove-Item -LiteralPath (Join-Path $DST {ps(UPDATE_EXE)}) -Force -ErrorAction SilentlyContinue",
        f"  Remove-Item -LiteralPath (Join-Path $DST {ps('_internal')}) -Recurse -Force -ErrorAction SilentlyContinue",
        f"  Restore-From-Backup {ps(UPDATE_EXE)}",
        f"  Restore-From-Backup {ps('_internal')}",
        "  Write-UpLog 'restart old app'",
        f"  Start-Process -FilePath (Join-Path $DST {ps(UPDATE_EXE)}) -WorkingDirectory $DST -ErrorAction SilentlyContinue",
        "  exit 1",
        "}",
        "",
    ]
    # Windows PowerShell 5.1 默认按 ANSI 读无 BOM 脚本，中文安装路径会乱码；
    # 用 UTF-8 BOM 写入，保证 "打包版" 这种路径能正确解析。
    script.write_bytes(b"\xef\xbb\xbf" + ("\r\n".join(lines)).encode("utf-8"))
    return str(script)


def cleanup_update_leftovers(root=None, max_age_hours=24):
    """清理 %TEMP% 里过期的更新临时文件（下载包/助手脚本/日志/解压目录）。

    只删自己命名规则、且超过 max_age_hours 的项；刚下载/刚取消的更新包不会被误删。
    返回删除数量。
    """
    base = Path(root or tempfile.gettempdir())
    cutoff = time.time() - max_age_hours * 3600
    patterns = ("vf_update_*.zip", "vf_update_*.log", "vf_update_*.ps1", "vf_update_*.bat")
    removed = 0
    try:
        for item in base.iterdir():
            if not item.is_file():
                continue
            if not any(fnmatch.fnmatch(item.name, pattern) for pattern in patterns):
                continue
            if item.stat().st_mtime >= cutoff:
                continue
            item.unlink()
            removed += 1
        for item in base.glob("vf_update_stage_*"):
            if item.is_dir() and item.stat().st_mtime < cutoff:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
    except OSError:
        pass
    return removed


def launch_update_helper(helper_path):
    """用隐藏窗口的 PowerShell 启动更新助手，随后当前程序退出。"""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", str(helper_path)],
        cwd=str(Path(helper_path).parent),
        creationflags=creationflags,
        close_fds=True,
    )


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
