"""CLI contracts use injected services, never real credentials or the network."""

import json
import pytest
from typer.testing import CliRunner

from textbook_cli.cli import commands
from textbook_cli.core.errors import AppError
from textbook_cli.core.models import BatchResult, Book, CatalogSnapshot, Credentials, DownloadResult, ProgressEvent
from textbook_cli.storage.paths import AppPaths

pytestmark = pytest.mark.cli


class FakeCatalog:
    def __init__(self):
        self.snapshot = CatalogSnapshot(
            books=[Book("book-a", "数学 七年级 下册", "old-a", category_paths=(("初中", "数学", "人教版"),))],
            fetched_at="2026-10-22T00:00:00+00:00", checked_at="2026-10-22T00:00:00+00:00",
        )
        self.last_warnings = []
        self.refreshes = []

    def cached(self):
        return self.snapshot

    def load(self, refresh=False):
        self.refreshes.append(refresh)
        return self.snapshot

    def status(self):
        return {"exists": True, "books": len(self.snapshot.books)}

    def search(self, query, **kwargs):
        return {"catalog": {"updated_at": self.snapshot.fetched_at, "checked_at": self.snapshot.checked_at, "source": "cache", "stale": False}, "total": 1, "limit": kwargs.get("limit", 20), "offset": kwargs.get("offset", 0), "items": [self.snapshot.books[0].to_search_dict()]}


class FakeStore:
    def __init__(self):
        self.value = None
        self.loads = 0
        self.fail = False

    def load(self):
        self.loads += 1
        if self.fail:
            raise AppError("CREDENTIAL_STORE_ERROR", "测试后端不可用")
        return self.value

    def save(self, value):
        if self.fail:
            raise AppError("CREDENTIAL_STORE_ERROR", "测试后端不可用")
        self.value = value

    def clear(self):
        self.value = None

    def status(self):
        return {"available": True, "present": self.value is not None}

    def backend_status(self):
        return {"available": True, "backend": "test"}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    calls = []
    result = BatchResult(1, [DownloadResult("book-a", "pdf-a", "completed", str(tmp_path / "book.pdf"), bytes=100)], artifacts=1)

    class FakeDownloader:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self, ids, **kwargs):
            calls.append((ids, self.kwargs, kwargs))
            self.kwargs["event_callback"](ProgressEvent("job-a", ids[0], "downloading", downloaded=15, total=100))
            return value.result

    value = commands.Runtime(object(), FakeCatalog(), FakeStore(), AppPaths(tmp_path / "config", tmp_path / "cache"), FakeDownloader)
    value.calls = calls
    value.result = result
    monkeypatch.setattr(commands, "runtime_factory", lambda: value)
    monkeypatch.setattr(commands, "is_interactive", lambda: False)
    return value


@pytest.mark.parametrize("args", [
    ["search", "--json"],
    ["search", "数学", "--limit", "0", "--json"],
    ["search", "数学", "--offset", "-1", "--json"],
    ["search", "数学", "--unknown", "--json"],
    ["download", "--jobs", "4", "--json"],
    ["download", "--json"],
])
def test_json_usage_errors_are_one_object(args, runtime):
    result = CliRunner().invoke(commands.app, args)
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["data"] is None
    assert "\x1b" not in result.stdout
    assert not runtime.calls


def test_no_arguments_and_browse_require_terminal(runtime):
    runner = CliRunner()
    result = runner.invoke(commands.app, [])
    assert result.exit_code == 2
    assert not result.stdout
    assert "search" in result.stderr
    result = runner.invoke(commands.app, ["browse"])
    assert result.exit_code == 2
    assert not runtime.catalog.refreshes


def test_search_json_and_warnings_use_separate_streams(runtime):
    runtime.catalog.last_warnings = ["目录刷新失败，使用旧缓存。"]
    result = CliRunner().invoke(commands.app, ["search", "数学", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["items"][0]["resource_id"] == "book-a"
    assert payload["warnings"] == runtime.catalog.last_warnings
    assert "旧缓存" in result.stderr
    assert runtime.credentials.loads == 0


def test_download_merges_inputs_and_aliases_without_fetching_catalog(runtime, tmp_path):
    inputs = tmp_path / "清单.txt"
    inputs.write_text("book-c\nbook-a\n\n", encoding="utf-8")
    url = "https://basic.smartedu.cn/tchMaterial/detail?contentId=book-a"
    result = CliRunner().invoke(commands.app, ["download", url, "--id", "old-a", "--id", "book-b", "--input", str(inputs), "--json"])
    assert result.exit_code == 0, result.output
    assert runtime.calls[0][0] == ["book-a", "book-b", "book-c"]
    assert not runtime.catalog.refreshes
    assert json.loads(result.stdout)["data"]["summary"]["completed"] == 1
    assert "下载中" in result.stderr


def test_download_preflights_entire_file_before_start(runtime, tmp_path):
    inputs = tmp_path / "bad.txt"
    inputs.write_text("book-a\nhttps://evil.example/x\n", encoding="utf-8")
    result = CliRunner().invoke(commands.app, ["download", "--input", str(inputs), "--json"])
    assert result.exit_code == 2
    assert "第 2 行" in json.loads(result.stdout)["error"]["message"]
    assert not runtime.calls
    assert runtime.credentials.loads == 0


def test_download_positionals_are_pages_not_ids(runtime):
    result = CliRunner().invoke(commands.app, ["download", "book-a", "--json"])
    assert result.exit_code == 2
    assert not runtime.calls


def test_alias_conflict_is_usage_error(runtime):
    runtime.catalog.snapshot.books.append(Book("book-b", "另一教材", "old-a"))
    result = CliRunner().invoke(commands.app, ["download", "--id", "old-a", "--json"])
    assert result.exit_code == 2
    assert "别名冲突" in json.loads(result.stdout)["error"]["message"]


def test_no_keyring_still_allows_public_download_with_warning(runtime):
    runtime.credentials.fail = True
    result = CliRunner().invoke(commands.app, ["download", "--id", "book-a", "--json"])
    assert result.exit_code == 0
    assert runtime.calls[0][1]["credentials"] is None
    assert json.loads(result.stdout)["warnings"]


def test_credentials_file_has_priority_and_is_never_saved_or_printed(runtime, tmp_path):
    runtime.credentials.value = Credentials("saved-fixture-token", "saved-fixture-key", 0)
    path = tmp_path / "凭据.json"
    path.write_text(json.dumps({"access_token": "temporary-fixture-token", "mac_key": "temporary-fixture-key", "diff": 42}), encoding="utf-8")
    result = CliRunner().invoke(commands.app, ["download", "--id", "book-a", "--credentials-file", str(path), "--json"])
    assert result.exit_code == 0, result.output
    assert runtime.credentials.loads == 0
    assert runtime.calls[0][1]["credentials"].diff == 42
    assert runtime.credentials.value.access_token == "saved-fixture-token"
    assert "temporary-fixture" not in result.output


def test_bad_credentials_file_does_not_fall_back(runtime, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"access_token":"do-not-print-this-secret"}', encoding="utf-8")
    result = CliRunner().invoke(commands.app, ["download", "--id", "book-a", "--credentials-file", str(path), "--json"])
    assert result.exit_code == 2
    assert runtime.credentials.loads == 0
    assert not runtime.calls
    assert "do-not-print-this-secret" not in result.output


@pytest.mark.parametrize("batch, expected", [
    (BatchResult(1, [DownloadResult("a", "a", "failed", error={"code": "AUTH_REJECTED", "message": "拒绝访问"})]), 3),
    (BatchResult(2, [DownloadResult("a", "a", "completed"), DownloadResult("b", None, "failed", error={"code": "RESOURCE_NOT_FOUND", "message": "已下架"})]), 4),
    (BatchResult(1, [DownloadResult("a", "a", "failed", error={"code": "NETWORK_ERROR", "message": "超时"})]), 1),
    (BatchResult(1, [DownloadResult("a", "a", "cancelled")], cancelled=True), 130),
])
def test_batch_exit_codes_match_json(runtime, batch, expected):
    runtime.result = batch
    result = CliRunner().invoke(commands.app, ["download", "--id", "book-a", "--json"])
    assert result.exit_code == expected, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["data"]["results"]
    assert payload["error"]


def test_auth_import_stdin_and_local_commands(runtime):
    runner = CliRunner()
    credentials = json.dumps({"access_token": "fixture-token", "mac_key": "fixture-key", "diff": 0})
    result = runner.invoke(commands.app, ["auth", "import", "--file", "-"], input=credentials)
    assert result.exit_code == 0, result.output
    assert runtime.credentials.value.access_token == "fixture-token"
    assert "fixture-token" not in result.output
    result = runner.invoke(commands.app, ["auth", "status"])
    assert result.exit_code == 0
    assert "fixture-token" not in result.output
    assert runner.invoke(commands.app, ["auth", "clear"]).exit_code == 0
    assert runtime.credentials.value is None
    assert runner.invoke(commands.app, ["catalog", "status"]).exit_code == 0
    assert runtime.catalog.refreshes == []
    assert runner.invoke(commands.app, ["doctor"]).exit_code == 0
    assert runtime.credentials.loads == 0


def test_auth_grab_saves_newest_candidate(runtime, monkeypatch):
    from textbook_cli.storage.browser_import import BrowserCredential

    candidates = [  # 契约：candidates[0] 为写入时间最新
        BrowserCredential(Credentials("new", "k", 2), "Chrome", "Default", 99),
        BrowserCredential(Credentials("old", "k", 1), "Chrome", "Default", 10),
    ]
    monkeypatch.setattr(commands, "import_from_browser", lambda: candidates)
    result = CliRunner().invoke(commands.app, ["auth", "grab"])
    assert result.exit_code == 0, result.output
    assert runtime.credentials.value.access_token == "new"
    assert "new" not in result.output
    assert "Default" in result.output


def test_auth_grab_across_databases_does_not_guess_newest(runtime, monkeypatch):
    """写入序号跨浏览器配置不可比；非交互时不做猜测，要求用户选择来源。"""
    from textbook_cli.storage.browser_import import BrowserCredential

    candidates = [
        BrowserCredential(Credentials("stale", "k", 1), "Edge", "Old", 99),
        BrowserCredential(Credentials("fresh", "k", 2), "Chrome", "Default", 3),
    ]
    monkeypatch.setattr(commands, "import_from_browser", lambda: candidates)
    result = CliRunner().invoke(commands.app, ["auth", "grab"])
    assert result.exit_code == 1
    assert runtime.credentials.value is None
    assert "无法判断写入先后" in result.output


def test_auth_grab_across_databases_uses_explicit_choice(runtime, monkeypatch):
    from textbook_cli.storage.browser_import import BrowserCredential

    candidates = [
        BrowserCredential(Credentials("stale", "k", 1), "Edge", "Old", 99),
        BrowserCredential(Credentials("fresh", "k", 2), "Chrome", "Default", 3),
    ]
    monkeypatch.setattr(commands, "import_from_browser", lambda: candidates)
    monkeypatch.setattr(commands, "is_interactive", lambda: True)
    # 先输入无效编号验证重试，再选择列表中的 Chrome（Default）。
    result = CliRunner().invoke(commands.app, ["auth", "grab"], input="9\n1\n")
    assert result.exit_code == 0, result.output
    assert runtime.credentials.value.access_token == "fresh"
    assert "Chrome" in result.output


def test_auth_grab_without_candidates_gives_guidance(runtime, monkeypatch):
    monkeypatch.setattr(commands, "import_from_browser", lambda: [])
    result = CliRunner().invoke(commands.app, ["auth", "grab"])
    assert result.exit_code == 1
    assert runtime.credentials.value is None
    assert "未能在本机" in result.output


def test_auth_import_non_tty_needs_explicit_file(runtime):
    result = CliRunner().invoke(commands.app, ["auth", "import"])
    assert result.exit_code == 2
    assert runtime.credentials.value is None


def test_explicit_refresh_failure_is_error_even_when_cache_exists(runtime, monkeypatch):
    def failure(refresh=False):
        raise AppError("NETWORK_ERROR", "更新失败")
    monkeypatch.setattr(runtime.catalog, "load", failure)
    result = CliRunner().invoke(commands.app, ["catalog", "refresh"])
    assert result.exit_code == 1
    assert "更新失败" in result.stderr


def test_input_stdin_and_no_json_progress_on_stdout(runtime):
    result = CliRunner().invoke(commands.app, ["download", "--input", "-", "--json"], input="book-a\nbook-b\n")
    assert result.exit_code == 0, result.output
    assert runtime.calls[0][0] == ["book-a", "book-b"]
    assert json.loads(result.stdout)["command"] == "download"


def test_credentials_stdin_alias_is_rejected(runtime):
    result = CliRunner().invoke(commands.app, ["download", "--id", "book-a", "--credentials-file", "-", "--json"])
    assert result.exit_code == 2
    assert not runtime.calls


def test_json_item_warnings_and_errors_are_also_redacted(runtime):
    runtime.credentials.value = Credentials("fixture-secret-token", "fixture-secret-key")
    runtime.result = BatchResult(1, [DownloadResult(
        "book-a", "pdf", "failed", error={"code": "NETWORK_ERROR", "message": "失败 fixture-secret-token"},
        warnings=["书签降级 fixture-secret-key"],
    )], warnings=["批次警告 fixture-secret-token"])
    result = CliRunner().invoke(commands.app, ["download", "--id", "book-a", "--json"])
    assert result.exit_code == 1
    assert "fixture-secret-token" not in result.stdout + result.stderr
    assert "fixture-secret-key" not in result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "<已隐藏>" in payload["data"]["results"][0]["warnings"][0]
    assert "<已隐藏>" in payload["data"]["results"][0]["error"]["message"]

