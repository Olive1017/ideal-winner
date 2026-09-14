"""转换与上传过程中共用的数据模型和异常。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

import pandas as pd


class ErrorKind(str, Enum):
    """错误分类，决定调度器是否安排重试。"""

    # 网络抖动、页面加载慢、元素迟迟不出现、服务端 5xx —— 值得退避后重试
    RETRYABLE = "retryable"
    # 密码错误、账号锁定、文件格式错、业务校验不通过 —— 重试只会一直撞墙
    FATAL = "fatal"


class UploadStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ConvertError(Exception):
    """转换过程中的可预期错误，message 直接面向最终用户展示。"""


class UploadError(Exception):
    """上传过程中的错误，带分类信息供调度器判断是否重试。"""

    def __init__(
        self,
        message: str,
        kind: ErrorKind = ErrorKind.FATAL,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        # detail 通常是错误截图路径或 SDCC 返回的原始文案
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.kind is ErrorKind.RETRYABLE

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志展示
        return self.message


@dataclass
class ConvertResult:
    """一次转换的产物。df 只在内存里，是否落盘由调用方决定。"""

    df: pd.DataFrame
    row_count: int = 0
    car_matched: int = 0
    car_unmatched: int = 0
    unmatched_samples: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    def summary(self) -> str:
        parts = [f"共 {self.row_count} 行订单"]
        if self.car_matched or self.car_unmatched:
            parts.append(f"车型匹配 {self.car_matched} 行 / 未匹配 {self.car_unmatched} 行")
        return "，".join(parts)


@dataclass
class UploadResult:
    """一次上传任务的结果，UI 和日志都以它为准。"""

    status: UploadStatus
    message: str
    run_id: str = ""
    file_path: Optional[str] = None
    archived_path: Optional[str] = None
    detail: Optional[str] = None
    screenshot: Optional[str] = None
    duration_sec: float = 0.0
    attempt: int = 1
    retryable: bool = False

    @property
    def ok(self) -> bool:
        return self.status is UploadStatus.SUCCESS

    @property
    def skipped(self) -> bool:
        return self.status is UploadStatus.SKIPPED
