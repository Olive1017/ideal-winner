from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QTableView


class OrderPreviewDialog(QDialog):
    """只读订单预览对话框。"""

    def __init__(self, file_path: str | Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("订单预览")
        self.resize(1100, 700)
        self._file_path = Path(file_path)
        self._df: Optional[pd.DataFrame] = None
        self._build_ui()
        self._load_data()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)

        self.title_label = QLabel("订单预览", self)
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(self.title_label)

        self.meta_label = QLabel(self)
        layout.addWidget(self.meta_label)

        self.table_view = QTableView(self)
        self.table_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSortingEnabled(False)
        self.table_view.setWordWrap(True)
        self.table_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table_view.horizontalHeader().setStretchLastSection(True)
        self.table_view.verticalHeader().setVisible(False)
        layout.addWidget(self.table_view, 1)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, self)
        self.button_box.accepted.connect(self.accept)
        layout.addWidget(self.button_box)

    def _load_data(self) -> None:
        try:
            df = pd.read_excel(self._file_path)
        except Exception as exc:
            self.meta_label.setText(f"无法读取订单文件\n{exc}")
            self.table_view.setModel(None)
            return

        if df is None or df.empty:
            self.meta_label.setText(f"文件：{self._file_path.name}\n当前订单文件没有可显示的订单。")
            self.table_view.setModel(None)
            return

        df_clean = df.dropna(how="all")
        self._df = df_clean
        if self._df is None or self._df.empty:
            self.meta_label.setText(f"文件：{self._file_path.name}\n当前订单文件没有可显示的订单。")
            self.table_view.setModel(None)
            return

        self.meta_label.setText(
            f"文件：{self._file_path.name}\n共 {len(self._df)} 条订单"
        )
        self._set_table_model(self._df)

    def _set_table_model(self, df: pd.DataFrame) -> None:
        headers = [str(col) for col in df.columns]
        model = QStandardItemModel(len(df.index), len(headers), self)
        model.setHorizontalHeaderLabels(headers)

        for row_idx, row in df.iterrows():
            for col_idx, value in enumerate(row.tolist()):
                item = QStandardItem()
                # 让长文本可见并保持只读
                if value is None or pd.isna(value):
                    text = ""
                else:
                    text = str(value)
                item.setText(text)
                item.setEditable(False)
                model.setItem(row_idx, col_idx, item)

        self.table_view.setModel(model)
        self.table_view.resizeRowsToContents()
