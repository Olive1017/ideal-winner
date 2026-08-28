"""主窗口。

调度器、托盘、配置都由这里统一持有，页面只发信号。

一个容易踩坑的点：APScheduler 的回调跑在它自己的后台线程里，
而 Qt 控件只能在主线程碰。所以回调里不直接改界面，而是 emit 信号，
Qt 会自动用队列连接把它派回主线程——直接改控件会随机闪退。
"""

from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    NavigationItemPosition,
    Theme,
    setTheme,
)

from core.models import UploadStatus
from services.config import Config
from services.scheduler import UploadScheduler
from services.single_instance import app_lock

from .pages.auto_page import AutoPage
from .pages.log_page import LogPage
from .pages.settings_page import SettingsPage
from .tray import APP_TITLE, TrayIcon, make_icon
from .workers import UploadWorker

NEXT_RUN_REFRESH_MS = 30_000

# 窗口最小尺寸：低于这个高度，导航栏底部的「设置」项会被挤出可视区
MIN_WINDOW_SIZE = (960, 640)


class MainWindow(FluentWindow):
    """主窗口。"""

    # 从调度器线程回主线程的桥
    schedulerProgress = Signal(str, str, str)
    schedulerResult = Signal(object)

    def __init__(self, start_minimized: bool = False) -> None:
        super().__init__()
        self.config = Config.load()
        self._force_quit = False
        self._worker = None

        self._init_window()
        self._init_pages()
        self._init_tray()
        self._init_scheduler()
        self._connect_signals()

        self._next_run_timer = QTimer(self)
        self._next_run_timer.setInterval(NEXT_RUN_REFRESH_MS)
        self._next_run_timer.timeout.connect(self._refresh_next_run)
        self._next_run_timer.start()
        self._refresh_next_run()

        if start_minimized:
            self.hide()

    # ------------------------------------------------------------ 初始化

    def _init_window(self) -> None:
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(make_icon("idle"))
        # 先给最小尺寸再 resize：否则内容多时无边框窗口的上下边可能拖不动，
        # 且底部导航项（设置）会被挤没
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        self.resize(1060, 740)
        self.navigationInterface.setExpandWidth(180)

    def _init_pages(self) -> None:
        self.auto_page = AutoPage(self.config, self)
        self.log_page = LogPage(self)
        self.settings_page = SettingsPage(self.config, self)

        self.addSubInterface(self.auto_page, FIF.SEND, "运行")
        self.addSubInterface(self.log_page, FIF.HISTORY, "日志")
        self.addSubInterface(
            self.settings_page, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM
        )

    def _init_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = TrayIcon(self)
        self.tray.show()

    def _init_scheduler(self) -> None:
        self.scheduler = UploadScheduler(
            self.config,
            on_result=self.schedulerResult.emit,
            progress=self.schedulerProgress.emit,
        )
        self.scheduler.start()

    def _connect_signals(self) -> None:
        self.auto_page.settingsChanged.connect(self._on_schedule_changed)
        self.auto_page.uploadRequested.connect(self._start_manual_upload)
        self.auto_page.reexportRequested.connect(self._start_reexport)
        self.auto_page.uploadFileRequested.connect(self._start_file_upload)

        self.settings_page.configSaved.connect(self._on_config_saved)

        self.schedulerProgress.connect(self._on_progress)
        self.schedulerResult.connect(self._on_upload_result)

        if self.tray is not None:
            self.tray.showRequested.connect(self._show_window)
            self.tray.uploadRequested.connect(self._start_manual_upload)
            self.tray.quitRequested.connect(self.quit_app)

    # ------------------------------------------------------------ 事件

    def _on_schedule_changed(self, enabled: bool, schedule_time: str) -> None:
        self.config.auto_prepare_enabled = enabled
        self.config.schedule_time = schedule_time
        self.config.save()
        self.scheduler.apply_config(self.config)
        self._refresh_next_run()

    def _on_config_saved(self, config: Config) -> None:
        self.config = config
        self.scheduler.apply_config(config)
        self.auto_page.load_config(config)
        self._refresh_next_run()

    def _on_progress(self, step: str, status: str, message: str) -> None:
        self.auto_page.append_progress(step, status, message)
        if self.tray is not None:
            self.tray.set_state("busy")

    def _refresh_next_run(self) -> None:
        self.auto_page.set_next_run(self.scheduler.next_run_time)

    # ---------------------------------------------------------- 手动执行

    def _start_manual_upload(self) -> None:
        """开始处理：队列有最新文件就直接传，队列空则走 导出→转换→上传 一条龙。"""
        self._start_worker(force_export=False)

    def _start_reexport(self) -> None:
        """强制拉最新：无视队列，重新从壳牌导出再跑整条流水线。"""
        self._start_worker(force_export=True)

    def _start_file_upload(self, file_path: str) -> None:
        """上传待上传列表里指定的某一份文件（行内「上传」按钮触发）。"""
        self._start_worker(file_path=file_path)

    def _start_worker(
        self, force_export: bool = False, file_path: Optional[str] = None
    ) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._info("已经有一个任务在跑了")
            return

        self.auto_page.set_running(True)
        if self.tray is not None:
            self.tray.set_state("busy")

        self._worker = UploadWorker(
            self.config, file_path=file_path, force_export=force_export, parent=self
        )
        self._worker.progressed.connect(self._on_progress)
        self._worker.finishedResult.connect(self._on_upload_result)
        self._worker.start()

    def _on_upload_result(self, result) -> None:
        self.auto_page.set_running(False)
        self.auto_page.refresh_queue()
        self.log_page.refresh()

        if result.status is UploadStatus.SUCCESS:
            self._set_tray("ok", result.message)
            self._toast_success("上传成功", result.message)
        elif result.status is UploadStatus.SKIPPED:
            self._set_tray("idle", result.message)
            self._info(result.message)
        else:
            hint = "（稍后会自动重试）" if result.retryable else "（需要人工处理）"
            self._set_tray("error", result.message)
            self._toast_error("上传失败" + hint, result.message)

    def _set_tray(self, state: str, detail: str = "") -> None:
        if self.tray is not None:
            self.tray.set_state(state, detail)

    # ------------------------------------------------------------ 窗口

    def _show_window(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~self.windowState().WindowMinimized)
        self.raise_()
        self.activateWindow()
        if self.tray is not None and self.tray._state == "error":
            self.tray.set_state("idle")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """默认收到托盘，否则关窗口定时任务就没了。"""
        if self._force_quit or self.tray is None or not self.config.minimize_to_tray:
            self._cleanup()
            event.accept()
            return

        event.ignore()
        self.hide()
        self.tray.showMessage(
            APP_TITLE,
            "程序已最小化到托盘，定时上传仍在运行",
            make_icon("idle"),
            3000,
        )

    def quit_app(self) -> None:
        self._force_quit = True
        self._cleanup()
        QApplication.quit()

    def _cleanup(self) -> None:
        self._next_run_timer.stop()
        try:
            self.scheduler.shutdown()
        except Exception:  # noqa: BLE001
            pass
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(5000)
        if self.tray is not None:
            self.tray.hide()
        app_lock.release()

    # ------------------------------------------------------------ 提示

    def _toast_success(self, title: str, message: str) -> None:
        InfoBar.success(title, message, duration=4000,
                        position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _toast_error(self, title: str, message: str) -> None:
        InfoBar.error(title, message, duration=-1, isClosable=True,
                      position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _info(self, message: str) -> None:
        InfoBar.info("提示", message, duration=3000,
                     position=InfoBarPosition.TOP_RIGHT, parent=self)


def run_app(start_minimized: bool = False) -> int:
    """启动 Qt 应用。"""
    # Windows 125%/150% 这类分数缩放下，Qt 默认的 PassThrough 取整会让控件
    # 尺寸和字体对不上，按钮被压扁、文字裁切；Round 让整体按整数倍缩放
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.Round
    )
    app = QApplication(sys.argv)
    # 关窗口不退出进程，否则收到托盘后会被 Qt 直接干掉
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_TITLE)
    app.setWindowIcon(make_icon("idle"))
    setTheme(Theme.AUTO)

    window = MainWindow(start_minimized=start_minimized)
    if not start_minimized:
        window.show()
    return app.exec()
