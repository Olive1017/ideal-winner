"""日志页：左侧按 run_id 列出每次执行，右侧看详情。

数据直接读 JSONL，不经过任何中间状态——后续把日志搬到网页前端时，
前端读的是同一份文件、同一套字段，不会出现两边对不上的情况。
"""

from __future__ import annotations

import subprocess
import sys
from html import escape
from typing import Any, Dict, List

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    ListWidget,
    PushButton,
    SubtitleLabel,
    TextBrowser,
)

from services.config import log_dir
from services.logger import group_by_run, read_entries

AUTO_REFRESH_MS = 5000
MAX_RUNS = 100

STATUS_BADGE = {
    "success": ("✓", "#0f7b0f"),
    "fail": ("✕", "#c42b1c"),
    "warn": ("⚠", "#9d5d00"),
    "info": ("·", "#606060"),
}


class LogPage(QWidget):
    """日志页。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("logPage")
        self._runs: List[Dict[str, Any]] = []
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(AUTO_REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        self.refresh()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("日志", self))
        header.addStretch(1)

        refresh_btn = PushButton("刷新", self)
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)

        open_btn = PushButton("打开日志目录", self)
        open_btn.clicked.connect(self._open_log_dir)
        header.addWidget(open_btn)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)

        left = CardWidget(self)
        left_inner = QVBoxLayout(left)
        left_inner.setContentsMargins(12, 12, 12, 12)
        left_inner.setSpacing(8)
        left_inner.addWidget(CaptionLabel("执行记录", left))
        self.run_list = ListWidget(left)
        self.run_list.currentRowChanged.connect(self._show_detail)
        left_inner.addWidget(self.run_list, 1)
        left.setFixedWidth(300)
        body.addWidget(left)

        right = CardWidget(self)
        right_inner = QVBoxLayout(right)
        right_inner.setContentsMargins(12, 12, 12, 12)
        right_inner.setSpacing(8)
        right_inner.addWidget(CaptionLabel("详情", right))
        self.detail = TextBrowser(right)
        self.detail.setOpenExternalLinks(False)
        right_inner.addWidget(self.detail, 1)
        body.addWidget(right, 1)

        layout.addLayout(body, 1)

    # -------------------------------------------------------------- 刷新

    def refresh(self) -> None:
        """重读日志。尽量保持用户当前选中的那一条。"""
        selected_run_id = None
        if 0 <= self.run_list.currentRow() < len(self._runs):
            selected_run_id = self._runs[self.run_list.currentRow()]["run_id"]

        self._runs = group_by_run(read_entries())[:MAX_RUNS]

        self.run_list.blockSignals(True)
        self.run_list.clear()
        for run in self._runs:
            badge, _ = STATUS_BADGE.get(run["status"], STATUS_BADGE["info"])
            started = (run.get("started_at") or "").replace("T", " ")[:19]
            self.run_list.addItem(f"{badge}  {started}")
        self.run_list.blockSignals(False)

        if not self._runs:
            self.detail.setHtml("<p style='color:#888'>还没有日志。跑一次上传后这里会有记录。</p>")
            return

        target_row = 0
        if selected_run_id is not None:
            for idx, run in enumerate(self._runs):
                if run["run_id"] == selected_run_id:
                    target_row = idx
                    break
        self.run_list.setCurrentRow(target_row)

    def _show_detail(self, row: int) -> None:
        if not (0 <= row < len(self._runs)):
            return
        run = self._runs[row]

        parts = [
            "<div style='font-family:Segoe UI,Microsoft YaHei;font-size:13px'>",
            f"<p style='color:#888'>run_id: {escape(run['run_id'])}</p>",
        ]

        for entry in run["entries"]:
            status = entry.get("status", "info")
            badge, color = STATUS_BADGE.get(status, STATUS_BADGE["info"])
            stamp = (entry.get("ts") or "").replace("T", " ")[11:23]
            step = escape(str(entry.get("step") or "-"))
            msg = escape(str(entry.get("msg") or ""))
            parts.append(
                f"<p style='margin:2px 0'>"
                f"<span style='color:#888'>{stamp}</span> "
                f"<span style='color:{color};font-weight:600'>{badge} {step}</span> "
                f"{msg}</p>"
            )
            error = entry.get("error")
            if error:
                parts.append(
                    "<pre style='background:#f5f5f5;padding:8px;border-radius:4px;"
                    f"white-space:pre-wrap;color:#c42b1c'>{escape(str(error))}</pre>"
                )

        parts.append("</div>")
        self.detail.setHtml("".join(parts))

    # -------------------------------------------------------------- 其他

    def _open_log_dir(self) -> None:
        path = log_dir()
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
