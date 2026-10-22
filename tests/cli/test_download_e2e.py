"""Actual command → downloader → local HTTP → SQLite/file round trip."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

from textbook_cli.cli import commands
from textbook_cli.core.models import Artifact
from textbook_cli.download.engine import DownloadService
from textbook_cli.providers.smartedu.http import classify_response
from textbook_cli.storage.manifest import Manifest
from textbook_cli.storage.paths import AppPaths

pytestmark = [pytest.mark.cli, pytest.mark.local_http]


def test_command_real_transfer_then_verified_skip(tmp_path, monkeypatch):
    body = b"%PDF-1.7\n" + b"locally generated fixture\n" * 100
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Provider:
        def resolve(self, resource_id, **kwargs):
            return [Artifact(resource_id, "pdf", "中文教材", "https://r1-ndr-private.ykt.cbern.com.cn/book.pdf", expected_size=len(body), expected_md5=hashlib.md5(body).hexdigest(), relative_dir=("初中", "数学"))]

    class LocalClient:
        def __init__(self, **kwargs):
            self.session = requests.Session()
            self.session.trust_env = False

        def open_stream_once(self, url, **kwargs):
            return self.session.get(f"http://127.0.0.1:{server.server_port}/book.pdf", stream=True, timeout=(1, 2))

        def classify_response(self, response):
            return classify_response(response)

        def retry_delay(self, response, attempt):
            return 0

        def close(self):
            self.session.close()

    class Catalog:
        snapshot = None

        def cached(self):
            return None

    class Store:
        def load(self):
            return None

    def factory(**kwargs):
        service = DownloadService(**kwargs, client_factory=LocalClient)
        service._minimum_interval = 0
        return service

    runtime = commands.Runtime(Provider(), Catalog(), Store(), AppPaths(tmp_path / "config", tmp_path / "cache"), factory)
    monkeypatch.setattr(commands, "runtime_factory", lambda: runtime)
    output = tmp_path / "含 空格输出"
    arguments = ["download", "--id", "book-a", "-o", str(output), "--json"]
    runner = CliRunner()
    try:
        first = runner.invoke(commands.app, arguments)
        assert first.exit_code == 0, first.output
        payload = json.loads(first.stdout)
        record = payload["data"]["results"][0]
        assert record["status"] == "completed"
        assert Path(record["path"]).read_bytes() == body
        with Manifest(output) as manifest:
            saved = manifest.get("book-a", "pdf")
        assert saved["final_sha256"] == hashlib.sha256(body).hexdigest()
        second = runner.invoke(commands.app, arguments)
        assert second.exit_code == 0, second.output
        assert json.loads(second.stdout)["data"]["results"][0]["status"] == "skipped"
        assert received == ["/book.pdf"]
        assert not list(output.rglob("*.part"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
