"""Cross-platform names and exclusive publication.

Filename handling is adapted from tchMaterial-parser (MIT), copyright (c)
2026 肥宅水水呀. See THIRD_PARTY_NOTICES.md and licenses/tchMaterial-parser-MIT.txt.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

from textbook_cli.core.errors import AppError

_REPLACEMENTS = str.maketrans('"*/:<>?\\|', '＂＊／：＜＞？＼｜')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
_PATH_LIMIT = 240 if os.name == "nt" else (1023 if sys.platform == "darwin" else 4095)


def sanitize_filename(value: str, max_bytes: int = 120) -> str:
    """Keep readable Unicode names while respecting common filesystem limits."""
    value = re.sub(r"[\x00-\x1f\x7f]", "", str(value)).translate(_REPLACEMENTS)
    value = value.strip().rstrip(". ") or "untitled"
    if value in {".", ".."} or _RESERVED.match(value):
        value = "_" + value
    while len(value.encode("utf-8")) > max_bytes:
        value = value[:-1]
    return value.rstrip(". ") or "untitled"


def safe_path(root: Path, relative: str | Path) -> Path:
    """Reject traversal and existing symlinks escaping the selected output root."""
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise AppError("FILESYSTEM_ERROR", "下载路径不能越出输出目录")
    target = root / relative
    try:
        target.resolve().relative_to(root)
    except (ValueError, OSError, RuntimeError) as exc:
        raise AppError("FILESYSTEM_ERROR", "下载路径不能越出输出目录") from exc
    length = len(str(target).encode("utf-16-le")) // 2 if os.name == "nt" else len(os.fsencode(target))
    if length > _PATH_LIMIT:
        raise AppError("FILESYSTEM_ERROR", "输出路径过长，请选择更短的输出根目录；本平台不支持此完整目录长度")
    return target


def _identity_suffix(identity: str, length: int = 8) -> str:
    plain = re.sub(r"[^A-Za-z0-9]", "", identity)
    return (plain if len(plain) >= length else hashlib.sha256(identity.encode()).hexdigest())[:length]


def artifact_path(root: Path, artifact, *, multiple: bool = False, suffix_length: int = 8) -> Path:
    directories = [sanitize_filename(part) for part in artifact.relative_dir if part]
    suffix = _identity_suffix(artifact.resource_id, suffix_length)
    if multiple:
        suffix += "-" + _identity_suffix(artifact.artifact_id)
    filename = f"{sanitize_filename(artifact.title)}--{suffix}.pdf"
    return safe_path(root, Path(*directories, filename))


def publish_file(source: Path, destination: Path, *, overwrite: bool) -> None:
    """Publish without a check/replace race; overwrite requires explicit consent."""
    try:
        if overwrite:
            os.replace(source, destination)
        else:
            os.link(source, destination)
            source.unlink()
    except FileExistsError as exc:
        raise AppError("FILE_CONFLICT", "目标文件已存在；请更换目录或显式使用 --overwrite") from exc
    except OSError as exc:
        raise AppError("FILESYSTEM_ERROR", "无法安全发布下载文件；请使用支持原子发布的本地文件系统") from exc
