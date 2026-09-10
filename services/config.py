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

EXCEL_SUFFIXES = {".xlsx", ".xls"}

DEFAULT_LOGIN_URL = "https://tms.i.sinotrans.com/sdccweb/manage/"
DEFAULT_PROJECT = "中海壳牌深圳"
DEFAULT_TEMPLATE = "中海壳牌深圳-中海壳牌导入模版"

# 壳牌 LMS（导出订单源数据；和 SDCC 是两套完全独立的账号）
DEFAULT_SHELL_LOGIN_URL = "https://lms.cnoocshell.com/"
DEFAULT_EXPORT_REGION = "粤西粤北区域"

# 旧版本把这三个数据文件夹直接放在程序目录；现在由用户在运行页自选
LEGACY_DATA_DIRS = ("壳牌订单", "SDCC订单", "归档")

PathLike = Union[str, Path]


# --------------------------------------------------------------------------- #
# 目录
# --------------------------------------------------------------------------- #


def work_dir() -> Path:
    """程序目录：~/SDCC订单工具。固定不变，放 config.json、日志、锁文件。"""
    path = Path.home() / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return work_dir() / CONFIG_FILENAME


def _sub_dir(name: str) -> Path:
    path = work_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir_configured() -> bool:
    """是否已在运行页选择数据文件夹。未选择时整条流水线都应被拦在入口。"""
    return bool(Config.load().data_dir.strip())


def data_root() -> Path:
    """订单数据根目录：用户在运行页选择的文件夹。

    只有订单数据（壳牌订单/SDCC订单/归档）走这里；
    config.json、日志、截图、锁文件固定在程序目录，不随数据文件夹搬。

    未选择时返回程序目录下的「数据」占位路径但不创建——正常流程在
    UI 和调度入口就会拦截未选择的情况，不会走到这里。
    """
    custom = Config.load().data_dir.strip()
    if not custom:
        return work_dir() / "数据"
    path = Path(custom)
    path.mkdir(parents=True, exist_ok=True)
    return path



def _data_sub_dir(name: str) -> Path:
    path = data_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def shell_orders_dir() -> Path:
    """壳牌 LMS 原始导出订单目录。"""
    return _data_sub_dir("壳牌订单")


def sdcc_orders_dir() -> Path:
    """转换后的 SDCC 文件目录，即待上传队列。"""
    return _data_sub_dir("SDCC订单")


def archive_dir() -> Path:
    """归档目录，按日期组织。"""
    return _data_sub_dir("归档")


def log_dir() -> Path:
    return _sub_dir("日志")


def screenshot_dir() -> Path:
    return _sub_dir("截图")


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
# 待上传队列：SDCC订单/ 目录下的 Excel，文件即状态
# --------------------------------------------------------------------------- #


def outbox_files() -> List[Path]:
    """返回 SDCC订单/ 下的 Excel，按修改时间倒序。"""
    try:
        entries = [
            p
            for p in sdcc_orders_dir().iterdir()
            if p.is_file() and p.suffix.lower() in EXCEL_SUFFIXES
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda p: p.stat().st_mtime, reverse=True)


def latest_outbox() -> Optional[Path]:
    """取最新的一个待上传 SDCC 文件。"""
    files = outbox_files()
    return files[0] if files else None


def clear_stale_outbox() -> List[Path]:
    """清理比今天更早的 SDCC 文件，避免旧文件被重新当成待上传候选。"""
    from datetime import date

    removed: List[Path] = []
    today = date.today()
    for path in outbox_files():
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


def archive_outbox(file_path: PathLike, success: bool) -> Path:
    """归档 SDCC 订单文件到 归档/YYYY-MM-DD/。"""
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

    data_dir: str = ""

        # SDCC API 直传（transfer_mode = "api" 时生效）
    transfer_mode: str = "rpa"  # rpa=浏览器人工上传（兜底）；api=HTTP 接口直传
    api_base_url: str = "https://api.sinotrans.com"  
    data_source_from: str = "zhqpsz"  
    api_item_code: str = "HN_SZ_ZHQPSZ"  # orderInfo.itemCode 项目编码，SDCC 方提供

    # 自动准备（每天定时导出+转换，不自动上传 SDCC）
    auto_prepare_enabled: bool = False
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
        # 旧配置键迁移：auto_upload_enabled → auto_prepare_enabled
        if "auto_upload_enabled" in raw:
            raw.setdefault("auto_prepare_enabled", raw.pop("auto_upload_enabled"))
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
