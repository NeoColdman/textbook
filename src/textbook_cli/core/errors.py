"""统一错误与输出脱敏；导入时不执行 I/O。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Credentials


def redact(text: object, credentials: Credentials | None = None) -> str:
    value = str(text)
    if credentials:
        for secret in (credentials.access_token, credentials.mac_key):
            if secret:
                value = value.replace(secret, "<已隐藏>")
    value = re.sub(
        r"(?i)([?&](?:access_?token|token|mac_key|authorization|signature|cookie)=)[^&\s\"'<>]+",
        r"\1<已隐藏>",
        value,
    )
    value = re.sub(
        r'(?im)\b(Authorization|X-ND-AUTH|Cookie|Set-Cookie)\s*:\s*[^\r\n]+',
        r"\1: <已隐藏>",
        value,
    )
    value = re.sub(
        r'''(?i)(["']?(?:access_token|mac_key|refresh_token)["']?\s*[:=]\s*)["']?[^\s,}\r\n"']+["']?''',
        r"\1<已隐藏>",
        value,
    )
    return value


class AppError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 1):
        self.code = code
        self.message = redact(message)
        self.exit_code = exit_code
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class Cancelled(AppError):
    def __init__(self, message: str = "下载已取消"):
        super().__init__("CANCELLED", message, 130)
