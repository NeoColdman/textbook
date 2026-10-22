import gzip
import hashlib
import multiprocessing
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from filelock import FileLock

from textbook_cli.core.errors import AppError
from textbook_cli.core.models import Artifact
from textbook_cli.download.engine import DownloadService
from textbook_cli.providers.smartedu.http import classify_response, retry_delay
from textbook_cli.storage.manifest import Manifest

PDF = b"%PDF-1.7\n" + b"download integration content\n" * 24000
pytestmark = pytest.mark.local_http


class Provider:
    def __init__(self, route="ok", count=1):
        self.route, self.count = route, count

    def resolve(self, resource_id, **kwargs):
        return [Artifact(resource_id, "file", "中文路径 教材", f"https://r1-ndr-private.ykt.cbern.com.cn/{self.route}", expected_md5=hashlib.md5(PDF).hexdigest(), relative_dir=("小学", "语文"))]


@pytest.fixture
def server():
    state = {"active": 0, "peak": 0, "requests": 0, "headers": [], "lock": threading.Lock()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with state["lock"]:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state["requests"] += 1
                request_number = state["requests"]
                state["headers"].append(dict(self.headers))
            try:
                route = self.path.strip("/")
                body = gzip.compress(PDF) if route == "gzip" else PDF
                self.send_response(200)
                if route != "unknown":
                    self.send_header("Content-Length", str(len(body)))
                if route == "gzip":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                if route == "retry" and request_number == 1:
                    self.wfile.write(body[:200])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                chunk_size = 64 * 1024
                for offset in range(0, len(body), chunk_size):
                    self.wfile.write(body[offset:offset + chunk_size])
                    self.wfile.flush()
                    if route == "slow":
                        time.sleep(0.025)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with state["lock"]:
                    state["active"] -= 1

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", state
    finally:
        http.shutdown()
        http.server_close()
        worker.join(timeout=3)


def local_factory(base, opened):
    tracker = {"active": 0, "peak": 0, "lock": threading.Lock()}

    def factory(credentials=None, cancel_event=None):
        class LocalClient:
            def __init__(self):
                self.session = requests.Session()
                self.session.trust_env = False
                opened.append(self.session)
                self.active = False

            def open_stream_once(self, url, **kwargs):
                if not self.active:
                    with tracker["lock"]:
                        tracker["active"] += 1
                        tracker["peak"] = max(tracker["peak"], tracker["active"])
                    self.active = True
                return self.session.get(base + urlsplit(url).path, stream=True, timeout=(1, 2))

            def classify_response(self, response):
                return classify_response(response, credentials)

            def retry_delay(self, response, attempt):
                return 0

            def close(self):
                self.session.close()
                if self.active:
                    with tracker["lock"]:
                        tracker["active"] -= 1
                    self.active = False

        return LocalClient()

    factory.tracker = tracker
    return factory


@pytest.mark.parametrize("route", ["ok", "unknown", "gzip", "retry"])
def test_real_stream_with_unknown_compressed_or_interrupted_body(tmp_path, server, route):
    base, state = server
    sessions = []
    downloader = DownloadService(Provider(route), client_factory=local_factory(base, sessions))
    downloader._minimum_interval = 0
    stop = threading.Event()
    stop.wait = lambda _: False
    result = downloader.run(["book"], tmp_path / "含 空格目录", cancel=stop)
    item = result.results[0]
    assert result.exit_code == 0 and Path(item.path).read_bytes() == PDF
    assert state["requests"] == (2 if route == "retry" else 1)
    assert not any("Authorization" in headers for headers in state["headers"])
    assert not list(tmp_path.rglob("*.part"))


def test_real_parallelism_is_bounded_and_sessions_are_independent(tmp_path, server):
    base, state = server
    sessions = []
    factory = local_factory(base, sessions)
    downloader = DownloadService(Provider("slow"), client_factory=factory)
    downloader._minimum_interval = 0
    result = downloader.run([f"book-{index}" for index in range(5)], tmp_path, jobs=2)
    assert result.exit_code == 0 and len(result.results) == 5
    assert factory.tracker["peak"] == 2 and factory.tracker["active"] == 0
    assert len({id(session) for session in sessions}) == 5


def test_real_transfer_cancellation_preserves_complete_files(tmp_path, server):
    base, _ = server
    stop = threading.Event()
    events = []

    def on_event(event):
        events.append(event)
        if event.stage == "downloading" and event.downloaded > 0:
            stop.set()

    downloader = DownloadService(Provider("slow"), event_callback=on_event, client_factory=local_factory(base, []))
    result = downloader.run(["book"], tmp_path, cancel=stop)
    assert result.exit_code == 130 and result.results[0].status == "cancelled"
    assert not list(tmp_path.rglob("*.pdf")) and not list(tmp_path.rglob("*.part"))
    assert any(event.downloaded for event in events)


def _hold_output_lock(root, ready, release):
    metadata = Path(root) / ".textbook"
    metadata.mkdir(parents=True, exist_ok=True)
    with FileLock(str(metadata / "write.lock"), timeout=0):
        ready.set()
        release.wait(10)


def test_two_process_output_lock_and_independent_directories(tmp_path, server):
    base, _ = server
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    first_output = tmp_path / "first"
    process = context.Process(target=_hold_output_lock, args=(str(first_output), ready, release))
    process.start()
    try:
        assert ready.wait(5)
        downloader = DownloadService(Provider(), client_factory=local_factory(base, []))
        with pytest.raises(AppError) as error:
            downloader.run(["book"], first_output)
        assert error.value.code == "OUTPUT_BUSY"
        assert not list(first_output.rglob("*.pdf"))
        assert downloader.run(["book"], tmp_path / "second").exit_code == 0
    finally:
        release.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
    assert process.exitcode == 0


def test_optional_real_pdf_bookmarks_have_separate_hashes(tmp_path):
    import io

    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    body = stream.getvalue()

    class BookmarkedProvider:
        def resolve(self, resource_id, **kwargs):
            return [Artifact(resource_id, "pdf", "with chapters", "https://r1-ndr-private.ykt.cbern.com.cn/book.pdf", expected_md5=hashlib.md5(body).hexdigest(), chapters=[{"title": "第一章", "page_index": 1, "children": [{"title": "第二节", "page_index": 2}]}])]

    from tests.unit.test_download_core import ClientFactory, Response

    downloader = DownloadService(BookmarkedProvider(), client_factory=ClientFactory([Response(body)]))
    result = downloader.run(["book"], tmp_path, bookmarks=True)
    item = result.results[0]
    assert item.status == "completed" and item.bookmarks_applied
    reader = pypdf.PdfReader(item.path)
    assert len(reader.pages) == 2 and reader.outline[0].title == "第一章"
    assert reader.outline[1][0].title == "第二节"
    with Manifest(tmp_path) as manifest:
        record = manifest.get("book", "pdf")
    assert record["source_md5"] == hashlib.md5(body).hexdigest()
    assert record["final_sha256"] == hashlib.sha256(Path(item.path).read_bytes()).hexdigest()
    assert record["source_sha256"] != record["final_sha256"]


def test_retry_after_too_long_is_not_silently_retried(tmp_path):
    from tests.unit.test_download_core import ClientFactory, Response

    factory = ClientFactory([Response(status=429, headers={"Retry-After": "61"})])

    def with_policy(**kwargs):
        client = factory(**kwargs)
        client.retry_delay = retry_delay
        return client

    downloader = DownloadService(Provider(), client_factory=with_policy)
    result = downloader.run(["book"], tmp_path)
    assert result.exit_code == 1 and len(factory.urls) == 1
    assert result.results[0].error["code"] == "RATE_LIMITED"


def _interruptible_download(root, ready, result_pipe):
    from tests.unit.test_download_core import ClientFactory, Response

    def slow_chunks():
        yield PDF[:32]
        ready.set()
        for offset in range(32, len(PDF), 1024):
            time.sleep(0.01)
            yield PDF[offset:offset + 1024]

    factory = ClientFactory([Response(chunks=slow_chunks)])
    result = DownloadService(Provider(), client_factory=factory).run(["book"], Path(root))
    result_pipe.send((result.exit_code, [item.status for item in result.results]))
    result_pipe.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows 控制台中断由平台终端验收覆盖")
def test_real_sigint_returns_cancelled_result_after_cleanup(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_interruptible_download, args=(str(tmp_path), ready, sender))
    process.start()
    sender.close()
    try:
        assert ready.wait(5)
        os.kill(process.pid, signal.SIGINT)
        assert receiver.poll(5)
        assert receiver.recv() == (130, ["cancelled"])
        process.join(timeout=5)
        assert process.exitcode == 0
        assert not list(tmp_path.rglob("*.pdf")) and not list(tmp_path.rglob("*.part"))
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        receiver.close()
