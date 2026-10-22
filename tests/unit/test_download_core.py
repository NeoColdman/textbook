import hashlib
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from requests import ConnectionError

from textbook_cli.core.errors import AppError
from textbook_cli.core.models import Artifact, Credentials
from textbook_cli.download.engine import DownloadService
from textbook_cli.download.integrity import verify_file
from textbook_cli.download.paths import artifact_path, publish_file, safe_path, sanitize_filename
from textbook_cli.storage.manifest import Manifest

pytestmark = pytest.mark.contract

PDF = b"%PDF-1.7\n" + b"minimal-test-content\n" * 100


class Response:
    def __init__(self, body=PDF, status=200, headers=None, chunks=None):
        self.body, self.status_code = body, status
        self.headers = {"Content-Length": str(len(body))} if headers is None else headers
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        if self.chunks is not None:
            yield from self.chunks()
        else:
            for offset in range(0, len(self.body), chunk_size):
                yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Provider:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def resolve(self, resource_id, **kwargs):
        value = self.artifacts[resource_id]
        if isinstance(value, Exception):
            raise value
        return value


class ClientFactory:
    def __init__(self, responses=None):
        self.responses = list(responses or [Response()])
        self.urls = []
        self.clients = []
        self.lock = threading.Lock()

    def __call__(self, **kwargs):
        owner = self

        class Client:
            def open_stream_once(self, url, **kwargs):
                with owner.lock:
                    owner.urls.append(url)
                    item = owner.responses.pop(0) if len(owner.responses) > 1 else owner.responses[0]
                if isinstance(item, Exception):
                    raise item
                return item

            def classify_response(self, response):
                if response.status_code in (401, 403):
                    return AppError("AUTH_REJECTED", "认证被拒绝")
                return AppError("NETWORK_ERROR" if response.status_code >= 500 else "HTTP_ERROR", f"HTTP {response.status_code}")

            def retry_delay(self, response, attempt):
                return 0

            def close(self):
                pass

        client = Client()
        self.clients.append(client)
        return client


def artifact(resource_id="book-001", **kwargs):
    return Artifact(resource_id, "file-001", "义务教育 教材", "https://r1-ndr-private.ykt.cbern.com.cn/a.pdf", **kwargs)


def service(items=None, responses=None, callback=None, **kwargs):
    factory = ClientFactory(responses)
    instance = DownloadService(Provider(items or {"book-001": [artifact()]}), event_callback=callback, client_factory=factory, **kwargs)
    instance._minimum_interval = 0
    return instance, factory


def test_completed_hashes_manifest_and_verified_skip(tmp_path):
    book = artifact(expected_size=len(PDF), expected_md5=hashlib.md5(PDF).hexdigest())
    downloader, factory = service({book.resource_id: [book]})
    result = downloader.run([book.resource_id, book.resource_id], tmp_path)
    item = result.results[0]
    assert result.exit_code == 0 and result.requested == 1 and result.artifacts == 1
    assert Path(item.path).read_bytes() == PDF
    with Manifest(tmp_path) as manifest:
        record = manifest.get(book.resource_id, book.artifact_id)
    assert record["final_sha256"] == hashlib.sha256(PDF).hexdigest()
    assert record["source_md5"] == hashlib.md5(PDF).hexdigest()
    second = downloader.run([book.resource_id], tmp_path)
    assert second.results[0].status == "skipped"
    assert len(factory.urls) == 1
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("body", [b"", b"<html>login</html>", b"some bytes"])
def test_non_pdf_never_published(tmp_path, body):
    downloader, _ = service(responses=[Response(body)])
    result = downloader.run(["book-001"], tmp_path)
    assert result.exit_code == 1
    assert result.results[0].error["code"] == "INTEGRITY_FAILED"
    assert not list(tmp_path.rglob("*.pdf"))
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("options", [{"expected_size": 1}, {"expected_md5": "0" * 32}])
def test_platform_integrity_mismatch_fails(tmp_path, options):
    downloader, _ = service({"book-001": [artifact(**options)]})
    result = downloader.run(["book-001"], tmp_path)
    assert result.results[0].error["code"] == "INTEGRITY_FAILED"
    assert not list(tmp_path.rglob("*.pdf"))


def test_missing_platform_integrity_fields_are_visible_as_warning(tmp_path):
    downloader, _ = service()
    result = downloader.run(["book-001"], tmp_path)
    assert result.exit_code == 0
    assert any("未验证缺失指标" in warning for warning in result.results[0].warnings)


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "bad"}, {"Content-Length": "-1"}, {"Content-Length": "1", "Content-Encoding": "gzip"}])
def test_unknown_and_compressed_lengths(tmp_path, headers):
    downloader, _ = service(responses=[Response(headers=headers)])
    assert downloader.run(["book-001"], tmp_path).results[0].status == "completed"


def test_stream_failure_and_network_errors_share_three_attempt_budget(tmp_path):
    def interrupted():
        yield PDF[:16]
        raise ConnectionError("断流")

    events = []
    downloader, factory = service(responses=[Response(chunks=interrupted), ConnectionError("断线"), Response()], callback=events.append)
    result = downloader.run(["book-001"], tmp_path)
    assert result.exit_code == 0 and len(factory.urls) == 3
    assert [event.attempt for event in events if event.stage == "connecting"] == [1, 2, 3]
    assert Path(result.results[0].path).read_bytes() == PDF
    assert not list(tmp_path.rglob("*.part"))


def test_persistent_network_failure_has_three_attempts(tmp_path):
    downloader, factory = service(responses=[AppError("NETWORK_ERROR", "unavailable")])
    # Retry waits are injectable by replacing the cancellation event's wait only
    # for this unit test; actual wait cancellation is covered separately.
    stop = threading.Event()
    stop.wait = lambda _: False
    result = downloader.run(["book-001"], tmp_path, cancel=stop)
    assert result.exit_code == 1 and len(factory.urls) == 3


@pytest.mark.parametrize("status", [401, 403, 404, 500, 501])
def test_auth_and_not_found_do_not_switch_mirrors(tmp_path, status):
    downloader, factory = service(responses=[Response(status=status)])
    result = downloader.run(["book-001"], tmp_path)
    assert len(factory.urls) == 1
    assert result.exit_code == (3 if status in (401, 403) else 1)


def test_400_retries_same_private_host_without_claiming_expiration(tmp_path):
    downloader, factory = service(responses=[Response(status=400), Response(status=400), Response(status=400)])
    result = downloader.run(["book-001"], tmp_path)
    assert len(factory.urls) == 3 and len(set(factory.urls)) == 1
    assert "过期" not in result.results[0].error["message"]


def test_existing_unregistered_and_modified_files_require_overwrite(tmp_path):
    book = artifact()
    target = artifact_path(tmp_path, book)
    target.write_bytes(b"user file")
    downloader, factory = service()
    failed = downloader.run([book.resource_id], tmp_path)
    assert failed.results[0].error["code"] == "FILE_CONFLICT"
    assert target.read_bytes() == b"user file" and not factory.urls
    completed = downloader.run([book.resource_id], tmp_path, overwrite=True)
    assert completed.results[0].status == "completed"
    target.write_bytes(b"changed")
    changed = downloader.run([book.resource_id], tmp_path)
    assert changed.results[0].error["code"] == "FILE_CONFLICT"
    target.unlink()
    assert downloader.run([book.resource_id], tmp_path).results[0].status == "completed"


def test_stable_manifest_path_when_title_and_category_change(tmp_path):
    old = artifact(relative_dir=("小学", "语文", "人教版"))
    downloader, _ = service({old.resource_id: [old]})
    first = downloader.run([old.resource_id], tmp_path).results[0]
    downloader.provider.artifacts[old.resource_id] = [replace(old, title="改名", relative_dir=("其他",))]
    second = downloader.run([old.resource_id], tmp_path).results[0]
    assert second.status == "skipped" and second.path == first.path
    assert "平台内容版本未确认" in " ".join(second.warnings)


def test_duplicate_artifacts_collapsed_and_partial_failures_preserved(tmp_path):
    book = artifact()
    downloader, _ = service({book.resource_id: [book, book], "bad": AppError("RESOURCE_NOT_FOUND", "已移除")})
    result = downloader.run([book.resource_id, "bad"], tmp_path)
    assert result.exit_code == 4 and result.artifacts == 1
    assert len(result.results) == 2 and result.results[1].artifact_id is None


def test_cancel_during_transfer_cleans_only_owned_parts(tmp_path):
    stop = threading.Event()
    owned_elsewhere = tmp_path / "other.part"
    owned_elsewhere.write_bytes(b"not ours")

    def chunks():
        yield PDF[:8]
        stop.set()
        yield PDF[8:]

    downloader, _ = service(responses=[Response(chunks=chunks)])
    result = downloader.run(["book-001"], tmp_path, cancel=stop)
    assert result.exit_code == 130 and result.results[0].status == "cancelled"
    assert list(tmp_path.rglob("*.part")) == [owned_elsewhere]
    assert not list(tmp_path.rglob("*.pdf"))


def test_cancel_on_commit_finishes_consistent_manifest(tmp_path):
    stop = threading.Event()

    def callback(event):
        if event.stage == "committing":
            stop.set()

    downloader, _ = service(callback=callback)
    result = downloader.run(["book-001"], tmp_path, cancel=stop)
    assert result.exit_code == 130 and result.results[0].status == "completed"
    with Manifest(tmp_path) as manifest:
        assert manifest.get("book-001", "file-001") is not None


def test_manifest_commit_failure_keeps_completed_file_and_no_false_record(tmp_path):
    class FailingManifest(Manifest):
        def put(self, record):
            raise OSError("disk full")

    downloader, _ = service(manifest_factory=FailingManifest)
    item = downloader.run(["book-001"], tmp_path).results[0]
    assert item.status == "failed" and "清单登记失败" in item.error["message"]
    assert Path(item.path).read_bytes() == PDF
    downloader.manifest_factory = Manifest
    assert downloader.run(["book-001"], tmp_path).results[0].error["code"] == "FILE_CONFLICT"


def test_callback_runs_only_on_coordinator_thread(tmp_path):
    thread_ids = []
    downloader, factory = service(callback=lambda event: thread_ids.append(threading.get_ident()))
    assert downloader.run(["book-001"], tmp_path).exit_code == 0
    assert set(thread_ids) == {threading.get_ident()}
    assert len(factory.clients) == 1


def test_bookmark_failure_retains_verified_original_and_warning(tmp_path, monkeypatch):
    book = artifact(chapters=[{"title": "one", "page_index": 1}])
    monkeypatch.setattr("textbook_cli.download.engine.add_bookmarks", lambda *args: (_ for _ in ()).throw(ValueError("bad PDF")))
    downloader, _ = service({book.resource_id: [book]})
    result = downloader.run([book.resource_id], tmp_path, bookmarks=True)
    item = result.results[0]
    assert result.exit_code == 0 and item.bookmarks == "failed" and not item.bookmarks_applied
    assert item.warnings and Path(item.path).read_bytes() == PDF
    with Manifest(tmp_path) as manifest:
        assert not manifest.get(book.resource_id, book.artifact_id)["bookmarks_applied"]


def test_publication_is_exclusive_and_does_not_replace_racing_file(tmp_path):
    source, destination = tmp_path / "a.part", tmp_path / "a.pdf"
    source.write_bytes(PDF)
    destination.write_bytes(b"other process")
    with pytest.raises(AppError, match="目标文件已存在"):
        publish_file(source, destination, overwrite=False)
    assert destination.read_bytes() == b"other process" and source.read_bytes() == PDF


def test_other_program_creates_target_immediately_before_commit(tmp_path):
    target = artifact_path(tmp_path, artifact())

    def racing_writer(event):
        if event.stage == "committing":
            target.write_bytes(b"external user file")

    downloader, _ = service(callback=racing_writer)
    result = downloader.run(["book-001"], tmp_path)
    assert result.results[0].error["code"] == "FILE_CONFLICT"
    assert target.read_bytes() == b"external user file"
    with Manifest(tmp_path) as manifest:
        assert manifest.get("book-001", "file-001") is None
    assert not list(tmp_path.rglob("*.part"))


def test_paths_reject_traversal_and_symlink_escape(tmp_path):
    with pytest.raises(AppError):
        safe_path(tmp_path, "../escape.pdf")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    try:
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(AppError):
        safe_path(tmp_path, "link/escape.pdf")


def test_excessive_total_path_has_clear_filesystem_error(tmp_path, monkeypatch):
    monkeypatch.setattr("textbook_cli.download.paths._PATH_LIMIT", len(str(tmp_path)) + 10)
    with pytest.raises(AppError, match="输出路径过长"):
        safe_path(tmp_path, "a" * 20 + "/book.pdf")


@pytest.mark.parametrize("name", ["CON", "aux.txt", "中文?课本*", "a/b\\c", "..", " ", "x" * 300])
def test_sanitized_names_are_safe(name):
    value = sanitize_filename(name)
    assert value and len(value.encode()) <= 120
    assert not any(character in value for character in '/\\:*?"<>|')
    assert not value.endswith((".", " "))


def test_integrity_validation_has_known_independent_hashes(tmp_path):
    path = tmp_path / "book.pdf"
    path.write_bytes(PDF)
    digest = verify_file(path, expected_size=len(PDF), expected_md5=hashlib.md5(PDF).hexdigest())
    assert digest.sha256 == hashlib.sha256(PDF).hexdigest()


def test_errors_redact_credentials(tmp_path):
    credentials = Credentials("private-token", "private-key")
    downloader, _ = service({"book-001": AppError("NETWORK_ERROR", "bad private-token private-key")}, credentials=credentials)
    message = downloader.run(["book-001"], tmp_path).results[0].error["message"]
    assert "private-token" not in message and "private-key" not in message


def test_same_prefix_resources_in_separate_batches_keep_both_files(tmp_path):
    """同标题、同目录且资源 ID 前八位相同的两本教材分批下载时互不覆盖。"""
    first = artifact(resource_id="book-aaaaaaaa-1")
    second = artifact(resource_id="book-aaaaaaaa-2")
    downloader, _ = service({first.resource_id: [first], second.resource_id: [second]})
    one = downloader.run([first.resource_id], tmp_path).results[0]
    assert one.status == "completed"
    original = Path(one.path).read_bytes()
    for overwrite in (False, True):
        two = downloader.run([second.resource_id], tmp_path, overwrite=overwrite).results[0]
        assert two.status == "completed", two.error
        assert Path(two.path) != Path(one.path)
        assert Path(one.path).read_bytes() == original  # 第一本文件从未被替换
    with Manifest(tmp_path) as manifest:
        assert manifest.get(first.resource_id, first.artifact_id)["relative_path"] != \
            manifest.get(second.resource_id, second.artifact_id)["relative_path"]


def test_bookmarks_without_valid_pages_keep_original_and_report_failure(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    import io

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=32, height=32)
    buffer = io.BytesIO()
    writer.write(buffer)
    body = buffer.getvalue()
    book = artifact(chapters=[{"title": "第一章", "page_index": None}], expected_size=len(body))
    downloader, _ = service({book.resource_id: [book]}, responses=[Response(body)])
    result = downloader.run([book.resource_id], tmp_path, bookmarks=True)
    item = result.results[0]
    assert result.exit_code == 0 and item.bookmarks == "failed" and not item.bookmarks_applied
    assert any("没有章节写入有效书签" in warning for warning in item.warnings)
    assert Path(item.path).read_bytes() == body
    with Manifest(tmp_path) as manifest:
        assert not manifest.get(book.resource_id, book.artifact_id)["bookmarks_applied"]
