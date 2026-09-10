"""运行页：数据文件夹、自动准备开关、时间、队列状态、实时进度。

页面本身不持有调度器，只发信号；调度器由主窗口统一管理，
避免关窗口后调度器跟着死掉。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtWidgets import (
    QFileDialog,
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
    InfoBar,
    InfoBarPosition,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    TimeEdit,
)

from services.config import (
    Config,
    data_root,
    outbox_files,
    work_dir,
)

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


class AutoPage(QScrollArea):
    """运行页。内容超出窗口高度时可滚动——窗口矮了不再把卡片和按钮压扁。"""

    settingsChanged = Signal(bool, str)  # enabled, "HH:MM"
    uploadRequested = Signal()  # 开始处理：队列最新直接传，队列空则一条龙
    reexportRequested = Signal()  # 强制拉最新：无视队列重新导出

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

        self._content = QWidget()
        self.setWidget(self._content)

        self._build_ui()
        self.load_config(config)
        self.refresh_queue()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """每次切到这个页面都重新扫一遍队列文件夹，替代手动刷新按钮。"""
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
        layout.addWidget(self._build_folder_card())
        layout.addWidget(self._build_switch_card())
        layout.addWidget(self._build_status_card())
        layout.addWidget(self._build_progress_card(), 1)

    def _build_folder_card(self) -> CardWidget:
        """数据文件夹：整条流水线的先决条件，所以放在运行页最上面。"""
        card = CardWidget(self._content)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(StrongBodyLabel("数据文件夹", card))
        self.data_dir_label = CaptionLabel("", card)
        self.data_dir_label.setWordWrap(True)
        text.addWidget(self.data_dir_label)
        row.addLayout(text, 1)

        self.choose_dir_btn = self._btn(PrimaryPushButton("选择文件夹", card), 120)
        self.choose_dir_btn.clicked.connect(self._choose_data_folder)
        row.addWidget(self.choose_dir_btn)
        inner.addLayout(row)

        inner.addWidget(
            CaptionLabel(
                "壳牌订单、转换结果和归档都会放进这个文件夹",
                card,
            )
        )

        return card

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
        self.switch_caption = CaptionLabel("", card)
        text.addWidget(self.switch_caption)
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

        self.reexport_btn = self._btn(PushButton("上传归档", card), 120)
        self.reexport_btn.clicked.connect(self.reexportRequested)
        row.addWidget(self.reexport_btn)

        self.open_folder_btn = self._btn(PushButton("打开数据文件夹", card), 140)
        self.open_folder_btn.clicked.connect(self._open_data_folder)
        row.addWidget(self.open_folder_btn)
        row.addStretch(1)
        inner.addLayout(row)

        self.progress_bar = IndeterminateProgressBar(card)
        self.progress_bar.hide()
        inner.addWidget(self.progress_bar)

        return card

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
        self.switch_caption.setText(
            "开启后程序驻留托盘，每天定时导出 → 转换 → API 自动上传"
            if config.transfer_mode == "api"
            else "开启后程序驻留托盘，每天定时从壳牌导出订单并转换成 SDCC 格式，不自动上传 SDCC。"
        )
        self._suppress = True
        try:
            self.enable_switch.setChecked(config.auto_prepare_enabled)
            hour, minute = config.schedule_hour_minute
            self.time_edit.setTime(QTime(hour, minute))
        finally:
            self._suppress = False
        self._refresh_data_dir()

    def refresh_queue(self) -> None:
        if not self._config.data_dir.strip():
            self.queue_label.setText("待上传订单：未选择数据文件夹")
            self.plan_label.setText(
                "先点上方「选择文件夹」，壳牌订单和转换结果都会放进你选的文件夹。"
            )
            self.upload_btn.setEnabled(False)
            self.reexport_btn.setEnabled(False)
            return

        files = outbox_files()
        if not files:
            self.queue_label.setText("待上传订单：暂无")
            self.plan_label.setText(
                "点「开始处理」会从壳牌导出订单、转换成 SDCC 格式后再上传，一条龙。"
            )
        else:
            latest = files[0]
            stamp = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%m-%d %H:%M")
            self.queue_label.setText(
                f"待上传订单：{len(files)} 个（最新：{latest.name} {stamp}）"
            )
            self.plan_label.setText(
                "点「开始处理」直接上传最新一份；需要重传历史文件就点「上传归档」。"
            )

        self.upload_btn.setEnabled(True)
        self.reexport_btn.setEnabled(True)

    def set_next_run(self, next_run: Optional[datetime]) -> None:
        if next_run is None:
            self.next_run_label.setText("自动准备未开启")
        else:
            self.next_run_label.setText(f"下次执行：{next_run:%Y-%m-%d %H:%M}")

    def set_running(self, running: bool) -> None:
        can_run = bool(self._config.data_dir.strip())
        self.upload_btn.setEnabled(not running and can_run)
        self.reexport_btn.setEnabled(not running and can_run)
        self.upload_btn.setText("处理中…" if running else "开始处理")
        self.progress_bar.setVisible(running)
        if running:
            self.progress_text.clear()

    def append_progress(self, step: str, status: str, message: str) -> None:
        icon = STATUS_ICONS.get(status, "·")
        label = STEP_LABELS.get(step, step)
        stamp = datetime.now().strftime("%H:%M:%S")
        self.progress_text.appendPlainText(f"{stamp}  {icon} [{label}] {message}")

    # -------------------------------------------------------------- 数据文件夹

    def _refresh_data_dir(self) -> None:
        data_dir = self._config.data_dir.strip()
        if data_dir:
            self.data_dir_label.setText(data_dir)
            self.choose_dir_btn.setText("更改")
        else:
            self.data_dir_label.setText("还未选择——选了文件夹才能开始处理订单")
            self.choose_dir_btn.setText("选择文件夹")

    def _choose_data_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择数据文件夹")
        if not path:
            return

        chosen = Path(path)
        if chosen.resolve() == work_dir().resolve():
            InfoBar.warning(
                "换一个文件夹",
                "这是程序自己的目录，请选一个专门放订单数据的文件夹",
                duration=4000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            return

        self._config.data_dir = str(chosen)
        self._config.save()

        self._refresh_data_dir()
        self.refresh_queue()


    def _open_data_folder(self) -> None:
        if not self._config.data_dir.strip():
            InfoBar.warning(
                "还没有数据文件夹",
                "先点上方「选择文件夹」",
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            return
        folder = data_root()
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
