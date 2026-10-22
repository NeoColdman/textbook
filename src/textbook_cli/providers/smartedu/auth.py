"""SmartEdu MAC signatures, adapted from tchMaterial-parser (MIT).

Copyright (c) 2026 肥宅水水呀. See LICENSE and THIRD_PARTY_NOTICES.md.
"""

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import unquote, urlsplit

from textbook_cli.core.models import Credentials


def generate_nonce(diff: int = 0) -> str:
    alphabet = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{int(time.time() * 1000) + diff}:{suffix}"


def signature_string(url: str, method: str, nonce: str) -> str:
    parts = urlsplit(url)
    relative = unquote(parts.path) + (f"?{parts.query}" if parts.query else "")
    return f"{nonce}\n{method.upper()}\n{relative}\n{parts.hostname or ''}\n"


def sign_mac(text: str, mac_key: str) -> str:
    digest = hmac.new(mac_key.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_nd_auth(
    url: str,
    method: str = "GET",
    credentials: Credentials | None = None,
    *,
    nonce: str | None = None,
) -> str:
    if credentials is None:
        return 'MAC id="0",nonce="0",mac="0"'
    value = nonce or generate_nonce(credentials.diff)
    mac = sign_mac(signature_string(url, method, value), credentials.mac_key)
    return f'MAC id="{credentials.access_token}",nonce="{value}",mac="{mac}"'
