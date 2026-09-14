"""密码存取。

通用密码优先走 Windows 凭据管理器（keyring）；
SDCC API 的 keyId / apiKey 改为从环境变量读取，避免明文落盘、
避免被提交到 Git，并支持本地独立配置。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

try:  # keyring 缺失时降级为「无法保存密码」，而不是整个程序起不来
    import keyring
    from keyring.errors import KeyringError
except ImportError:  # pragma: no cover
    keyring = None  # type: ignore[assignment]

    class KeyringError(Exception):  # type: ignore[no-redef]
        pass


SERVICE_NAME = "ShellConvert-SDCC"
API_ENV_VARS = {
    "sdcc_api_key_id": "SDCC_API_KEY_ID",
    "sdcc_api_key": "SDCC_API_KEY",
}

log = logging.getLogger("shell_convert")


def _load_local_env() -> None:
    """从项目根目录的 .env 读取本地配置；不提交到 Git。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return

    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = [part.strip() for part in line.split("=", 1)]
            if key and key not in os.environ:
                os.environ[key] = value.strip('"\'')
    except OSError:
        return


_load_local_env()


def available() -> bool:
    return keyring is not None


def get_password(username: str) -> Optional[str]:
    if not username:
        return None

    env_name = API_ENV_VARS.get(username)
    if env_name:
        value = os.getenv(env_name)
        if value:
            return value

    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, username)
    except KeyringError as exc:
        log.warning("读取密码失败：%s", exc)
        return None


def set_password(username: str, password: str) -> bool:
    if not username:
        return False

    # SDCC API 凭证不再走 keyring，统一改为环境变量配置。
    if username in API_ENV_VARS:
        log.warning("SDCC API 凭证请设置环境变量：%s", API_ENV_VARS[username])
        return False

    if keyring is None:
        return False
    try:
        keyring.set_password(SERVICE_NAME, username, password)
        return True
    except KeyringError as exc:
        log.warning("保存密码失败：%s", exc)
        return False


def delete_password(username: str) -> bool:
    if not username:
        return False
    if username in API_ENV_VARS:
        return False
    if keyring is None:
        return False
    try:
        keyring.delete_password(SERVICE_NAME, username)
        return True
    except KeyringError:
        return False


def has_password(username: str) -> bool:
    if not username:
        return False

    env_name = API_ENV_VARS.get(username)
    if env_name and os.getenv(env_name):
        return True

    return bool(get_password(username))
