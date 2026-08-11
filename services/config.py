"""应用配置与数据目录。

配置和数据一律放在 ``%APPDATA%/ShellConvert/``，不放程序目录——
打包成 exe 之后程序目录可能只读，而且换版本时会被整个覆盖掉。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

APP_NAME = "ShellConvert"
CONFIG_FILENAME = "config.json"

# 待上传文件的统一文件名，保证队列里始终只有最新的一份
UPLOAD_FILENAME = "SDCC导入版.xlsx"
EXCEL_SUFFIXES = {".xlsx", ".xls"}

DEFAULT_LOGIN_URL = "https://tms.i.sinotrans.com/sdccweb/manage/"
DEFAULT_PROJECT = "中海壳牌深圳"
DEFAULT_TEMPLATE = "中海壳牌深圳-中海壳牌导入模版"

PathLike = Union[str, Path]


# --------------------------------------------------------------------------- #
# 目录
# --------------------------------------------------------------------------- #


def app_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sub_dir(name: str) -> Path:
    path = app_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def pending_dir() -> Path:
    """待上传队列。UI 转换完写进来，定时任务到点取走。"""
    return _sub_dir("pending")


def archive_dir() -> Path:
    """上传完的归档，成功失败都留档，便于事后核对。"""
    return _sub_dir("archive")


def log_dir() -> Path:
    return _sub_dir("logs")


def resource_path(relative: str) -> Path:
    """assets 资源路径，兼容 PyInstaller 打包后的临时解压目录。"""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent.parent
    return root / relative


# --------------------------------------------------------------------------- #
# 待上传队列
# --------------------------------------------------------------------------- #


def pending_files() -> List[Path]:
    """待上传目录里的 Excel，按修改时间倒序。"""
    try:
        entries = [
            p
            for p in pending_dir().iterdir()
            if p.is_file() and p.suffix.lower() in EXCEL_SUFFIXES
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda p: p.stat().st_mtime, reverse=True)


def latest_pending() -> Optional[Path]:
    """取最新的一个待上传文件；一般情况下队列里就只有一个。"""
    files = pending_files()
    return files[0] if files else None


def put_pending(source: PathLike, filename: str = UPLOAD_FILENAME) -> Path:
    """把转换结果放进待上传队列，同名覆盖。"""
    target = pending_dir() / filename
    shutil.copyfile(str(source), str(target))
    return target


def clear_pending() -> int:
    removed = 0
    for path in pending_files():
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def archive_pending(file_path: PathLike, success: bool) -> Path:
    """上传结束后归档到 ``archive/YYYY-MM-DD/`` 下。"""
    source = Path(file_path)
    now = datetime.now()
    day_dir = archive_dir() / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    prefix = "ok" if success else "fail"
    target = day_dir / f"{prefix}_{now:%H%M%S}_{source.name}"
    shutil.move(str(source), str(target))
    return target


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    """用户可配置项。密码不在这里，走 keyring。"""

    # SDCC
    username: str = ""
    login_url: str = DEFAULT_LOGIN_URL
    project: str = DEFAULT_PROJECT
    template: str = DEFAULT_TEMPLATE

    # 自动上传
    auto_upload_enabled: bool = False
    schedule_time: str = "09:30"  # HH:MM，24 小时制
    retry_delays_minutes: List[int] = field(default_factory=lambda: [5, 15, 30])

    # 浏览器
    browser_channels: List[str] = field(default_factory=lambda: ["chrome", "msedge"])
    headless: bool = False
    timeout_ms: int = 100_000
    result_timeout_ms: int = 120_000

    # 其他
    autostart: bool = False
    minimize_to_tray: bool = True

    @classmethod
    def path(cls) -> Path:
        return app_dir() / CONFIG_FILENAME

    @classmethod
    def load(cls) -> "Config":
        path = cls.path()
        if not path.exists():
            config = cls()
            config.save()
            return config
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 配置坏了不能让程序起不来，退回默认值
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        path = self.path()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)

    @property
    def schedule_hour_minute(self) -> Tuple[int, int]:
        try:
            hour, minute = str(self.schedule_time).split(":")
            return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
        except (ValueError, AttributeError):
            return 9, 30

    @property
    def max_attempts(self) -> int:
        """首次尝试 + 重试次数。"""
        return len(self.retry_delays_minutes or []) + 1
