"""运行页：自动上传开关、时间、队列状态、实时进度。

页面本身不持有调度器，只发信号；调度器由主窗口统一管理，
避免关窗口后调度器跟着死掉。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
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

from services.config import Config, outbox_files, sdcc_orders_dir
from ui.dialogs.order_preview_dialog import OrderPreviewDialog

# 进度框最多保留的行数，再多就去日志页看
MAX_PROGRESS_LINES = 200

STEP_LABELS = {
    "queue": "队列",
    "lock": "互斥锁",
    "export": "导出",
    "convert": "转换",
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
    """运行页。"""

    settingsChanged = Signal(bool, str)  # enabled, "HH:MM"
    uploadRequested = Signal()
    reexportRequested = Signal()

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

        layout.addWidget(SubtitleLabel("订单处理", self))
        layout.addWidget(self._build_switch_card())
        layout.addWidget(self._build_status_card())
        layout.addWidget(self._build_pending_card())
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
        text.addWidget(StrongBodyLabel("自动准备订单", card))
        text.addWidget(
            CaptionLabel(
                "开启后程序驻留托盘，每天定时从壳牌导出订单并转换成 SDCC 格式，不自动上传 SDCC。",
                card,
            )
        )
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

        self.queue_label = StrongBodyLabel("待上传订单：检查中…", card)
        inner.addWidget(self.queue_label)

        # 本次执行会不会从壳牌导出，直接写在脸上，不让用户猜
        self.plan_label = CaptionLabel("", card)
        self.plan_label.setWordWrap(True)
        inner.addWidget(self.plan_label)

        self.pending_file_combo = QComboBox(card)
        self.pending_file_combo.setPlaceholderText("请选择待上传订单")
        self.pending_file_combo.setEnabled(False)
        self.pending_file_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        inner.addWidget(self.pending_file_combo)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.upload_btn = PrimaryPushButton("立即准备订单", card)
        self.upload_btn.clicked.connect(self.uploadRequested)
        row.addWidget(self.upload_btn)

        self.preview_btn = PushButton("查看订单", card)
        self.preview_btn.clicked.connect(self.show_order_preview)
        row.addWidget(self.preview_btn)

        self.reexport_btn = PushButton("重新导出壳牌订单", card)
        self.reexport_btn.clicked.connect(self.reexportRequested)
        row.addWidget(self.reexport_btn)

        self.refresh_btn = PushButton("刷新订单", card)
        self.refresh_btn.clicked.connect(self.refresh_queue)
        row.addWidget(self.refresh_btn)
        row.addStretch(1)
        inner.addLayout(row)

        self.progress_bar = IndeterminateProgressBar(card)
        self.progress_bar.hide()
        inner.addWidget(self.progress_bar)

        return card

    def _build_pending_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(12)

        inner.addWidget(StrongBodyLabel("待上传订单", card))

        self.pending_list = PlainTextEdit(card)
        self.pending_list.setReadOnly(True)
        self.pending_list.setMaximumBlockCount(80)
        self.pending_list.setPlaceholderText("暂无待上传订单")
        self.pending_list.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.pending_list.setMinimumHeight(120)
        inner.addWidget(self.pending_list, 1)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.upload_pending_btn = PrimaryPushButton("上传", card)
        self.upload_pending_btn.clicked.connect(self.uploadRequested)
        row.addWidget(self.upload_pending_btn)

        self.preview_file_btn = PushButton("查看订单", card)
        self.preview_file_btn.clicked.connect(self.show_order_preview)
        row.addWidget(self.preview_file_btn)

        self.open_folder_btn = PushButton("打开文件夹", card)
        self.open_folder_btn.clicked.connect(self._open_sdcc_folder)
        row.addWidget(self.open_folder_btn)
        row.addStretch(1)
        inner.addLayout(row)

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
        self.progress_text.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.progress_text.setMinimumHeight(120)
        inner.addWidget(self.progress_text, 1)

        return card

    # -------------------------------------------------------------- 对外

    def load_config(self, config: Config) -> None:
        """把配置回写到控件，不触发 settingsChanged。"""
        self._config = config
        self._suppress = True
        try:
            self.enable_switch.setChecked(config.auto_prepare_enabled)
            hour, minute = config.schedule_hour_minute
            self.time_edit.setTime(QTime(hour, minute))
        finally:
            self._suppress = False

    def refresh_queue(self) -> None:
        files = outbox_files()
        self.pending_file_combo.blockSignals(True)
        self.pending_file_combo.clear()
        self.pending_file_combo.setEnabled(bool(files))
        if not files:
            self.queue_label.setText("待上传订单：暂无")
            self.plan_label.setText(
                "最近一次订单准备：等待从壳牌导出并转换为 SDCC 订单。"
            )
            self.pending_list.setPlainText("暂无待上传订单")
            self.pending_file_combo.addItem("暂无待上传订单")
            self._selected_pending_path = None
        else:
            latest = files[0]
            self._selected_pending_path = str(latest)
            for path in files:
                label = f"{path.name}  |  {datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')}"
                self.pending_file_combo.addItem(label, str(path))
                if str(path) == self._selected_pending_path:
                    self.pending_file_combo.setCurrentIndex(self.pending_file_combo.count() - 1)
            stamp = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%m-%d %H:%M")
            self.queue_label.setText(f"待上传订单：{len(files)} 个（最新：{latest.name}）")
            self.plan_label.setText(
                "最近一次订单准备：已生成 SDCC 订单，等待人工登录上传。"
            )
            lines = []
            for path in files[:12]:
                mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                size_kb = max(1, path.stat().st_size // 1024)
                lines.append(f"{path.name}\n{mtime}\n{size_kb} KB\n待上传")
            self.pending_list.setPlainText("\n\n".join(lines))
        self.pending_file_combo.blockSignals(False)

        self.upload_btn.setEnabled(True)
        self.upload_pending_btn.setEnabled(bool(files))
        self.preview_btn.setEnabled(bool(files))
        self.preview_file_btn.setEnabled(bool(files))

    def set_next_run(self, next_run: Optional[datetime]) -> None:
        if next_run is None:
            self.next_run_label.setText("自动准备未开启")
        else:
            self.next_run_label.setText(f"下次执行：{next_run:%Y-%m-%d %H:%M}")

    def set_running(self, running: bool) -> None:
        self.upload_btn.setEnabled(not running)
        self.reexport_btn.setEnabled(not running)
        self.upload_pending_btn.setEnabled(not running and bool(outbox_files()))
        self.upload_btn.setText("准备中…" if running else "立即准备订单")
        self.progress_bar.setVisible(running)
        if running:
            self.progress_text.clear()

    def append_progress(self, step: str, status: str, message: str) -> None:
        icon = STATUS_ICONS.get(status, "·")
        label = STEP_LABELS.get(step, step)
        stamp = datetime.now().strftime("%H:%M:%S")
        self.progress_text.appendPlainText(f"{stamp}  {icon} [{label}] {message}")

    def show_order_preview(self) -> None:
        path = self._selected_pending_path
        if not path:
            files = outbox_files()
            if not files:
                return
            path = str(files[0])
        try:
            dialog = OrderPreviewDialog(path, self)
            dialog.exec()
        except Exception:  # pragma: no cover
            return

    @property
    def selected_pending_file(self):
        if not getattr(self, "_selected_pending_path", None):
            return None
        return Path(self._selected_pending_path)

    def _open_sdcc_folder(self) -> None:
        folder = sdcc_orders_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                os.system(f'open "{folder}"')
        except Exception:  # pragma: no cover
            pass

    # -------------------------------------------------------------- 内部

    def _emit_settings(self, *_args) -> None:
        if self._suppress:
            return
        self.settingsChanged.emit(
            self.enable_switch.isChecked(), self.time_edit.time().toString("HH:mm")
        )
