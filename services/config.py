from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

APP_NAME = "SDCC订单工具"
CONFIG_FILENAME = "config.json"

# 新流程不再使用固定 pending/ 目录以及 session.json；保留兼容字段，避免旧调用直接报错。
UPLOAD_FILENAME = "SDCC导入版.xlsx"
EXCEL_SUFFIXES = {".xlsx", ".xls"}

DEFAULT_LOGIN_URL = "https://tms.i.sinotrans.com/sdccweb/manage/"
DEFAULT_PROJECT = "中海壳牌深圳"
DEFAULT_TEMPLATE = "中海壳牌深圳-中海壳牌导入模版"

# 壳牌 LMS（导出订单源数据；和 SDCC 是两套完全独立的账号）
DEFAULT_SHELL_LOGIN_URL = "https://lms.cnoocshell.com/"
DEFAULT_EXPORT_REGION = "粤西粤北区域"

PathLike = Union[str, Path]


# --------------------------------------------------------------------------- #
# 目录
# --------------------------------------------------------------------------- #


def work_dir() -> Path:
    """显式的用户工作目录：~/SDCC订单工具。"""
    path = Path.home() / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def app_dir() -> Path:
    """兼容旧调用：本项目已统一使用工作目录。"""
    return work_dir()


def config_path() -> Path:
    return work_dir() / CONFIG_FILENAME


def _sub_dir(name: str) -> Path:
    path = work_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def shell_orders_dir() -> Path:
    """壳牌 LMS 原始导出订单目录。"""
    return _sub_dir("壳牌订单")


def sdcc_orders_dir() -> Path:
    """转换后的 SDCC 文件目录。"""
    return _sub_dir("SDCC订单")


def archive_dir() -> Path:
    """归档目录，按日期组织。"""
    return _sub_dir("归档")


def log_dir() -> Path:
    return _sub_dir("日志")


def screenshot_dir() -> Path:
    return _sub_dir("截图")


def pending_dir() -> Path:
    """兼容旧命名：现在指向 SDCC订单/。"""
    return sdcc_orders_dir()


def download_dir() -> Path:
    """兼容旧命名：原始壳牌导出目录。"""
    return shell_orders_dir()


def resource_path(relative: str) -> Path:
    """assets 资源路径，兼容 PyInstaller 打包后的临时解压目录。"""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent.parent
    return root / relative


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_\-]+", "_", value or "订单")
    return cleaned.strip("_.") or "订单"


def sdcc_file_name(source_name: Optional[str] = None) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = _safe_name(Path(source_name).stem if source_name else "订单")
    return f"SDCC_{base}_{stamp}.xlsx"


# --------------------------------------------------------------------------- #
# 兼容旧调用：旧 pending/ 目录依赖保留，但不再作为核心业务目录
# --------------------------------------------------------------------------- #


def pending_files() -> List[Path]:
    """返回 SDCC订单/ 下的 Excel，按修改时间倒序。"""
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
    """取最新的一个 SDCC 文件。"""
    files = pending_files()
    return files[0] if files else None


def put_pending(source: PathLike, filename: str = "") -> Path:
    """兼容旧接口：将转换结果写入 SDCC订单/，默认创建带时间戳的文件名。"""
    target_dir = pending_dir()
    target_name = filename or sdcc_file_name(str(source))
    target = target_dir / target_name
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


def clear_stale_pending() -> List[Path]:
    """清理比今天更早的 SDCC 文件，避免旧文件被重新当成待上传候选。"""
    from datetime import date

    removed: List[Path] = []
    today = date.today()
    for path in pending_files():
        try:
            mtime = date.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime < today:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                continue
    return removed


def archive_pending(file_path: PathLike, success: bool) -> Path:
    """归档 SDCC 订单文件到 archive/YYYY-MM-DD/。"""
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

    # 壳牌 LMS（导出源数据）
    shell_username: str = ""
    shell_login_url: str = DEFAULT_SHELL_LOGIN_URL
    export_region: str = DEFAULT_EXPORT_REGION
    car_file: str = ""  # 车型映射表路径；相对稳定，配一次即可

    # 自动上传
    auto_upload_enabled: bool = False
    schedule_time: str = "18:30"  # HH:MM，24 小时制
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
        return config_path()

    @classmethod
    def load(cls) -> "Config":
        path = cls.path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        overrides = {k: v for k, v in raw.items() if k in known}
        return cls(**overrides)

    def save(self) -> None:
        defaults = asdict(Config())
        overrides = {k: v for k, v in asdict(self).items() if v != defaults[k]}
        path = self.path()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8"
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
