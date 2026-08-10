"""转换页：选文件 → 转换 → 预览 → 导出 / 放入待上传队列。

两个输入文件都需要人工选（壳牌订单每天变、车型表不定期变），
所以转换这一步不自动化，只自动化上传。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
)

from core.converter import export
from services.config import UPLOAD_FILENAME, pending_dir

from ..workers import ConvertWorker

PREVIEW_ROWS = 50
EXCEL_FILTER = "Excel 文件 (*.xlsx *.xls)"


class FilePickerRow(QWidget):
    """一行：标题 + 路径输入框 + 浏览按钮。"""

    changed = Signal()

    def __init__(self, label: str, placeholder: str, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = BodyLabel(label, self)
        title.setFixedWidth(96)

        self.edit = LineEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.textChanged.connect(self.changed)

        browse = PushButton("浏览", self)
        browse.clicked.connect(self._browse)

        layout.addWidget(title)
        layout.addWidget(self.edit, 1)
        layout.addWidget(browse)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", "", EXCEL_FILTER)
        if path:
            self.edit.setText(path)

    def value(self) -> str:
        return self.edit.text().strip()


class ConvertPage(QWidget):
    """转换页。"""

    queueChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("convertPage")
        self._result = None
        self._worker: Optional[ConvertWorker] = None
        self._build_ui()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)

        layout.addWidget(SubtitleLabel("转换", self))

        layout.addWidget(self._build_input_card())
        layout.addWidget(self._build_summary_card())
        layout.addWidget(self._build_preview_card(), 1)

    def _build_input_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(12)

        self.shell_row = FilePickerRow("壳牌订单", "含「运单」工作表的 Excel", card)
        self.car_row = FilePickerRow("车型映射", "可选；不选则车型全部填「未知」", card)
        inner.addWidget(self.shell_row)
        inner.addWidget(self.car_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.convert_btn = PrimaryPushButton("开始转换", card)
        self.convert_btn.clicked.connect(self._start_convert)

        self.export_btn = PushButton("另存为…", card)
        self.export_btn.clicked.connect(self._export_as)
        self.export_btn.setEnabled(False)

        self.queue_btn = PushButton("放入待上传队列", card)
        self.queue_btn.clicked.connect(self._put_in_queue)
        self.queue_btn.setEnabled(False)

        buttons.addWidget(self.convert_btn)
        buttons.addWidget(self.export_btn)
        buttons.addWidget(self.queue_btn)
        buttons.addStretch(1)
        inner.addLayout(buttons)

        return card

    def _build_summary_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 14, 20, 14)
        inner.setSpacing(6)

        self.summary_label = StrongBodyLabel("尚未转换", card)
        self.warning_label = CaptionLabel("", card)
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextColor("#c76b00", "#ffb951")
        self.warning_label.hide()

        inner.addWidget(self.summary_label)
        inner.addWidget(self.warning_label)
        return card

    def _build_preview_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 14, 20, 16)
        inner.setSpacing(8)

        inner.addWidget(CaptionLabel(f"预览（最多显示前 {PREVIEW_ROWS} 行）", card))

        self.table = TableWidget(card)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        inner.addWidget(self.table, 1)

        return card

    # -------------------------------------------------------------- 事件

    def _start_convert(self) -> None:
        shell_file = self.shell_row.value()
        if not shell_file:
            self._warn("请先选择壳牌订单文件")
            return

        self.convert_btn.setEnabled(False)
        self.convert_btn.setText("转换中…")
        self.summary_label.setText("正在读取并转换…")
        self.warning_label.hide()

        self._worker = ConvertWorker(shell_file, self.car_row.value() or None, self)
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_finished(self) -> None:
        self.convert_btn.setEnabled(True)
        self.convert_btn.setText("开始转换")

    def _on_success(self, result) -> None:
        self._result = result
        self.export_btn.setEnabled(True)
        self.queue_btn.setEnabled(True)

        self.summary_label.setText(result.summary())
        if result.warnings:
            self.warning_label.setText("⚠ " + "\n⚠ ".join(result.warnings))
            self.warning_label.show()
        else:
            self.warning_label.hide()

        self._fill_preview(result.df)
        self._success(f"转换完成，{result.row_count} 行")

    def _on_failed(self, message: str) -> None:
        self._result = None
        self.export_btn.setEnabled(False)
        self.queue_btn.setEnabled(False)
        self.summary_label.setText("转换失败")
        self.table.setRowCount(0)
        self._error(message)

    def _fill_preview(self, df) -> None:
        preview = df.head(PREVIEW_ROWS)
        self.table.setColumnCount(len(preview.columns))
        self.table.setRowCount(len(preview))
        self.table.setHorizontalHeaderLabels([str(c) for c in preview.columns])

        for row_idx, (_, row) in enumerate(preview.iterrows()):
            for col_idx, value in enumerate(row):
                item = QTableWidgetItem("" if value is None else str(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
                self.table.setItem(row_idx, col_idx, item)

        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

    def _export_as(self) -> None:
        if self._result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "另存为", UPLOAD_FILENAME, EXCEL_FILTER)
        if not path:
            return
        try:
            export(self._result.df, path)
        except OSError as exc:
            self._error(f"导出失败：{exc}")
            return
        self._success(f"已导出到 {Path(path).name}")

    def _put_in_queue(self) -> None:
        if self._result is None:
            return
        target = pending_dir() / UPLOAD_FILENAME
        try:
            export(self._result.df, target)
        except OSError as exc:
            self._error(f"写入待上传队列失败：{exc}")
            return
        self.queueChanged.emit()
        self._success("已放入待上传队列，下次定时任务会自动上传")

    # ------------------------------------------------------------ 提示条

    def _success(self, message: str) -> None:
        InfoBar.success("成功", message, duration=3000,
                        position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _warn(self, message: str) -> None:
        InfoBar.warning("提示", message, duration=3000,
                        position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _error(self, message: str) -> None:
        InfoBar.error("出错了", message, duration=6000, isClosable=True,
                      position=InfoBarPosition.TOP_RIGHT, parent=self)
