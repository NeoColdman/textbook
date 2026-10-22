"""Atomic, non-secret catalog snapshot persistence."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from textbook_cli.core.errors import AppError
from textbook_cli.core.models import CatalogSnapshot

MAX_CACHE_BYTES = 128 * 1024 * 1024


class CatalogCache:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.last_error: str | None = None

    def load(self) -> CatalogSnapshot | None:
        self.last_error = None
        try:
            if self.path.stat().st_size > MAX_CACHE_BYTES:
                raise ValueError("oversized cache")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                raise ValueError("incompatible cache")
            snapshot = CatalogSnapshot.from_dict(data)
            if not snapshot.checked_at or not snapshot.fetched_at or not isinstance(snapshot.books, list):
                raise ValueError("incomplete cache")
            for timestamp in (snapshot.fetched_at, snapshot.checked_at):
                if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError("missing timezone")
            if not isinstance(snapshot.categories, dict) or not all(isinstance(w, str) for w in snapshot.warnings):
                raise ValueError("invalid snapshot fields")
            for book in snapshot.books:
                if (
                    not isinstance(book.resource_id, str) or not book.resource_id
                    or not isinstance(book.title, str) or not book.title
                    or len(book.category_paths) != len(book.path_tags)
                    or any(not all(isinstance(name, str) for name in path) for path in book.category_paths)
                    or any(not all(isinstance(key, str) and isinstance(value, str) for key, value in tags.items())
                           for tags in book.path_tags)
                ):
                    raise ValueError("incomplete book")
            return snapshot
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.last_error = "本地目录缓存不可读、损坏或版本不兼容，需要重新获取。"
            return None

    def save(self, snapshot: CatalogSnapshot) -> None:
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix=".catalog-", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(snapshot.to_dict(), stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, TypeError, ValueError):
            raise AppError("FILESYSTEM_ERROR", "无法保存目录缓存，请检查缓存目录权限和可用空间。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # Preserve the structured save error if cleanup is denied too.
