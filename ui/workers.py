"""后台线程。

转换要读几千行 Excel，上传要开浏览器跑几十秒——都不能放在 UI 线程，
否则窗口会直接卡死变白。结果通过信号回主线程。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal

from core.converter import convert
from core.models import ConvertError
from services.config import Config
from services.scheduler import run_upload_once


class ConvertWorker(QThread):
    """后台执行转换。"""

    succeeded = Signal(object)  # ConvertResult
    failed = Signal(str)

    def __init__(self, shell_file: str, car_file: Optional[str], parent=None) -> None:
        super().__init__(parent)
        self.shell_file = shell_file
        self.car_file = car_file

    def run(self) -> None:  # noqa: D102
        try:
            result = convert(self.shell_file, self.car_file)
        except ConvertError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 线程里抛异常会静默死掉
            self.failed.emit(f"转换出现未预期错误：{exc}")
        else:
            self.succeeded.emit(result)


class UploadWorker(QThread):
    """后台执行一次上传，实时向 UI 报进度。"""

    progressed = Signal(str, str, str)  # step, status, message
    finishedResult = Signal(object)  # UploadResult

    def __init__(self, config: Config, file_path: Optional[str] = None, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.file_path = file_path

    def run(self) -> None:  # noqa: D102
        def progress(step: str, status: str, message: str) -> None:
            self.progressed.emit(step, status, message)

        result = run_upload_once(
            self.config, file_path=self.file_path, progress=progress
        )
        self.finishedResult.emit(result)
