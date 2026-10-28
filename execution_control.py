# -*- coding: utf-8 -*-
"""执行控制：启动/停止/暂停/单步、worker 生命周期、全局停止热键、日志与诊断搬运。

从 `gui_pyside6.py` 的 `PySide6ScriptWindow` 整体外移出来的内聚职责块（14 个方法）。
窗口类通过 `ExecutionControlMixin` 混入，`self.start_script()` 之类的调用方式不变。

**测试缝**：本模块自己导入 `TaskWorker`，因此测试要拦 worker 必须替换
`execution_control.TaskWorker`（原来替换 `gui_pyside6.TaskWorker` 的做法在代码搬走后
会失效并真的启动 worker —— 与"数据落盘隔离缝"是同一类问题）。
"""
import diagnostics
import pygetwindow as gw
from PySide6.QtCore import Qt, QThread, QTimer, Slot
from PySide6.QtWidgets import QMessageBox

import config
import preflight
from core.hotkey import GlobalHotkey
from core.input import release_all_inputs
from tasks import TASKS
from task_widgets import TaskWorker


class ExecutionControlMixin:
    """执行控制相关方法；依赖宿主窗口提供 log_edit / task_list / append_log 等。"""

    def _log_pending_warnings(self):
        """把 diagnostics 里**新产生**的诊断信息搬进日志框，返回本次搬运条数。

        线程安全：只对共享列表取快照后在 GUI 线程追加，不做跨线程 UI 操作。
        """
        warnings = diagnostics.snapshot()
        pending = warnings[self._warning_cursor:]
        self._warning_cursor = len(warnings)
        for message in pending:
            self.append_log(message)
        return len(pending)

    def append_log(self, message):
        self.log_box.appendPlainText(str(message))

    def _capture_log(self, message):
        self.append_log(message)
    def _preflight_before_run(self):
        """开跑前体检（F2）。返回 True 表示继续执行（可能已自动修复安全项）。

        检查对象是模块级 `TASKS`（不是 `selected_tasks()` 的副本）：要判断"跳转目标
        指向已禁用步骤"就必须能同时看到禁用步骤，而且"自动修复"要落到用户真实数据上。

        行为约束：**没查出问题时什么都不做**（与加这个功能之前完全一致）；
        查出问题时才多一个对话框，用户选"仍然开始"即与旧行为等价。
        """
        issues = preflight.check_tasks(TASKS)
        if not issues:
            return True
        fatal = [issue for issue in issues if issue.level == preflight.FATAL]
        fixable = [issue for issue in issues if issue.fixable]
        self.append_log(f"执行前体检：发现 {len(issues)} 个配置问题（其中 {len(fatal)} 个会导致执行失败）。")
        for issue in issues:
            self.append_log(f"  - {issue.describe()}")
        box = QMessageBox(self)
        box.setWindowTitle("执行前体检")
        box.setIcon(QMessageBox.Icon.Warning if fatal else QMessageBox.Icon.Information)
        box.setText(f"发现 {len(issues)} 个配置问题，其中 {len(fatal)} 个会导致执行失败。\n"
                    "详情见「显示详情」；也可以先自动修复能安全修复的项。")
        box.setDetailedText("\n".join(issue.describe() for issue in issues))
        keep_button = box.addButton("仍然开始", QMessageBox.ButtonRole.AcceptRole)
        fix_button = None
        if fixable:
            fix_button = box.addButton("自动修复可安全项并开始", QMessageBox.ButtonRole.ActionRole)
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_button)
        box.exec()
        clicked = box.clickedButton()
        if fix_button is not None and clicked is fix_button:
            fixes = preflight.apply_safe_fixes(TASKS)
            for line in fixes:
                self.append_log(f"  已自动修复：{line}")
            self.save_current_tasks()
            self.refresh_task_list()
            return True
        return clicked is keep_button

    @Slot()
    def start_script(self):
        self._start_worker(None)

    def _minimize_for_run(self):
        """脚本开始执行时最小化主窗口，让目标窗口在前台；记录原窗口状态。"""
        self._auto_minimized = False
        try:
            self._pre_run_window_state = self.windowState()
            if self.isMinimized():
                return
            self.showMinimized()
            self._auto_minimized = True
        except Exception:
            self._auto_minimized = False

    def _restore_after_run(self):
        """脚本结束/停止/异常时恢复主窗口（只恢复本程序主动最小化的情况）。"""
        if not getattr(self, "_auto_minimized", False):
            return
        self._auto_minimized = False
        try:
            state = getattr(self, "_pre_run_window_state", None)
            if state is not None and state & Qt.WindowState.WindowFullScreen:
                self.showFullScreen()
            elif state is not None and state & Qt.WindowState.WindowMaximized:
                self.showMaximized()
            else:
                self.showNormal()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def _target_window_exists(self, title):
        """开始执行前校验目标窗口是否真的存在。

        空标题表示"使用当前前台窗口"（config.USE_WINDOW_MODE=False），不校验。
        校验用与运行时相同的 `pygetwindow.getWindowsWithTitle()`，避免两边判断不一致。
        """
        if not title:
            return True
        try:
            return bool(gw.getWindowsWithTitle(title))
        except Exception:
            return False

    def _start_worker(self, start_node_id):
        if self.worker_thread and self.worker_thread.isRunning():
            return
        tasks = self.selected_tasks()
        if not tasks:
            QMessageBox.warning(self, "无法执行", "没有启用的任务步骤。")
            return
        # H1：目标窗口不存在就直接阻止，绝不因为"找不到"而退回前台窗口去误点
        target_title = self.window_combo.currentText().strip()
        if target_title and not self._target_window_exists(target_title):
            QMessageBox.warning(
                self,
                "目标窗口不存在",
                f"目标窗口「{target_title}」当前不存在或已关闭，脚本没有启动。\n\n"
                "请点「刷新窗口」后重新选择；如果确实想对当前前台窗口操作，请把目标窗口留空。",
            )
            self.append_log(f"启动已取消：目标窗口「{target_title}」不存在（请刷新后重选，或清空目标窗口使用前台窗口）。")
            return
        if not self._preflight_before_run():
            return
        # 体检可能自动修复过数据（改了启用状态/跳转字段），重新取一次运行副本
        tasks = self.selected_tasks()
        self.execution_states.clear()
        if self.blueprint_window is not None:
            self.blueprint_window.reset_execution_states()
        window_title = target_title
        # 运行期改写 config 是为了让引擎按指定窗口识别；但这两个字段是模块级
        # 全局，此前改完就不管了 —— 脚本停止后 config 仍指向上次的目标窗口，
        # 影响所有在此之后读取它的代码。这里记下原值，收尾时还原
        # （见 _restore_config_after_run，正常运行/异常/停止都会经过）。
        self._saved_config = (config.TARGET_WINDOW_TITLE, config.USE_WINDOW_MODE)
        config.TARGET_WINDOW_TITLE = window_title or None
        config.USE_WINDOW_MODE = bool(window_title)
        self.stop_event.clear()
        self.pause_event.clear()
        self.single_step_event.clear()
        self.completion_notified = False
        self._set_running_state(True)
        self._begin_run_progress()
        self._set_status("running")
        self.append_log(f"脚本启动，共 {len(tasks)} 个启用步骤。")

        # 脚本真正开始执行：先最小化主窗口，避免挡住目标窗口
        self._minimize_for_run()
        self.worker_thread = QThread(self)
        self.worker = TaskWorker(tasks, self.loop_checkbox.isChecked(), self.stop_event, self.pause_event, self.single_step_event, start_node_id, self._fallback_steps_for_mode())
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.execution_started.connect(self.on_execution_started)
        self.worker.execution_result.connect(self.on_execution_result)
        self.worker.completed.connect(self.on_execution_completed)
        self.worker.finished.connect(self.worker_thread.quit, Qt.ConnectionType.DirectConnection)
        self.worker_thread.finished.connect(self.on_worker_finished)
        self.worker_thread.start()
        self._start_stop_hotkey()

    def _start_stop_hotkey(self):
        """脚本开始执行时注册系统级全局停止快捷键（仅在运行期间生效）。"""
        if self._hotkey is not None and self._hotkey.active:
            return
        try:
            self._hotkey = GlobalHotkey()
            self._hotkey.start(config.STOP_HOTKEY, self._on_global_stop_hotkey)
        except Exception as exc:
            self.append_log(f"注册全局停止快捷键失败: {exc}")
            self._hotkey = None
            return
        if self._hotkey is not None and self._hotkey.active:
            self.append_log(f"已注册全局停止快捷键: {config.STOP_HOTKEY}（脚本运行期间随时可停止）")
        else:
            self.append_log(f"全局停止快捷键 {config.STOP_HOTKEY} 注册失败，可能已被其它程序占用。")

    def _stop_stop_hotkey(self):
        """脚本执行结束后注销全局停止快捷键，避免影响其它程序的按键（如复制粘贴）。"""
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None

    def _on_global_stop_hotkey(self):
        """全局快捷键回调（在后台线程执行）：立即置位停止信号，再经由信号到主线程更新界面。"""
        release_all_inputs()
        self.stop_event.set()
        self.pause_event.clear()
        self.single_step_event.set()
        self.stop_requested.emit()
    @Slot()
    def stop_script(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.stop_event.set()
            self.pause_event.clear()
            self.single_step_event.set()
            release_all_inputs()
            self._set_status("stopped", "正在停止")
            self.stop_button.setEnabled(False)
            self.append_log("正在停止脚本...")
            # 兜底：若工作线程因事件丢失等原因迟迟未退出，几秒后再次请求其退出事件循环
            QTimer.singleShot(5000, self._request_worker_quit_if_running)

    def _request_worker_quit_if_running(self):
        if self.worker_thread is not None and self.worker_thread.isRunning():
            self.worker_thread.quit()
            self.append_log("当前步骤仍在收尾，等待其结束后自动停止。")
    @Slot()
    def toggle_pause(self):
        if not self.worker_thread or not self.worker_thread.isRunning():
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_button.setText("暂停")
            self._set_status("running")
        else:
            self.pause_event.set()
            self.pause_button.setText("继续")
            self._set_status("paused")
    @Slot()
    def step_script(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.pause_event.set()
            self.single_step_event.set()
            self.pause_button.setText("继续")
            self._set_status("paused", "单步执行")
    @Slot()
    def _restore_config_after_run(self):
        """还原 _start_worker 改写的 config 字段。

        正常结束、脚本异常、用户点停止都会走到 on_worker_finished，所以它是
        唯一的收尾点。没有运行过的窗口调用它是安全的（_saved_config 为 None）。
        """
        saved = getattr(self, "_saved_config", None)
        if saved is None:
            return
        config.TARGET_WINDOW_TITLE, config.USE_WINDOW_MODE = saved
        self._saved_config = None

    def on_worker_finished(self):
        self._restore_after_run()
        self._set_running_state(False)
        self._restore_config_after_run()
        self.pause_button.setText("暂停")
        if self.stop_event.is_set():
            self._set_status("stopped")
        self.append_log("脚本执行结束。")
        self._stop_stop_hotkey()
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker_thread = None
        self.worker = None
