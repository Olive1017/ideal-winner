"""密码存取。

密码进 Windows 凭据管理器（keyring），配置文件里只留用户名——
程序要发给同事用，明文密码落盘不可接受。
"""

from __future__ import annotations

import logging
from typing import Optional

try:  # keyring 缺失时降级为「无法保存密码」，而不是整个程序起不来
    import keyring
    from keyring.errors import KeyringError
except ImportError:  # pragma: no cover
    keyring = None  # type: ignore[assignment]

    class KeyringError(Exception):  # type: ignore[no-redef]
        pass


SERVICE_NAME = "ShellConvert-SDCC"

log = logging.getLogger("shell_convert")


def available() -> bool:
    return keyring is not None


def get_password(username: str) -> Optional[str]:
    if not username or keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, username)
    except KeyringError as exc:
        log.warning("读取密码失败：%s", exc)
        return None


def set_password(username: str, password: str) -> bool:
    if not username or keyring is None:
        return False
    try:
        keyring.set_password(SERVICE_NAME, username, password)
        return True
    except KeyringError as exc:
        log.warning("保存密码失败：%s", exc)
        return False


def delete_password(username: str) -> bool:
    if not username or keyring is None:
        return False
    try:
        keyring.delete_password(SERVICE_NAME, username)
        return True
    except KeyringError:
        return False


def has_password(username: str) -> bool:
    return bool(get_password(username))
