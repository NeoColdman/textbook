"""Streaming hashes and deliberately lightweight PDF checks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from textbook_cli.core.errors import AppError, Cancelled


@dataclass(frozen=True)
class FileDigest:
    size: int
    md5: str
    sha256: str


def file_digest(path: Path, cancel: Event | None = None) -> FileDigest:
    md5, sha256, size = hashlib.md5(usedforsecurity=False), hashlib.sha256(), 0
    with Path(path).open("rb") as source:
        while chunk := source.read(256 * 1024):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            size += len(chunk)
            md5.update(chunk)
            sha256.update(chunk)
    return FileDigest(size, md5.hexdigest(), sha256.hexdigest())


def check_pdf_header(path: Path) -> None:
    with Path(path).open("rb") as source:
        header = source.read(1024)
    lowered = header.lstrip().lower()
    if not header or b"%PDF-" not in header or lowered.startswith((b"<!doctype html", b"<html")):
        raise AppError("INTEGRITY_FAILED", "响应不是可识别的 PDF 文件（空文件、登录页或缺少 PDF 头）")


def verify_file(
    path: Path,
    *,
    expected_size: int | None = None,
    expected_md5: str | None = None,
    content_length: int | None = None,
    cancel: Event | None = None,
) -> FileDigest:
    check_pdf_header(path)
    digest = file_digest(path, cancel)
    if expected_size is not None and digest.size != expected_size:
        raise AppError("INTEGRITY_FAILED", f"文件大小校验失败：预期 {expected_size} 字节，收到 {digest.size} 字节")
    if content_length is not None and digest.size != content_length:
        raise AppError("INTEGRITY_FAILED", f"传输不完整：预期 {content_length} 字节，收到 {digest.size} 字节")
    if expected_md5 is not None and digest.md5.lower() != expected_md5.lower():
        raise AppError("INTEGRITY_FAILED", "平台 MD5 内容校验失败")
    return digest
