"""自动上传页：开关、时间、队列状态、实时进度。

页面本身不持有调度器，只发信号；调度器由主窗口统一管理，
避免关窗口后调度器跟着死掉。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    IndeterminateProgressBar,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    TimeEdit,
)

from services.config import Config, latest_pending

# 进度框最多保留的行数，再多就去日志页看
MAX_PROGRESS_LINES = 200

STEP_LABELS = {
    "queue": "队列",
    "lock": "互斥锁",
    "run": "任务",
    "browser": "浏览器",
    "login": "登录",
    "navigate": "导航",
    "dialog": "弹窗",
    "select": "选项",
    "upload": "上传",
}

STATUS_ICONS = {
    "start": "▸",
    "success": "✓",
    "fail": "✕",
    "warn": "⚠",
    "info": "·",
}


class AutoPage(QWidget):
    """自动上传页。"""

    settingsChanged = Signal(bool, str)  # enabled, "HH:MM"
    uploadRequested = Signal()

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("autoPage")
        self._config = config
        self._suppress = False  # 程序回写控件时不要反向触发信号
        self._build_ui()
        self.load_config(config)
        self.refresh_queue()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)

        layout.addWidget(SubtitleLabel("自动上传", self))
        layout.addWidget(self._build_switch_card())
        layout.addWidget(self._build_status_card())
        layout.addWidget(self._build_progress_card(), 1)

    def _build_switch_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(14)

        # ── 开关行
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(StrongBodyLabel("自动上传模式", card))
        text.addWidget(CaptionLabel("开启后程序驻留托盘，每天定时把队列里的文件传到 SDCC", card))
        row.addLayout(text)
        row.addStretch(1)

        self.enable_switch = SwitchButton(card)
        self.enable_switch.setOnText("已开启")
        self.enable_switch.setOffText("已关闭")
        self.enable_switch.checkedChanged.connect(self._emit_settings)
        row.addWidget(self.enable_switch)
        inner.addLayout(row)

        # ── 时间行
        time_row = QHBoxLayout()
        time_row.setSpacing(12)
        time_row.addWidget(BodyLabel("每日执行时间", card))

        self.time_edit = TimeEdit(card)
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.timeChanged.connect(self._emit_settings)
        time_row.addWidget(self.time_edit)

        self.next_run_label = CaptionLabel("", card)
        time_row.addWidget(self.next_run_label)
        time_row.addStretch(1)
        inner.addLayout(time_row)

        return card

    def _build_status_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(12)

        self.queue_label = StrongBodyLabel("待上传队列：检查中…", card)
        inner.addWidget(self.queue_label)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.upload_btn = PrimaryPushButton("立即上传一次", card)
        self.upload_btn.clicked.connect(self.uploadRequested)
        row.addWidget(self.upload_btn)

        self.refresh_btn = PushButton("刷新队列", card)
        self.refresh_btn.clicked.connect(self.refresh_queue)
        row.addWidget(self.refresh_btn)
        row.addStretch(1)
        inner.addLayout(row)

        self.progress_bar = IndeterminateProgressBar(card)
        self.progress_bar.hide()
        inner.addWidget(self.progress_bar)

        return card

    def _build_progress_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 14, 20, 16)
        inner.setSpacing(8)

        inner.addWidget(CaptionLabel("实时进度", card))

        self.progress_text = PlainTextEdit(card)
        self.progress_text.setReadOnly(True)
        self.progress_text.setMaximumBlockCount(MAX_PROGRESS_LINES)
        self.progress_text.setPlaceholderText("还没有运行过任务")
        inner.addWidget(self.progress_text, 1)

        return card

    # -------------------------------------------------------------- 对外

    def load_config(self, config: Config) -> None:
        """把配置回写到控件，不触发 settingsChanged。"""
        self._config = config
        self._suppress = True
        try:
            self.enable_switch.setChecked(config.auto_upload_enabled)
            hour, minute = config.schedule_hour_minute
            self.time_edit.setTime(QTime(hour, minute))
        finally:
            self._suppress = False

    def refresh_queue(self) -> None:
        pending = latest_pending()
        if pending is None:
            self.queue_label.setText("待上传队列：空（到点会跳过）")
            self.upload_btn.setEnabled(False)
            return
        stamp = datetime.fromtimestamp(pending.stat().st_mtime).strftime("%m-%d %H:%M")
        self.queue_label.setText(f"待上传队列：{pending.name}（{stamp} 生成）")
        self.upload_btn.setEnabled(True)

    def set_next_run(self, next_run: Optional[datetime]) -> None:
        if next_run is None:
            self.next_run_label.setText("自动上传未开启")
        else:
            self.next_run_label.setText(f"下次执行：{next_run:%Y-%m-%d %H:%M}")

    def set_running(self, running: bool) -> None:
        self.upload_btn.setEnabled(not running and latest_pending() is not None)
        self.upload_btn.setText("上传中…" if running else "立即上传一次")
        self.progress_bar.setVisible(running)
        if running:
            self.progress_text.clear()

    def append_progress(self, step: str, status: str, message: str) -> None:
        icon = STATUS_ICONS.get(status, "·")
        label = STEP_LABELS.get(step, step)
        stamp = datetime.now().strftime("%H:%M:%S")
        self.progress_text.appendPlainText(f"{stamp}  {icon} [{label}] {message}")

    # -------------------------------------------------------------- 内部

    def _emit_settings(self, *_args) -> None:
        if self._suppress:
            return
        self.settingsChanged.emit(
            self.enable_switch.isChecked(), self.time_edit.time().toString("HH:mm")
        )
