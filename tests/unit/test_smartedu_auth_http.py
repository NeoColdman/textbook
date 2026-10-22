"""Offline protocol contracts; credentials below are synthetic."""

import json
import threading

import pytest
import requests

from textbook_cli.core.errors import AppError, Cancelled
from textbook_cli.core.models import Credentials
from textbook_cli.providers.smartedu.auth import build_nd_auth, sign_mac, signature_string
from textbook_cli.providers.smartedu.http import (
    SmartEduClient,
    classify_response,
    mirror_urls,
    retry_delay,
    validate_url,
)

PRIVATE = "https://r1-ndr-private.ykt.cbern.com.cn/book.pdf"

pytestmark = pytest.mark.contract
PUBLIC = "https://s-file-1.ykt.cbern.com.cn/catalog.json"


def response(status=200, value=None, *, headers=None, body=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result._content = body if body is not None else json.dumps(value).encode()
    result._content_consumed = True
    return result


class FakeSession:
    def __init__(self, effects):
        self.effects = list(effects)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        effect = self.effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        effect.url = url
        return effect

    def close(self):
        self.closed = True


def client_for(effects, credentials=None):
    session = FakeSession(effects)
    waits = []
    client = SmartEduClient(credentials, session_factory=lambda: session, sleeper=waits.append)
    return client, session, waits


@pytest.mark.parametrize("url,nonce,expected", [
    ("https://example.com/v1/users/1?with_ext=true", "1:ABCDEFGH", "VELg/RgBxeRZJWpku3Ggvkqu18Czem4bYh3D/8r6URA="),
    ("https://r1-ndr-private.ykt.cbern.com.cn/edu_product/esp/assets/demo.pkg/%E4%B8%AD%E6%96%87%20%E6%95%99%E6%9D%90.pdf", "3:ABCDEFGH", "GNgACOhD4wact4qOSsix4gsp9X3oPTX6/Ct0FGFx9II="),
])
def test_signature_matches_independently_calculated_upstream_vectors(url, nonce, expected):
    text = signature_string(url, "GET", nonce)
    assert text.endswith("\n")
    assert sign_mac(text, "test-mac-key") == expected
    assert build_nd_auth(url, credentials=Credentials("tok", "test-mac-key"), nonce=nonce).endswith(f'mac="{expected}"')


@pytest.mark.parametrize("url", [
    "http://r1-ndr-private.ykt.cbern.com.cn/x", "https://evil.example/x",
    "https://r1-ndr-private.ykt.cbern.com.cn.evil.example/x",
    "https://user@r1-ndr-private.ykt.cbern.com.cn/x",
    "https://r1-ndr-private.ykt.cbern.com.cn:444/x", PRIVATE + "#fragment",
])
def test_rejects_unapproved_urls_before_network(url):
    client, session, _ = client_for([])
    with pytest.raises(AppError):
        client.open_stream_once(url)
    assert session.calls == []


def test_metadata_never_receives_credentials_and_redirect_is_resigned():
    credentials = Credentials("synthetic-token", "synthetic-key")
    client, session, _ = client_for([
        response(302, headers={"Location": PRIVATE}), response(200, value={}),
    ], credentials)
    result = client.open_stream_once(PUBLIC)
    result.close()
    assert "Authorization" not in session.calls[0][1]["headers"]
    assert "X-ND-AUTH" not in session.calls[0][1]["headers"]
    assert session.calls[1][1]["headers"]["Authorization"] == "Bearer synthetic-token"
    assert session.calls[1][1]["headers"]["X-ND-AUTH"].startswith('MAC id="synthetic-token"')
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert all(call[1]["timeout"] == (10, 60) for call in session.calls)


def test_cross_host_redirect_to_unknown_host_never_receives_a_request():
    client, session, _ = client_for([response(302, headers={"Location": "https://evil.example/?token=secret"})])
    with pytest.raises(AppError, match="未经允许"):
        client.open_stream_once(PRIVATE)
    assert len(session.calls) == 1


def test_private_redirect_gets_a_new_host_signature(monkeypatch):
    monkeypatch.setattr("textbook_cli.providers.smartedu.auth.generate_nonce", lambda diff: "1:ABCDEFGH")
    client, session, _ = client_for([
        response(302, headers={"Location": "https://r2-ndr-private.ykt.cbern.com.cn/book.pdf"}), response(200),
    ], Credentials("tok", "key"))
    client.open_stream_once(PRIVATE).close()
    assert session.calls[0][1]["headers"]["X-ND-AUTH"] != session.calls[1][1]["headers"]["X-ND-AUTH"]


def test_redirect_limit_is_bounded():
    client, session, _ = client_for([response(302, headers={"Location": PRIVATE}) for _ in range(4)])
    with pytest.raises(AppError, match="重定向"):
        client.open_stream_once(PRIVATE)
    assert len(session.calls) == 4


def test_json_retries_connection_and_transient_status_with_total_budget():
    client, session, waits = client_for([requests.Timeout(), response(503), response(200, {"ok": True})])
    assert client.get_json(PUBLIC) == {"ok": True}
    assert len(session.calls) == 3
    assert 1 in waits and 3 in waits


def test_json_body_failure_consumes_same_attempt_budget():
    broken = response(200)

    def chunks(_size):
        yield b'{"partial":'
        raise requests.exceptions.ChunkedEncodingError("contains-secret")

    broken.iter_content = chunks
    client, session, _ = client_for([broken, response(200, {"complete": True})])
    assert client.get_json(PUBLIC) == {"complete": True}
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 400, 500, 501])
def test_json_permanent_status_does_not_retry(status):
    client, session, _ = client_for([response(status)])
    with pytest.raises(AppError):
        client.get_json(PUBLIC)
    assert len(session.calls) == 1


def test_private_mapping_400_is_retried_same_url_but_not_called_expired():
    client, session, _ = client_for([response(400), response(400), response(400)])
    with pytest.raises(AppError) as caught:
        client.get_json(PRIVATE)
    assert caught.value.code == "PROVIDER_REQUEST_REJECTED"
    assert [url for url, _ in session.calls] == [PRIVATE] * 3


def test_retry_after_over_limit_fails_without_waiting():
    client, session, waits = client_for([response(429, headers={"Retry-After": "61"})])
    with pytest.raises(AppError) as caught:
        client.get_json(PUBLIC)
    assert caught.value.code == "RATE_LIMITED"
    assert len(session.calls) == 1
    assert 61 not in waits
    assert retry_delay(response(429, headers={"Retry-After": "5"}), 1) == 5


def test_unparseable_or_oversized_json_is_schema_failure(monkeypatch):
    client, session, _ = client_for([response(200, body=b"<html>login</html>")])
    with pytest.raises(AppError) as caught:
        client.get_json(PUBLIC)
    assert caught.value.code == "PROVIDER_SCHEMA_CHANGED"
    assert len(session.calls) == 1
    monkeypatch.setattr("textbook_cli.providers.smartedu.http.MAX_JSON_BYTES", 3)
    client, _, _ = client_for([response(200, {"too": "large"})])
    with pytest.raises(AppError) as caught:
        client.get_json(PUBLIC)
    assert caught.value.code == "PROVIDER_SCHEMA_CHANGED"


def test_cancellation_and_error_messages_never_include_raw_server_exception():
    event = threading.Event()
    event.set()
    client = SmartEduClient(cancel_event=event)
    with pytest.raises(Cancelled):
        client.get_json(PUBLIC)
    client, _, _ = client_for([requests.Timeout("synthetic-secret")] * 3)
    with pytest.raises(AppError) as caught:
        client.get_json(PUBLIC)
    assert "synthetic-secret" not in str(caught.value)


def test_status_classification_and_mirrors():
    assert classify_response(response(401)).code == "AUTH_REQUIRED"
    assert classify_response(response(403), Credentials("tok", "key")).code == "AUTH_REJECTED"
    assert len(mirror_urls(PRIVATE)) == 3
    assert mirror_urls(PUBLIC) == (PUBLIC,)
    assert validate_url(PRIVATE) == PRIVATE
