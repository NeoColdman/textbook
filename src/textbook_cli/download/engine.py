"""Bounded concurrent downloads with coordinator-only durable commits.

Transfer, cancellation and naming ideas are adapted from tchMaterial-parser
(MIT), copyright (c) 2026 肥宅水水呀. The original license is retained in
THIRD_PARTY_NOTICES.md and licenses/tchMaterial-parser-MIT.txt.
"""

from __future__ import annotations

import os
import queue
import signal
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock, Timeout
from requests import RequestException

from textbook_cli.core.errors import AppError, Cancelled, redact
from textbook_cli.core.models import BatchResult, DownloadResult, ProgressEvent
from textbook_cli.download.bookmarks import add_bookmarks
from textbook_cli.download.integrity import FileDigest, verify_file
from textbook_cli.download.paths import artifact_path, publish_file, safe_path


@dataclass
class _Task:
    artifact: object
    path: Path
    task_id: str


@dataclass
class _Transferred:
    task: _Task
    temporary: Path
    source: FileDigest
    final: FileDigest
    bookmarks: str
    applied: bool
    warnings: list[str]


class DownloadService:
    def __init__(self, provider=None, credentials=None, event_callback=None, *, client_factory=None, manifest_factory=None):
        if provider is None:
            from textbook_cli.providers.smartedu import SmartEduProvider

            provider = SmartEduProvider()
        self.provider = provider
        self.credentials = credentials
        self.event_callback = event_callback
        self.client_factory = client_factory
        self.manifest_factory = manifest_factory
        self._events = queue.Queue()
        self._rate_lock = threading.Lock()
        self._last_request = 0.0
        self._minimum_interval = 0.2
        self._cancel = threading.Event()
        self._callback_warning = False

    def _emit(self, task_id, resource_id, stage, *, attempt=1, downloaded=0, total=None, speed=0.0, message=""):
        self._events.put(ProgressEvent(task_id, resource_id, stage, attempt, downloaded, total, speed, message))

    def _drain_events(self):
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            if self.event_callback is not None:
                try:
                    self.event_callback(event)
                except KeyboardInterrupt:
                    self._cancel.set()
                except Exception:
                    self._callback_warning = True

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    @contextmanager
    def _interrupts(self):
        # SIGINT marks cancellation, so it cannot interrupt file/SQLite commit.
        installed = threading.current_thread() is threading.main_thread()
        previous = None
        if installed:
            previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda *_: self._cancel.set())
        try:
            yield
        finally:
            if installed:
                signal.signal(signal.SIGINT, previous)

    def run(self, resource_ids, output: Path, bookmarks=False, jobs=2, overwrite=False, cancel=None, books=None):
        if not 1 <= jobs <= 3:
            raise AppError("INVALID_INPUT", "--jobs 必须在 1 到 3 之间", exit_code=2)
        resource_ids = list(dict.fromkeys(resource_ids))
        if not resource_ids:
            raise AppError("INVALID_INPUT", "请至少指定一本教材", exit_code=2)
        self._cancel = cancel if cancel is not None else threading.Event()
        self._callback_warning = False
        self._last_request = 0.0
        self._events = queue.Queue()
        root = Path(output).expanduser().resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
            metadata = safe_path(root, ".textbook")
            metadata.mkdir(exist_ok=True)
            safe_path(root, ".textbook/write.lock")
            safe_path(root, ".textbook/manifest.sqlite3")
        except (OSError, RuntimeError) as exc:
            raise AppError("FILESYSTEM_ERROR", "无法创建输出目录") from exc
        lock = FileLock(str(metadata / "write.lock"), timeout=0)
        try:
            with self._interrupts(), lock:
                return self._run_locked(resource_ids, root, bookmarks, jobs, overwrite, books or {})
        except Timeout as exc:
            raise AppError("OUTPUT_BUSY", "已有下载批次正在使用此输出目录") from exc
        except OSError as exc:
            raise AppError("FILESYSTEM_ERROR", redact(f"无法使用输出目录：{exc}", self.credentials)) from exc

    def _run_locked(self, resource_ids, root, bookmarks, jobs, overwrite, books):
        from textbook_cli.storage.manifest import Manifest

        manifest = (self.manifest_factory or Manifest)(root)
        results, tasks, artifacts = [], [], []
        batch_warnings = []
        try:
            # 分配新路径前先登记清单中已占用的文件：同标题、同目录且资源 ID
            # 前缀相同的两本教材分批下载时，不能把对方文件当作自己的目标。
            claims: dict[str, tuple] = {}
            for existing in manifest.records():
                relative = existing.get("relative_path")
                if not isinstance(relative, str):
                    continue
                try:
                    claims[str(safe_path(root, relative)).casefold()] = (
                        existing.get("resource_id"), existing.get("artifact_id"),
                    )
                except (AppError, OSError, RuntimeError, ValueError):
                    continue
            if any(root.rglob("*.part")):
                batch_warnings.append("输出目录存在旧 .part 临时文件；未将其视为完成文件，也未自动删除或续传")
            for resource_id in resource_ids:
                task_id = resource_id
                self._emit(task_id, resource_id, "queued")
                self._emit(task_id, resource_id, "resolving")
                self._drain_events()
                if self._cancel.is_set():
                    results.append(self._terminal(resource_id, None, "cancelled", task_id=task_id))
                    continue
                try:
                    resolved = self.provider.resolve(
                        resource_id, bookmarks=bookmarks, credentials=self.credentials,
                        book=books.get(resource_id), cancel=self._cancel,
                    )
                    resolved = [item for item in resolved if item.file_format.lower() == "pdf"]
                    if not resolved:
                        raise AppError("UNSUPPORTED_RESOURCE", "该资源没有可下载的 PDF")
                    seen = {(item.resource_id, item.artifact_id) for item in artifacts}
                    unique = []
                    for item in resolved:
                        identity = (item.resource_id, item.artifact_id)
                        if identity not in seen:
                            seen.add(identity)
                            unique.append(item)
                    for index, artifact in enumerate(unique):
                        if artifact.file_format.lower() != "pdf":
                            continue
                        artifacts.append(artifact)
                        aid = task_id if index == 0 else f"{resource_id}:{artifact.artifact_id}"
                        if index:
                            self._emit(aid, resource_id, "queued")
                            self._emit(aid, resource_id, "resolving")
                        tasks.append((artifact, aid))
                except KeyboardInterrupt:
                    self._cancel.set()
                    results.append(self._terminal(resource_id, None, "cancelled", task_id=task_id))
                except Cancelled:
                    self._cancel.set()
                    results.append(self._terminal(resource_id, None, "cancelled", task_id=task_id))
                except Exception as exc:
                    results.append(self._failure(resource_id, None, exc, task_id=task_id))

            planned = []
            counts = Counter(item.resource_id for item in artifacts)
            reserved = set()
            for artifact, task_id in tasks:
                path = None
                try:
                    self._check_cancel()
                    record = manifest.get(artifact.resource_id, artifact.artifact_id)
                    if record is not None and (
                        not isinstance(record, dict)
                        or record.get("resource_id") != artifact.resource_id
                        or record.get("artifact_id") != artifact.artifact_id
                        or not isinstance(record.get("relative_path"), str)
                    ):
                        raise AppError("FILESYSTEM_ERROR", "下载清单记录无效，未将已有文件认作完成")
                    if record:
                        path = safe_path(root, record["relative_path"])
                        owner = claims.get(str(path).casefold())
                        if owner is not None and owner != (artifact.resource_id, artifact.artifact_id):
                            raise AppError("FILE_CONFLICT", "清单中不同教材指向同一文件")
                    else:
                        length = 8
                        path = artifact_path(root, artifact, multiple=counts[artifact.resource_id] > 1)
                        while str(path).casefold() in reserved or str(path).casefold() in claims:
                            # 与本批其他任务或清单中其他资源的文件冲突时延长资源 ID
                            # 后缀，而不是报冲突或在 --overwrite 时覆盖对方文件。
                            length += 4
                            if length > 64:
                                raise AppError("FILE_CONFLICT", "无法为教材分配唯一文件名")
                            path = artifact_path(root, artifact, multiple=counts[artifact.resource_id] > 1, suffix_length=length)
                    if str(path).casefold() in reserved:
                        raise AppError("FILE_CONFLICT", "清单中不同教材指向同一文件")
                    reserved.add(str(path).casefold())
                    if path.exists() and not overwrite:
                        warnings = self._validate_existing(path, artifact, record, bookmarks)
                        result = DownloadResult(
                            artifact.resource_id, artifact.artifact_id, "skipped", path=str(path),
                            bytes=path.stat().st_size, bookmarks=self._bookmark_status(bookmarks, record),
                            bookmarks_applied=bool(record.get("bookmarks_applied")), warnings=warnings,
                        )
                        results.append(result)
                        self._emit(task_id, artifact.resource_id, "skipped", downloaded=result.bytes)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        safe_path(root, path.relative_to(root))
                        planned.append(_Task(artifact, path, task_id))
                except Cancelled:
                    results.append(self._terminal(artifact.resource_id, artifact.artifact_id, "cancelled", path, task_id))
                except Exception as exc:
                    results.append(self._failure(artifact.resource_id, artifact.artifact_id, exc, path, task_id))

            with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="textbook-download") as pool:
                futures = {pool.submit(self._perform, task, bookmarks): task for task in planned}
                pending = set(futures)
                while pending:
                    try:
                        done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                    except KeyboardInterrupt:
                        self._cancel.set()
                        continue
                    self._drain_events()
                    for future in done:
                        task = futures[future]
                        outcome = future.result()
                        if isinstance(outcome, DownloadResult):
                            results.append(outcome)
                            continue
                        if self._cancel.is_set():
                            self._cleanup(outcome.temporary, outcome.warnings)
                            results.append(self._terminal(task.artifact.resource_id, task.artifact.artifact_id, "cancelled", task.path, task.task_id, warnings=outcome.warnings))
                        else:
                            results.append(self._commit(outcome, root, manifest, bookmarks, overwrite))
                self._drain_events()
            order = {rid: index for index, rid in enumerate(resource_ids)}
            results.sort(key=lambda result: (order.get(result.resource_id, len(order)), result.artifact_id or ""))
            if self._callback_warning:
                batch_warnings.append("部分进度事件未能显示，下载结果仍已记录")
            return BatchResult(requested=len(resource_ids), results=results, artifacts=len(artifacts), cancelled=self._cancel.is_set(), warnings=batch_warnings)
        finally:
            self._drain_events()
            manifest.close()

    @staticmethod
    def _bookmark_status(requested, record):
        if not requested:
            return "disabled"
        if record.get("bookmarks_applied"):
            return "added"
        return "failed" if any("书签处理失败" in str(item) for item in record.get("warnings") or []) else "unavailable"

    def _validate_existing(self, path, artifact, record, bookmarks):
        if not record or not path.is_file():
            raise AppError("FILE_CONFLICT", "目标已有文件但没有可信清单；请使用 --overwrite 授权替换")
        if bool(record.get("bookmarks_requested")) != bool(bookmarks):
            raise AppError("FILE_CONFLICT", "书签选项与已有文件不一致；请使用 --overwrite")
        try:
            current = verify_file(path, cancel=self._cancel)
        except Cancelled:
            raise
        except AppError as exc:
            raise AppError("FILE_CONFLICT", "已有文件未通过 PDF 检查；请使用 --overwrite 授权替换") from exc
        if current.sha256 != record.get("final_sha256") or current.size != record.get("final_size"):
            raise AppError("FILE_CONFLICT", "本地文件与清单不一致；请使用 --overwrite 授权替换")
        revision = artifact.upstream_revision
        old_revision = record.get("upstream_revision")
        if revision is not None and old_revision is not None and revision != old_revision:
            raise AppError("FILE_CONFLICT", "平台教材版本已变化；请使用 --overwrite")
        if artifact.expected_size is not None and artifact.expected_size != record.get("source_size"):
            raise AppError("FILE_CONFLICT", "平台教材大小已变化；请使用 --overwrite")
        if artifact.expected_md5 and artifact.expected_md5.lower() != str(record.get("source_md5", "")).lower():
            raise AppError("FILE_CONFLICT", "平台教材内容已变化；请使用 --overwrite")
        warnings = list(getattr(artifact, "warnings", [])) + list(record.get("warnings") or [])
        confirmed = bool(artifact.expected_md5) or (revision is not None and revision == old_revision)
        if not confirmed:
            warnings.append("本地已有且校验通过，平台内容版本未确认")
        return list(dict.fromkeys(warnings))

    def _pace(self):
        with self._rate_lock:
            delay = self._minimum_interval - (time.monotonic() - self._last_request)
            if delay > 0 and self._cancel.wait(delay):
                raise Cancelled()
            self._check_cancel()
            self._last_request = time.monotonic()

    def _new_client(self):
        from textbook_cli.providers.smartedu.http import SmartEduClient

        factory = self.client_factory or SmartEduClient
        return factory(credentials=self.credentials, cancel_event=self._cancel)

    def _perform(self, task, bookmarks):
        artifact = task.artifact
        warnings = list(getattr(artifact, "warnings", []))
        if artifact.expected_size is None:
            warnings.append(
                "平台未提供文件大小；已核对平台 MD5 与 PDF 头，并记录本地 SHA-256"
                if artifact.expected_md5 is not None
                else "平台未提供大小和标准摘要；仅做轻量 PDF 检查并记录本地 SHA-256，未验证缺失指标"
            )
        elif artifact.expected_md5 is None and not any("MD5" in str(item) for item in warnings):
            warnings.append("平台未提供标准 MD5；仅核对平台大小与 PDF 头，本地 SHA-256 已记录")
        client, temporary = None, None
        try:
            self._check_cancel()
            client = self._new_client()
            temporary, source = self._transfer(client, task, warnings)
            final, applied, bookmark_status = source, False, "disabled"
            if bookmarks:
                bookmark_status = "unavailable"
                if artifact.chapters:
                    self._emit(task.task_id, artifact.resource_id, "bookmarking", downloaded=source.size)
                    descriptor, name = tempfile.mkstemp(prefix=f".{task.path.name}.", suffix=".part", dir=task.path.parent)
                    os.close(descriptor)
                    modified = Path(name)
                    try:
                        count = add_bookmarks(temporary, modified, artifact.chapters)
                        digest = verify_file(modified, cancel=self._cancel)
                    except Cancelled:
                        self._cleanup(modified, warnings)
                        raise
                    except Exception:
                        self._cleanup(modified, warnings)
                        warnings.append("书签处理失败，已保留通过校验的原始 PDF")
                        bookmark_status = "failed"
                    else:
                        if count:
                            self._cleanup(temporary, warnings)
                            temporary, final, applied, bookmark_status = modified, digest, True, "added"
                        else:
                            # 没有任何章节取得有效页码：不发布与原文件相同的改写副本。
                            self._cleanup(modified, warnings)
                            warnings.append("书签处理失败：没有章节写入有效书签，已保留通过校验的原始 PDF")
                            bookmark_status = "failed"
                else:
                    warnings.append("平台未提供可用章节，已保留原始 PDF")
            self._check_cancel()
            return _Transferred(task, temporary, source, final, bookmark_status, applied, warnings)
        except Cancelled:
            if temporary is not None:
                self._cleanup(temporary, warnings)
            return self._terminal(artifact.resource_id, artifact.artifact_id, "cancelled", task.path, task.task_id, warnings=warnings)
        except Exception as exc:
            if temporary is not None:
                self._cleanup(temporary, warnings)
            return self._failure(artifact.resource_id, artifact.artifact_id, exc, task.path, task.task_id, warnings=warnings)
        finally:
            if client is not None and hasattr(client, "close"):
                try:
                    client.close()
                except Exception:
                    warnings.append("下载连接清理失败")

    def _transfer(self, client, task, warnings):
        from textbook_cli.providers.smartedu.http import PRIVATE_HOSTS, mirror_urls

        artifact, candidate_index = task.artifact, 0
        candidates = tuple(dict.fromkeys((artifact.url, *artifact.candidate_urls, *mirror_urls(artifact.url))))
        for attempt in range(1, 4):
            self._check_cancel()
            response, temporary, delay, retry = None, None, float((1, 3, 3)[attempt - 1]), False
            complete = False
            self._emit(task.task_id, artifact.resource_id, "connecting", attempt=attempt)
            try:
                self._pace()
                response = client.open_stream_once(candidates[candidate_index], credentials=self.credentials, cancel=self._cancel)
                self._check_cancel()
                status = response.status_code
                if status != 200:
                    error = client.classify_response(response)
                    should_retry = status in {429, 502, 503, 504} or (status == 400 and urlsplit(candidates[candidate_index]).hostname in PRIVATE_HOSTS)
                    if should_retry:
                        delay = client.retry_delay(response, attempt)
                        retry = True
                    if status in {502, 503, 504}:
                        candidate_index = min(candidate_index + 1, len(candidates) - 1)
                    raise error
                raw_length = response.headers.get("Content-Length")
                try:
                    content_length = int(raw_length) if raw_length is not None else None
                    if content_length is not None and content_length < 0:
                        content_length = None
                except (ValueError, TypeError):
                    content_length = None
                if response.headers.get("Content-Encoding"):
                    content_length = None
                total = artifact.expected_size if artifact.expected_size is not None else content_length
                descriptor, name = tempfile.mkstemp(prefix=f".{task.path.name}.", suffix=".part", dir=task.path.parent)
                temporary = Path(name)
                size, started = 0, time.monotonic()
                self._emit(task.task_id, artifact.resource_id, "downloading", attempt=attempt, total=total)
                with os.fdopen(descriptor, "wb") as target:
                    for chunk in response.iter_content(chunk_size=128 * 1024):
                        self._check_cancel()
                        if not chunk:
                            continue
                        target.write(chunk)
                        size += len(chunk)
                        self._emit(task.task_id, artifact.resource_id, "downloading", attempt=attempt, downloaded=size, total=total, speed=size / max(0.001, time.monotonic() - started))
                    self._check_cancel()
                    target.flush()
                    os.fsync(target.fileno())
                self._emit(task.task_id, artifact.resource_id, "verifying", attempt=attempt, downloaded=size, total=total)
                digest = verify_file(temporary, expected_size=artifact.expected_size, expected_md5=artifact.expected_md5, content_length=content_length, cancel=self._cancel)
                complete = True
                return temporary, digest
            except Cancelled:
                raise
            except (RequestException, AppError) as exc:
                if isinstance(exc, RequestException) or (
                    getattr(exc, "code", "") == "NETWORK_ERROR"
                    and (response is None or response.status_code == 200)
                ):
                    retry = True
                    candidate_index = min(candidate_index + 1, len(candidates) - 1)
                if not retry or attempt == 3:
                    if isinstance(exc, AppError):
                        raise
                    raise AppError("NETWORK_ERROR", redact(f"下载连接中断：{exc}", self.credentials)) from exc
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        warnings.append("下载响应清理失败")
                if temporary is not None and not complete:
                    self._cleanup(temporary, warnings)
            if self._cancel.wait(delay):
                raise Cancelled()
        raise AppError("NETWORK_ERROR", "下载重试次数已用完")

    def _commit(self, outcome, root, manifest, bookmarks, overwrite):
        task, artifact = outcome.task, outcome.task.artifact
        self._emit(task.task_id, artifact.resource_id, "committing", downloaded=outcome.final.size)
        self._drain_events()
        try:
            safe_path(root, task.path.relative_to(root))
            publish_file(outcome.temporary, task.path, overwrite=overwrite)
            record = {
                "resource_id": artifact.resource_id, "artifact_id": artifact.artifact_id,
                "upstream_revision": artifact.upstream_revision, "expected_md5": artifact.expected_md5,
                "expected_size": artifact.expected_size, "relative_path": task.path.relative_to(root).as_posix(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "source_md5": outcome.source.md5, "source_sha256": outcome.source.sha256, "source_size": outcome.source.size,
                "final_sha256": outcome.final.sha256, "final_size": outcome.final.size,
                "bookmarks_requested": bool(bookmarks), "bookmarks_applied": outcome.applied,
                "warnings": outcome.warnings,
            }
            try:
                manifest.put(record)
            except Exception as exc:
                raise AppError("FILESYSTEM_ERROR", "文件已保存，清单登记失败；重跑时请核对文件并显式选择覆盖") from exc
            result = DownloadResult(artifact.resource_id, artifact.artifact_id, "completed", path=str(task.path), bytes=outcome.final.size, bookmarks=outcome.bookmarks, bookmarks_applied=outcome.applied, warnings=outcome.warnings)
            self._emit(task.task_id, artifact.resource_id, "completed", downloaded=outcome.final.size)
            return result
        except Exception as exc:
            self._cleanup(outcome.temporary, outcome.warnings)
            return self._failure(artifact.resource_id, artifact.artifact_id, exc, task.path, task.task_id, warnings=outcome.warnings, committing=True)

    @staticmethod
    def _cleanup(path, warnings):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            warnings.append("无法清理本任务的临时文件，请检查输出目录中的 .part 文件")

    def _terminal(self, resource_id, artifact_id, status, path=None, task_id=None, warnings=None):
        self._emit(task_id or resource_id, resource_id, status)
        return DownloadResult(resource_id, artifact_id, status, path=str(path) if path is not None else None, warnings=warnings if warnings is not None else [])

    def _failure(self, resource_id, artifact_id, exc, path=None, task_id=None, warnings=None, committing=False):
        if isinstance(exc, Cancelled) or (self._cancel.is_set() and not committing):
            return self._terminal(resource_id, artifact_id, "cancelled", path, task_id, warnings=warnings)
        code = exc.code if isinstance(exc, AppError) else "FILESYSTEM_ERROR" if isinstance(exc, OSError) else "DOWNLOAD_FAILED"
        message = redact(str(exc), self.credentials)
        self._emit(task_id or resource_id, resource_id, "failed", message=message)
        return DownloadResult(resource_id, artifact_id, "failed", path=str(path) if path is not None else None, error={"code": code, "message": message}, warnings=warnings if warnings is not None else [])
