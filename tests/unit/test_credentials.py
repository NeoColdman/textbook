import json

import pytest

from textbook_cli.core.errors import AppError, redact
from textbook_cli.core.models import Credentials
from textbook_cli.storage.credentials import CredentialStore, parse_credentials, read_credentials_file

pytestmark = pytest.mark.contract


def payload(**updates):
    return json.dumps({"access_token": "synthetic-access", "mac_key": "synthetic-key", "diff": 0, **updates})


@pytest.mark.parametrize("diff", [True, 1.5, "0", None])
def test_rejects_non_integer_clock_diff(diff):
    with pytest.raises(AppError) as error:
        parse_credentials(payload(diff=diff))
    assert error.value.exit_code == 2


def test_parses_console_wrapped_json_and_safe_repr():
    credentials = parse_credentials(json.dumps(payload()))
    assert credentials.access_token == "synthetic-access"
    assert "synthetic" not in repr(credentials)


@pytest.mark.parametrize("value", ["", "\nnot-json", "[]", '{}', payload(mac_key="bad\nheader")])
def test_invalid_values_are_not_echoed(value):
    with pytest.raises(AppError) as error:
        parse_credentials(value)
    assert "bad" not in str(error.value)


def test_credentials_file_is_bounded_and_utf8(tmp_path):
    path = tmp_path / "用户凭据.json"
    path.write_text(payload(), encoding="utf-8-sig")
    assert read_credentials_file(path).diff == 0
    path.write_bytes(b"x" * 65537)
    with pytest.raises(AppError, match="64 KiB"):
        read_credentials_file(path)
    with pytest.raises(AppError):
        read_credentials_file(tmp_path / "missing")


class MemoryBackend:
    value = None

    def get_password(self, service, account):
        assert service == "textbook-cli" and account == "default"
        return self.value

    def set_password(self, service, account, value):
        self.value = value

    def delete_password(self, service, account):
        self.value = None


def test_save_status_clear_and_does_not_touch_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backend = MemoryBackend()
    store = CredentialStore(backend)
    assert store.load() is None
    credentials = parse_credentials(payload())
    store.save(credentials)
    assert store.load() == credentials
    assert store.status()["present"] is True
    assert "synthetic" not in json.dumps(store.status())
    store.clear()
    store.clear()
    assert store.load() is None
    assert not list(tmp_path.iterdir())


def test_backend_failure_never_discloses_exception_or_falls_back(tmp_path, monkeypatch):
    class Broken(MemoryBackend):
        def set_password(self, *args):
            raise OSError("synthetic-access synthetic-key")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AppError) as error:
        CredentialStore(Broken()).save(parse_credentials(payload()))
    assert error.value.exit_code == 1
    assert "synthetic" not in str(error.value)
    assert not list(tmp_path.iterdir())


def test_rejects_unsafe_automatically_selected_backend(monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: MemoryBackend())
    with pytest.raises(AppError) as error:
        CredentialStore().load()
    assert error.value.code == "CREDENTIAL_STORE_ERROR"


def test_doctor_backend_status_never_reads_secret():
    class NoRead(MemoryBackend):
        def get_password(self, *args):
            pytest.fail("doctor 不应读取秘密")
    assert CredentialStore(NoRead()).backend_status()["available"]


def test_redaction_hides_known_and_structured_secrets():
    credentials = Credentials("secret-access", "secret-key")
    message = redact(
        'secret-access secret-key https://example.test/a?accessToken=other&signature=third\n'
        'X-ND-AUTH: MAC id="fourth"\n{"mac_key":"fifth"}', credentials
    )
    for value in ("secret-access", "secret-key", "other", "third", "fourth", "fifth"):
        assert value not in message
