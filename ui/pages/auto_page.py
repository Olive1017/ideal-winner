"""运行页：自动准备开关、时间、队列状态、实时进度。

页面本身不持有调度器，只发信号；调度器由主窗口统一管理，
避免关窗口后调度器跟着死掉。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QScrollArea,
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

from services.config import Config, outbox_files, sdcc_orders_dir, shell_orders_dir
from ui.dialogs.order_preview_dialog import OrderPreviewDialog

# 进度框最多保留的行数，再多就去日志页看
MAX_PROGRESS_LINES = 200
# 待上传列表最多展示的文件数，更多去文件夹里看
MAX_PENDING_ROWS = 12

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


class AutoPage(QScrollArea):
    """运行页。内容超出窗口高度时可滚动——窗口矮了不再把卡片和按钮压扁。"""

    settingsChanged = Signal(bool, str)  # enabled, "HH:MM"
    uploadRequested = Signal()  # 开始处理：队列最新直接传，队列空则一条龙
    reexportRequested = Signal()  # 强制拉最新：无视队列重新导出
    uploadFileRequested = Signal(str)  # 上传待上传列表里指定的某一份

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("autoPage")

        # 只增加滚动能力，不改变原来的页面布局（和设置页同款处理）
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._config = config
        self._suppress = False  # 程序回写控件时不要反向触发信号
        self._row_upload_btns: List[PushButton] = []

        self._content = QWidget()
        self.setWidget(self._content)

        self._build_ui()
        self.load_config(config)
        self.refresh_queue()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """每次切到这个页面都重新扫一遍文件夹，替代原来的「刷新订单」按钮。"""
        super().showEvent(event)
        self.refresh_queue()

    # ------------------------------------------------------------------ UI

    @staticmethod
    def _btn(button, min_width: int = 0):
        """统一按钮尺寸：不给最小尺寸时，Fluent 按钮在部分 Windows 缩放下会被压扁、文字裁切。"""
        button.setMinimumHeight(36)
        if min_width:
            button.setMinimumWidth(min_width)
        return button

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)

        layout.addWidget(SubtitleLabel("订单处理", self._content))
        layout.addWidget(self._build_switch_card())
        layout.addWidget(self._build_status_card())
        layout.addWidget(self._build_pending_card())
        layout.addWidget(self._build_progress_card(), 1)

    def _build_switch_card(self) -> CardWidget:
        card = CardWidget(self._content)
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
        card = CardWidget(self._content)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(12)

        self.queue_label = StrongBodyLabel("待上传订单：检查中…", card)
        inner.addWidget(self.queue_label)

        # 点了按钮会发生什么，直接写在脸上，不让用户猜
        self.plan_label = CaptionLabel("", card)
        self.plan_label.setWordWrap(True)
        inner.addWidget(self.plan_label)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.upload_btn = self._btn(PrimaryPushButton("开始处理", card), 120)
        self.upload_btn.clicked.connect(self.uploadRequested)
        row.addWidget(self.upload_btn)

        self.reexport_btn = self._btn(PushButton("强制拉最新", card), 120)
        self.reexport_btn.clicked.connect(self.reexportRequested)
        row.addWidget(self.reexport_btn)
        row.addStretch(1)
        inner.addLayout(row)

        self.progress_bar = IndeterminateProgressBar(card)
        self.progress_bar.hide()
        inner.addWidget(self.progress_bar)

        return card

    def _build_pending_card(self) -> CardWidget:
        card = CardWidget(self._content)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(8)

        inner.addWidget(StrongBodyLabel("待上传订单", card))

        # 文件行都放进这个容器，refresh_queue 每次重建
        self.pending_rows = QWidget(card)
        self.pending_rows_layout = QVBoxLayout(self.pending_rows)
        self.pending_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.pending_rows_layout.setSpacing(6)
        inner.addWidget(self.pending_rows)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.open_folder_btn = self._btn(PushButton("打开SDCC订单", card), 130)
        self.open_folder_btn.clicked.connect(self._open_sdcc_folder)
        row.addWidget(self.open_folder_btn)

        self.open_shell_folder_btn = self._btn(PushButton("打开壳牌订单", card), 130)
        self.open_shell_folder_btn.clicked.connect(self._open_shell_folder)
        row.addWidget(self.open_shell_folder_btn)
        row.addStretch(1)
        inner.addLayout(row)

        return card

    def _build_pending_row(self, path: Path) -> QWidget:
        """待上传列表的一行：文件名 + 时间/大小 + 行内「上传」「查看」按钮。"""
        row_widget = QWidget(self.pending_rows)
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(BodyLabel(path.name, row_widget))
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        size_kb = max(1, path.stat().st_size // 1024)
        text.addWidget(CaptionLabel(f"{mtime} · {size_kb} KB · 待上传", row_widget))
        row.addLayout(text, 1)

        upload_btn = PushButton("上传", row_widget)
        upload_btn.setMinimumHeight(30)
        upload_btn.setMinimumWidth(70)
        upload_btn.clicked.connect(
            lambda _checked=False, p=str(path): self.uploadFileRequested.emit(p)
        )
        self._row_upload_btns.append(upload_btn)
        row.addWidget(upload_btn)

        preview_btn = PushButton("查看", row_widget)
        preview_btn.setMinimumHeight(30)
        preview_btn.setMinimumWidth(70)
        preview_btn.clicked.connect(
            lambda _checked=False, p=str(path): self.show_order_preview(p)
        )
        row.addWidget(preview_btn)

        return row_widget

    def _build_progress_card(self) -> CardWidget:
        card = CardWidget(self._content)
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

        # 重建待上传列表的行
        while self.pending_rows_layout.count():
            item = self.pending_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._row_upload_btns.clear()

        if not files:
            self.queue_label.setText("待上传订单：暂无")
            self.plan_label.setText(
                "点「开始处理」会从壳牌导出订单、转换成 SDCC 格式后再上传，一条龙。"
            )
            self.pending_rows_layout.addWidget(
                CaptionLabel("暂无待上传订单", self.pending_rows)
            )
        else:
            latest = files[0]
            self.queue_label.setText(f"待上传订单：{len(files)} 个（最新：{latest.name}）")
            self.plan_label.setText(
                "点「开始处理」直接上传最新一份；不信任队列就点「强制拉最新」重新导出。"
            )
            for path in files[:MAX_PENDING_ROWS]:
                self.pending_rows_layout.addWidget(self._build_pending_row(path))

        self.upload_btn.setEnabled(True)

    def set_next_run(self, next_run: Optional[datetime]) -> None:
        if next_run is None:
            self.next_run_label.setText("自动准备未开启")
        else:
            self.next_run_label.setText(f"下次执行：{next_run:%Y-%m-%d %H:%M}")

    def set_running(self, running: bool) -> None:
        self.upload_btn.setEnabled(not running)
        self.reexport_btn.setEnabled(not running)
        for btn in self._row_upload_btns:
            btn.setEnabled(not running)
        self.upload_btn.setText("处理中…" if running else "开始处理")
        self.progress_bar.setVisible(running)
        if running:
            self.progress_text.clear()

    def append_progress(self, step: str, status: str, message: str) -> None:
        icon = STATUS_ICONS.get(status, "·")
        label = STEP_LABELS.get(step, step)
        stamp = datetime.now().strftime("%H:%M:%S")
        self.progress_text.appendPlainText(f"{stamp}  {icon} [{label}] {message}")

    def show_order_preview(self, path: Optional[str] = None) -> None:
        """预览订单文件；不传路径时看最新一份。"""
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

    def _open_sdcc_folder(self) -> None:
        self._open_folder(sdcc_orders_dir())

    def _open_shell_folder(self) -> None:
        self._open_folder(shell_orders_dir())

    @staticmethod
    def _open_folder(folder: Path) -> None:
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
