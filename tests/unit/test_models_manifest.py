import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from textbook_cli.core.errors import AppError
from textbook_cli.core.models import BatchResult, Book, CatalogSnapshot, DownloadResult
from textbook_cli.storage.manifest import Manifest

pytestmark = pytest.mark.contract


def test_multi_path_search_display_does_not_change_primary_path():
    book = Book(
        "resource", "教材", category_paths=(("小学", "数学"), ("初中", "数学")),
        path_tags=({"stage": "小学", "subject": "数学"}, {"stage": "初中", "subject": "数学"}),
    )
    entry = book.to_search_dict([1])
    assert entry["stage"] == "初中"
    assert entry["category_path"] == ["初中", "数学"]
    assert entry["primary_category_path"] == list(book.primary_category_path)
    assert Book.from_dict(json.loads(json.dumps(book.to_dict()))) == book


def test_cache_round_trip_and_schema_refusal():
    snapshot = CatalogSnapshot([Book("id", "教材")], fetched_at="now", checked_at="now")
    assert CatalogSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict()))) == snapshot
    with pytest.raises(ValueError):
        CatalogSnapshot.from_dict({"schema_version": 999})


@pytest.mark.parametrize("statuses,expected", [
    ([], 0), (["completed"], 0), (["skipped"], 0), (["failed"], 1),
    (["completed", "failed"], 4), (["skipped", "failed"], 4),
    (["cancelled", "completed"], 130),
])
def test_batch_exit_codes(statuses, expected):
    batch = BatchResult(1, [DownloadResult("r", "a", status) for status in statuses])
    assert batch.exit_code == expected
    assert sum(batch.to_dict()["summary"][s] for s in ("completed", "skipped", "failed", "cancelled")) == len(statuses)


def test_auth_failure_and_cancel_priority():
    result = DownloadResult("r", None, "failed", error={"code": "AUTH_REQUIRED", "message": "需要凭据"})
    assert BatchResult(1, [result]).exit_code == 3
    assert BatchResult(1, [result], cancelled=True).exit_code == 130


def test_manifest_round_trip_filters_unknown_fields(tmp_path):
    record = {
        "resource_id": "r", "artifact_id": "a", "relative_path": "a.pdf",
        "final_sha256": hashlib.sha256(b"pdf").hexdigest(), "final_size": 3,
        "access_token": "must-not-persist", "private_url": "must-not-persist",
    }
    with Manifest(tmp_path) as manifest:
        manifest.put(record)
        assert manifest.get("r", "missing") is None
        saved = manifest.get("r", "a")
        assert saved["final_sha256"] == record["final_sha256"]
        assert "access_token" not in saved and "private_url" not in saved
    assert b"must-not-persist" not in (tmp_path / ".textbook" / "manifest.sqlite3").read_bytes()


def test_manifest_corrupt_database_and_incompatible_version(tmp_path):
    location = tmp_path / ".textbook"
    location.mkdir()
    database = location / "manifest.sqlite3"
    database.write_text("broken", encoding="utf-8")
    with pytest.raises(AppError):
        Manifest(tmp_path)
    database.unlink()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(AppError):
        Manifest(tmp_path)


def test_importing_core_does_not_load_graphics_or_perform_io():
    code = '''
import socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError("导入不能联网")
socket.create_connection = forbidden
import textbook_cli.core.models, textbook_cli.core.errors
assert not any(name in sys.modules for name in ("tkinter", "PIL", "sv_ttk", "psutil", "win32api"))
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
