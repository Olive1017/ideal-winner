"""JSONL 结构化日志。

每行一个 JSON，字段固定为 ts / run_id / level / step / status / msg / extra。
UI 的日志页按 run_id 分组渲染成时间线；将来要把日志搬到网页前端展示，
格式不用改，直接按行 parse 即可。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import log_dir

LOGGER_NAME = "shell_convert"
LOG_FILENAME = "run.jsonl"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

_setup_lock = threading.Lock()
_configured = False


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "run_id": getattr(record, "run_id", ""),
            "level": record.levelname,
            "step": getattr(record, "step", ""),
            "status": getattr(record, "status", "info"),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_data", None)
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup(console: bool = True) -> logging.Logger:
    """初始化全局 logger，重复调用无副作用。"""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    with _setup_lock:
        if _configured:
            return logger
        logger.setLevel(logging.INFO)
        logger.propagate = False

        file_handler = RotatingFileHandler(
            log_dir() / LOG_FILENAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonLineFormatter())
        logger.addHandler(file_handler)

        if console:
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(
                logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S")
            )
            logger.addHandler(stream)

        _configured = True
    return logger


class RunLogger:
    """绑定一个 run_id，负责一次任务全过程的日志。"""

    def __init__(self, run_id: Optional[str] = None, console: bool = True) -> None:
        self.run_id = run_id or new_run_id()
        self._logger = setup(console=console)

    def log(
        self,
        step: str,
        status: str = "info",
        msg: str = "",
        level: int = logging.INFO,
        exc_info: bool = False,
        **extra: Any,
    ) -> None:
        self._logger.log(
            level,
            msg or f"{step} {status}",
            exc_info=exc_info,
            extra={
                "run_id": self.run_id,
                "step": step,
                "status": status,
                "extra_data": extra or None,
            },
        )

    def start(self, step: str, msg: str = "", **extra: Any) -> None:
        self.log(step, "start", msg, **extra)

    def success(self, step: str, msg: str = "", **extra: Any) -> None:
        self.log(step, "success", msg, **extra)

    def info(self, step: str, msg: str = "", **extra: Any) -> None:
        self.log(step, "info", msg, **extra)

    def warn(self, step: str, msg: str = "", **extra: Any) -> None:
        self.log(step, "warn", msg, level=logging.WARNING, **extra)

    def fail(self, step: str, msg: str = "", exc_info: bool = False, **extra: Any) -> None:
        self.log(step, "fail", msg, level=logging.ERROR, exc_info=exc_info, **extra)


# --------------------------------------------------------------------------- #
# 读取（供 UI 日志页使用）
# --------------------------------------------------------------------------- #


def log_file() -> Path:
    return log_dir() / LOG_FILENAME


def read_entries(limit: int = 1000) -> List[Dict[str, Any]]:
    """读取最近的日志条目，按时间正序返回。"""
    path = log_file()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    entries: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def group_by_run(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 run_id 聚合成一次次执行，最新的排前面。

    每组的 status 取最严重的那个：只要出现过 fail 就算 fail。
    """
    runs: Dict[str, Dict[str, Any]] = {}

    for entry in entries:
        run_id = entry.get("run_id") or "-"
        run = runs.get(run_id)
        if run is None:
            run = {
                "run_id": run_id,
                "entries": [],
                "status": "info",
                "started_at": entry.get("ts", ""),
                "ended_at": entry.get("ts", ""),
            }
            runs[run_id] = run

        run["entries"].append(entry)
        run["ended_at"] = entry.get("ts", run["ended_at"])

        status = entry.get("status")
        if status == "fail":
            run["status"] = "fail"
        elif status == "success" and run["status"] != "fail":
            run["status"] = "success"
        elif status == "warn" and run["status"] not in ("fail", "success"):
            run["status"] = "warn"

    return sorted(runs.values(), key=lambda r: r.get("started_at") or "", reverse=True)


def latest_run(entries: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """最近一次执行，托盘图标用它决定显示正常还是报警。"""
    runs = group_by_run(entries if entries is not None else read_entries())
    return runs[0] if runs else None
