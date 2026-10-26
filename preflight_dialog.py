# -*- coding: utf-8 -*-
"""执行前体检的结果对话框（全中文，自带「显示详情 / 收起详情」折叠）。

为什么不用 QMessageBox.setDetailedText
--------------------------------------
那个"展开/收起详细选项"的按钮文字来自 Qt 自带的翻译文件（`qtbase_zh_CN.qm`）。
本项目没有加载 Qt 翻译器，所以它固定显示英文 "Show Details..." / "Hide Details..."
（所有者 2026-10-26 反馈）。依赖翻译文件还有第二个坑：打包产物里不一定包含
`PySide6/translations`，源码版是中文、打包版又变回英文。

这里用最朴素的 QDialog + QPushButton + QPlainTextEdit 自绘一份：文字全部写死中文，
不依赖任何翻译资源，源码运行与打包运行行为一致。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

import preflight


class PreflightDialog(QDialog):
    """显示体检问题清单，返回用户选择：继续 / 自动修复后继续 / 取消。"""

    CONTINUE = "continue"
    FIX = "fix"
    CANCEL = "cancel"

    def __init__(self, issues, parent=None):
        super().__init__(parent)
        self.setWindowTitle("执行前体检")
        self.resize(720, 460)
        self.setMinimumSize(520, 300)
        self._issues = list(issues or [])
        self._decision = self.CANCEL
        fatal = [issue for issue in self._issues if issue.level == preflight.FATAL]
        fixable = [issue for issue in self._issues if issue.fixable]

        layout = QVBoxLayout(self)
        self.summary_label = QLabel(
            f"发现 {len(self._issues)} 个配置问题，其中 {len(fatal)} 个会导致执行失败。\n"
            "可以先自动修复能安全修复的项，也可以忽略问题继续执行，或取消本次执行。"
        )
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.details_button = QPushButton("显示详情")
        self.details_button.setCheckable(True)
        self.details_button.setToolTip("展开/收起全部问题清单")
        self.details_button.toggled.connect(self._on_details_toggled)
        layout.addWidget(self.details_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setPlainText("\n".join(issue.describe() for issue in self._issues))
        self.details_view.setVisible(False)
        layout.addWidget(self.details_view, 1)

        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self._on_cancel)
        buttons.addWidget(self.cancel_button)
        self.continue_button = QPushButton("仍然开始")
        self.continue_button.clicked.connect(self._on_continue)
        buttons.addWidget(self.continue_button)
        self.fix_button = None
        if fixable:
            self.fix_button = QPushButton("自动修复可安全项并开始")
            self.fix_button.clicked.connect(self._on_fix)
            buttons.addWidget(self.fix_button)
        layout.addLayout(buttons)
        # 默认动作是"取消"：误按回车不会带着已知问题开跑
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()

    # ---------- 交互 ----------

    def _on_details_toggled(self, checked):
        self.details_view.setVisible(bool(checked))
        self.details_button.setText("收起详情" if checked else "显示详情")

    def _on_cancel(self):
        self._decision = self.CANCEL
        self.reject()

    def _on_continue(self):
        self._decision = self.CONTINUE
        self.accept()

    def _on_fix(self):
        self._decision = self.FIX
        self.accept()

    # ---------- 结果 ----------

    def decision(self):
        """用户选择：CONTINUE / FIX / CANCEL（直接关窗视为 CANCEL）。"""
        return self._decision
