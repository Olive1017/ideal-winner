"""系统托盘。

图标用代码画，不依赖 assets 里的图片文件——PyInstaller 打包时少一个
容易遗漏的 --add-data，而且三种状态只是颜色不同，没必要存三张图。

状态颜色：
- idle  蓝色  空闲
- busy  黄色  正在上传
- ok    绿色  最近一次成功
- error 红色  最近一次失败（不弹窗，只变色）
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

APP_TITLE = "壳牌订单转换"

STATE_COLORS = {
    "idle": "#0078d4",
    "busy": "#f7b500",
    "ok": "#107c10",
    "error": "#c42b1c",
}

STATE_TOOLTIPS = {
    "idle": "空闲",
    "busy": "正在上传…",
    "ok": "最近一次上传成功",
    "error": "最近一次上传失败，点开看日志",
}


def make_icon(state: str = "idle", size: int = 64) -> QIcon:
    """画一个带字母的圆形图标。"""
    color = QColor(STATE_COLORS.get(state, STATE_COLORS["idle"]))

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawEllipse(QRectF(2, 2, size - 4, size - 4))

    painter.setPen(QColor("#ffffff"))
    font = QFont()
    font.setPointSizeF(size * 0.46)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "S")
    painter.end()

    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    """托盘图标。只发信号，不直接操作主窗口。"""

    showRequested = Signal()
    uploadRequested = Signal()
    quitRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self.setIcon(make_icon("idle"))
        self.setToolTip(f"{APP_TITLE} - {STATE_TOOLTIPS['idle']}")
        self._build_menu()
        self.activated.connect(self._on_activated)

    def _build_menu(self) -> None:
        menu = QMenu()

        show_action = QAction("显示主窗口", menu)
        show_action.triggered.connect(self.showRequested)
        menu.addAction(show_action)

        upload_action = QAction("立即上传一次", menu)
        upload_action.triggered.connect(self.uploadRequested)
        menu.addAction(upload_action)

        menu.addSeparator()

        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(self.quitRequested)
        menu.addAction(quit_action)

        self._menu = menu  # 保一个引用，否则会被 GC 掉导致菜单不弹
        self.setContextMenu(menu)

    def set_state(self, state: str, detail: str = "") -> None:
        if state == self._state and not detail:
            return
        self._state = state
        self.setIcon(make_icon(state))
        tip = STATE_TOOLTIPS.get(state, "")
        self.setToolTip(f"{APP_TITLE} - {detail or tip}")

    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.showRequested.emit()
