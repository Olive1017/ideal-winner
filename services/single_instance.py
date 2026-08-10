"""进程锁。

两个用途：
1. 应用单实例 —— 避免同事重复双击开两个窗口，两个调度器同时到点。
2. 上传互斥 —— 手动上传和定时上传不能同时开浏览器，同一个 SDCC 账号会互相踢下线。

用文件锁而不是内存锁，因为需要跨进程生效；进程被强杀时操作系统会自动释放。
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from .config import app_dir

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class FileLock:
    """跨进程文件锁，默认非阻塞。"""

    def __init__(self, name: str) -> None:
        self.path = app_dir() / name
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, blocking: bool = False) -> bool:
        if self._handle is not None:
            return True

        try:
            handle = open(self.path, "a+")
        except OSError:
            return False

        try:
            if sys.platform == "win32":
                handle.seek(0)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
            else:
                flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), flags)
        except OSError:
            handle.close()
            return False

        self._handle = handle
        self._write_pid()
        return True

    def _write_pid(self) -> None:
        """记一下 PID，排查时能知道是谁占着锁。写失败不影响锁本身。"""
        if self._handle is None:
            return
        try:
            self._handle.seek(1)
            self._handle.truncate(1)
            self._handle.write(f" pid={os.getpid()}")
            self._handle.flush()
        except OSError:
            pass

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform == "win32":
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise RuntimeError(f"未能获取锁：{self.path.name}")
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


# 全局单例：整个进程共用同一个锁对象
app_lock = FileLock("app.lock")
upload_lock = FileLock("upload.lock")


def ensure_single_instance() -> bool:
    """启动时调用；返回 False 说明已经有一个实例在跑。"""
    return app_lock.acquire()
