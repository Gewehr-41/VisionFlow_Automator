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
import threading

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

import update_check


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
