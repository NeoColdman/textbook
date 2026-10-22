"""用户凭据的显式导入与系统安全后端存储。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..core.errors import AppError
from ..core.models import Credentials


def parse_credentials(raw: str) -> Credentials:
    try:
        data = json.loads(raw)
        if isinstance(data, str):
            data = json.loads(data)
    except (ValueError, TypeError):
        raise AppError("INVALID_INPUT", "凭据必须是包含 access_token、mac_key、diff 的 JSON", 2) from None
    if not isinstance(data, dict):
        raise AppError("INVALID_INPUT", "凭据 JSON 必须是对象", 2)
    for name in ("access_token", "mac_key"):
        if not isinstance(data.get(name), str) or not data[name].strip():
            raise AppError("INVALID_INPUT", f"凭据缺少有效字段 {name}", 2)
        if any(char in data[name] for char in ("\r", "\n", '"')):
            raise AppError("INVALID_INPUT", f"凭据字段 {name} 含有不支持的字符", 2)
    if type(data.get("diff")) is not int:
        raise AppError("INVALID_INPUT", "凭据 diff 必须是整数", 2)
    return Credentials(data["access_token"].strip(), data["mac_key"], data["diff"])


def read_credentials_file(path: Path) -> Credentials:
    try:
        if path.stat().st_size > 65536:
            raise AppError("INVALID_INPUT", "凭据文件超过 64 KiB 限制", 2)
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise AppError("INVALID_INPUT", "无法读取 UTF-8 凭据文件，请检查路径和权限", 2) from None
    return parse_credentials(raw)


class CredentialStore:
    SERVICE = "textbook-cli"
    ACCOUNT = "default"
    SAFE_BACKENDS = {
        "keyring.backends.macOS.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.kwallet.DBusKeyring",
        "keyring.backends.kwallet.DBusKeyringKWallet4",
    }

    def __init__(self, backend=None):
        self._injected = backend is not None
        self._backend = backend

    def _get_backend(self):
        if self._backend is None:
            try:
                import keyring
                backend = keyring.get_keyring()
                name = type(backend).__module__ + "." + type(backend).__name__
                if name == "keyring.backends.chainer.ChainerBackend":
                    backend = next(
                        (b for b in backend.backends
                         if type(b).__module__ + "." + type(b).__name__ in self.SAFE_BACKENDS), None
                    )
                if backend is None:
                    raise ValueError("没有系统安全后端")
                name = type(backend).__module__ + "." + type(backend).__name__
                if name not in self.SAFE_BACKENDS:
                    raise ValueError("不支持的凭据后端")
                self._backend = backend
            except Exception:
                raise AppError(
                    "CREDENTIAL_STORE_ERROR",
                    "系统安全凭据库不可用。可在向导中仅本次使用，或下载时指定 --credentials-file。",
                ) from None
        return self._backend

    def load(self) -> Credentials | None:
        backend = self._get_backend()
        try:
            raw = backend.get_password(self.SERVICE, self.ACCOUNT)
        except Exception:
            raise AppError("CREDENTIAL_STORE_ERROR", "无法读取系统凭据库，请检查后端是否解锁") from None
        if raw is None:
            return None
        try:
            return parse_credentials(raw)
        except AppError:
            raise AppError("CREDENTIAL_STORE_ERROR", "已保存的凭据格式无效，请重新导入") from None

    def save(self, credentials: Credentials) -> None:
        raw = json.dumps(asdict(credentials), ensure_ascii=False)
        parse_credentials(raw)
        try:
            self._get_backend().set_password(self.SERVICE, self.ACCOUNT, raw)
        except AppError:
            raise
        except Exception:
            raise AppError("CREDENTIAL_STORE_ERROR", "凭据未保存，请检查系统凭据库是否解锁") from None

    def clear(self) -> None:
        backend = self._get_backend()
        try:
            if backend.get_password(self.SERVICE, self.ACCOUNT) is not None:
                backend.delete_password(self.SERVICE, self.ACCOUNT)
        except Exception:
            raise AppError("CREDENTIAL_STORE_ERROR", "无法删除已保存的凭据") from None

    def backend_status(self) -> dict:
        """只检查后端类型，不读取秘密值，用于 doctor。"""
        try:
            backend = self._get_backend()
            return {"available": True, "backend": type(backend).__module__ + "." + type(backend).__name__}
        except AppError as exc:
            return {"available": False, "backend": None, "error": exc.to_dict()}

    def status(self) -> dict:
        info = self.backend_status()
        if not info["available"]:
            raise AppError("CREDENTIAL_STORE_ERROR", info["error"]["message"])
        info["present"] = self.load() is not None
        info["platform_validated"] = False
        return info
