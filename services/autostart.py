"""开机自启。

写当前用户的 ``HKCU\\...\\Run``，不需要管理员权限，也不用装服务——
程序要发给同事自己装，要求 UAC 提权会直接劝退一半人。

非 Windows 平台上所有函数都是空操作，方便在 Mac/Linux 上开发调试。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .config import APP_NAME

log = logging.getLogger("shell_convert")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ENTRY_NAME = APP_NAME

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import winreg


def _launch_command() -> str:
    """拼出开机要执行的命令。

    打包成 exe 后 ``sys.frozen`` 为真，直接拿 exe 路径；
    源码运行时用当前解释器 + main.py。
    两种情况都带 --minimized，开机直接进托盘，不弹窗口。
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}" --minimized'
    entry = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{Path(sys.executable)}" "{entry}" ui --minimized'


def is_enabled() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, ENTRY_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("读取开机自启状态失败：%s", exc)
        return False


def enable() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, ENTRY_NAME, 0, winreg.REG_SZ, _launch_command())
        return True
    except OSError as exc:
        log.warning("开启开机自启失败：%s", exc)
        return False


def disable() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, ENTRY_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("关闭开机自启失败：%s", exc)
        return False


def apply(enabled: bool) -> bool:
    return enable() if enabled else disable()


def supported() -> bool:
    return _IS_WINDOWS
