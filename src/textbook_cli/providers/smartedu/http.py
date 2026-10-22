"""Bounded HTTP access to explicitly allowed SmartEdu hosts.

Signature protocol adapted from tchMaterial-parser, Copyright (c) 2026
肥宅水水呀, MIT. No upstream global session or credential state is used.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from textbook_cli.core.errors import AppError, Cancelled
from textbook_cli.core.models import Credentials

from .auth import build_nd_auth

METADATA_HOSTS = frozenset(
    f"s-file-{number}.ykt.cbern.com.cn" for number in (1, 2, 3)
)
PRIVATE_HOSTS = frozenset(
    f"r{number}-ndr-private.ykt.cbern.com.cn" for number in (1, 2, 3)
)
ALLOWED_HOSTS = METADATA_HOSTS | PRIVATE_HOSTS
TIMEOUT = (10, 60)
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ATTEMPTS = 3


def validate_url(url: str) -> str:
    """Reject off-platform URLs before creating a request or attaching secrets."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except (ValueError, TypeError):
        raise AppError("INVALID_INPUT", "平台文件地址格式无效。", 2) from None
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or any(char in url for char in ("\r", "\n", "\x00"))
    ):
        raise AppError("UNSUPPORTED_RESOURCE", "拒绝访问未经允许的平台文件主机或地址。")
    return url


def mirror_urls(url: str) -> tuple[str, ...]:
    validate_url(url)
    parts = urlsplit(url)
    if parts.hostname not in PRIVATE_HOSTS:
        return (url,)
    others = sorted(PRIVATE_HOSTS - {parts.hostname})
    return (url, *(urlunsplit(parts._replace(netloc=host)) for host in others))


def classify_response(response: requests.Response, credentials: Credentials | None = None) -> AppError:
    """Classify without including a server body, URL query or authentication data."""
    status = response.status_code
    if status in (401, 403):
        if credentials is None:
            return AppError("AUTH_REQUIRED", f"平台拒绝匿名访问（HTTP {status}），请提供自己的登录凭据。", 3)
        return AppError("AUTH_REJECTED", f"凭据或资源权限被平台拒绝（HTTP {status}）。", 3)
    if status == 404:
        return AppError("RESOURCE_NOT_FOUND", "平台资源不存在或已下架（HTTP 404）。")
    if status == 429:
        return AppError("RATE_LIMITED", "平台请求受到限流（HTTP 429），请稍后重试。")
    if status == 400:
        return AppError("PROVIDER_REQUEST_REJECTED", "平台拒绝请求（HTTP 400），可能与签名或请求参数有关。")
    return AppError("NETWORK_ERROR", f"平台返回异常响应（HTTP {status}）。")


def retry_delay(response: requests.Response | None, attempt: int) -> float:
    if response is not None and response.status_code == 429:
        value = response.headers.get("Retry-After", "").strip()
        delay: float | None = None
        try:
            if value:
                delay = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
        if delay is not None:
            if delay > 60:
                raise AppError("RATE_LIMITED", "平台要求等待超过 60 秒，本次任务已结束，请稍后重试。")
            return max(0.0, delay)
    return 1.0 if attempt <= 1 else 3.0


class SmartEduClient:
    def __init__(
        self,
        credentials: Credentials | None = None,
        cancel_event: threading.Event | None = None,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.credentials = credentials
        self.cancel_event = cancel_event
        self._session_factory = session_factory
        self._local = threading.local()
        self._sleeper = sleeper
        self._clock = clock
        self._pace_lock = threading.Lock()
        self._last_json_start = 0.0

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = self._session_factory()
        return self._local.session

    def close(self) -> None:
        if hasattr(self._local, "session"):
            self._local.session.close()
            del self._local.session

    def _check_cancel(self, cancel: threading.Event | None = None) -> None:
        if (cancel is not None and cancel.is_set()) or (
            self.cancel_event is not None and self.cancel_event.is_set()
        ):
            raise Cancelled()

    def wait(self, seconds: float, cancel: threading.Event | None = None) -> None:
        self._check_cancel(cancel)
        if self._sleeper is not None:
            self._sleeper(seconds)
            self._check_cancel(cancel)
            return
        end = self._clock() + seconds
        while self._clock() < end:
            self._check_cancel(cancel)
            time.sleep(max(0.0, min(0.1, end - self._clock())))

    def _headers(self, url: str, credentials: Credentials | None) -> dict[str, str]:
        result = {
            "User-Agent": "textbook-cli/0.1 (+https://basic.smartedu.cn)",
            "Origin": "https://basic.smartedu.cn",
            "Referer": "https://basic.smartedu.cn/",
            "Accept-Encoding": "identity",
        }
        if urlsplit(url).hostname in PRIVATE_HOSTS:
            result["X-ND-AUTH"] = build_nd_auth(url, credentials=credentials)
            if credentials is not None:
                result["Authorization"] = f"Bearer {credentials.access_token}"
        return result

    def open_stream_once(
        self,
        url: str,
        credentials: Credentials | None = None,
        cancel: threading.Event | None = None,
    ) -> requests.Response:
        current = validate_url(url)
        active = credentials if credentials is not None else self.credentials
        for redirect_count in range(4):
            self._check_cancel(cancel)
            # Sign exactly the prepared URL Requests will send (not an unescaped path).
            prepared_url = requests.Request("GET", current).prepare().url
            if prepared_url is None:
                raise AppError("INVALID_INPUT", "无法构造平台请求地址。", 2)
            validate_url(prepared_url)
            try:
                response = self._session().get(
                    prepared_url,
                    headers=self._headers(prepared_url, active),
                    timeout=TIMEOUT,
                    stream=True,
                    allow_redirects=False,
                )
            except requests.RequestException:
                self._check_cancel(cancel)
                raise AppError("NETWORK_ERROR", "连接平台失败或超时，请检查网络后重试。") from None
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            response.close()
            if redirect_count == 3 or not location:
                raise AppError("NETWORK_ERROR", "平台重定向次数过多或缺少目标地址。")
            current = validate_url(urljoin(prepared_url, location))
        raise AppError("NETWORK_ERROR", "平台重定向失败。")

    def classify_response(self, response: requests.Response) -> AppError:
        return classify_response(response, self.credentials)

    @staticmethod
    def retry_delay(response: requests.Response | None, attempt: int) -> float:
        return retry_delay(response, attempt)

    def get_json(
        self,
        url: str,
        credentials: Credentials | None = None,
        cancel: threading.Event | None = None,
    ):
        validate_url(url)
        last_error: AppError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._check_cancel(cancel)
            with self._pace_lock:
                remaining = 0.2 - (self._clock() - self._last_json_start)
                if remaining > 0:
                    self.wait(remaining, cancel)
                self._last_json_start = self._clock()
            response = None
            try:
                response = self.open_stream_once(url, credentials=credentials, cancel=cancel)
                if response.status_code != 200:
                    last_error = classify_response(response, credentials or self.credentials)
                    retryable = response.status_code in (429, 502, 503, 504) or (
                        response.status_code == 400 and urlsplit(url).hostname in PRIVATE_HOSTS
                    )
                    if not retryable or attempt == MAX_ATTEMPTS:
                        raise last_error
                else:
                    content = bytearray()
                    for chunk in response.iter_content(64 * 1024):
                        self._check_cancel(cancel)
                        content.extend(chunk)
                        if len(content) > MAX_JSON_BYTES:
                            raise AppError("PROVIDER_SCHEMA_CHANGED", "平台元数据超过允许大小，已停止读取。")
                    try:
                        return json.loads(content.decode("utf-8-sig"))
                    except (ValueError, UnicodeError):
                        raise AppError("PROVIDER_SCHEMA_CHANGED", "平台返回了无法解析的 JSON 元数据。") from None
            except requests.RequestException:
                last_error = AppError("NETWORK_ERROR", "读取平台元数据失败或超时。")
                if attempt == MAX_ATTEMPTS:
                    raise last_error from None
            except AppError as error:
                if error.code != "NETWORK_ERROR" or response is not None or attempt == MAX_ATTEMPTS:
                    raise
                last_error = error
            finally:
                if response is not None:
                    response.close()
            delay = retry_delay(response, attempt)
            self.wait(delay, cancel)
        raise last_error or AppError("NETWORK_ERROR", "平台元数据请求失败。")
