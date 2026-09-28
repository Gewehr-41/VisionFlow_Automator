# -*- coding: utf-8 -*-
"""执行控制：启动/停止/暂停/单步、worker 生命周期、全局停止热键、日志与诊断搬运。

从 `gui_pyside6.py` 的 `PySide6ScriptWindow` 整体外移出来的内聚职责块（14 个方法）。
窗口类通过 `ExecutionControlMixin` 混入，`self.start_script()` 之类的调用方式不变。

**测试缝**：本模块自己导入 `TaskWorker`，因此测试要拦 worker 必须替换
`execution_control.TaskWorker`（原来替换 `gui_pyside6.TaskWorker` 的做法在代码搬走后
会失效并真的启动 worker —— 与"数据落盘隔离缝"是同一类问题）。
"""
import diagnostics
from PySide6.QtCore import Qt, QThread, QTimer, Slot
from PySide6.QtWidgets import QMessageBox

import config
from core.hotkey import GlobalHotkey
from core.input import release_all_inputs
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
    @Slot()
    def start_script(self):
        self._start_worker(None)

    def _start_worker(self, start_node_id):
        if self.worker_thread and self.worker_thread.isRunning():
            return
        tasks = self.selected_tasks()
        if not tasks:
            QMessageBox.warning(self, "无法执行", "没有启用的任务步骤。")
            return
        self.execution_states.clear()
        if self.blueprint_window is not None:
            self.blueprint_window.reset_execution_states()
        window_title = self.window_combo.currentText().strip()
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
        self.status_text = "运行中"
        self.status_label.setText("状态: 运行中")
        self.append_log(f"脚本启动，共 {len(tasks)} 个启用步骤。")

        self.worker_thread = QThread(self)
        self.worker = TaskWorker(tasks, self.loop_checkbox.isChecked(), self.stop_event, self.pause_event, self.single_step_event, start_node_id)
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
            self.status_label.setText("状态: 停止中")
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
            self.status_label.setText("状态: 运行中")
        else:
            self.pause_event.set()
            self.pause_button.setText("继续")
            self.status_label.setText("状态: 已暂停")
    @Slot()
    def step_script(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.pause_event.set()
            self.single_step_event.set()
            self.pause_button.setText("继续")
            self.status_label.setText("状态: 单步执行")
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
        self._set_running_state(False)
        self._restore_config_after_run()
        self.pause_button.setText("暂停")
        if self.stop_event.is_set():
            self.status_text = "已停止"
            self.status_label.setText("状态: 已停止")
        self.append_log("脚本执行结束。")
        self._stop_stop_hotkey()
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker_thread = None
        self.worker = None
