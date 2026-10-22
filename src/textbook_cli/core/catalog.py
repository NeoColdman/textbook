"""Cached catalog service and deterministic, path-aware search."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from textbook_cli.core.errors import AppError
from textbook_cli.core.models import CatalogSnapshot
from textbook_cli.providers.smartedu import SmartEduProvider
from textbook_cli.storage.cache import CatalogCache
from textbook_cli.storage.paths import AppPaths

TTL_SECONDS = 24 * 60 * 60


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp is not timezone aware")
    return result


def is_stale(snapshot: CatalogSnapshot) -> bool:
    try:
        age = (datetime.now(timezone.utc) - _timestamp(snapshot.checked_at)).total_seconds()
        return age >= TTL_SECONDS or age < -300
    except (ValueError, TypeError, AttributeError):
        return True


def _path_key(path: tuple[str, ...]):
    return tuple(part.casefold() for part in path), path


def search_books(
    snapshot: CatalogSnapshot,
    query: str,
    *,
    stage: str | None = None,
    subject: str | None = None,
    edition: str | None = None,
    grade: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    if isinstance(limit, bool) or isinstance(offset, bool) or not 1 <= limit <= 1000 or offset < 0:
        raise AppError("INVALID_INPUT", "limit 必须为 1–1000，offset 必须为非负整数。", 2)
    keywords = query.casefold().split()
    filters = {key: value.casefold() for key, value in {
        "stage": stage, "subject": subject, "edition": edition, "grade": grade
    }.items() if value is not None}
    matches = []
    for book in snapshot.books:
        matched_indices = []
        for index, path in enumerate(book.category_paths):
            tags = book.path_tags[index] if index < len(book.path_tags) else {}
            text = " ".join((book.title, *path)).casefold()
            if all(keyword in text for keyword in keywords) and all(
                isinstance(tags.get(dimension), str) and tags[dimension].casefold() == value
                for dimension, value in filters.items()
            ):
                matched_indices.append(index)
        if not book.category_paths and not filters and all(keyword in book.title.casefold() for keyword in keywords):
            matched_indices = [-1]
        if matched_indices:
            matches.append((book, matched_indices))
    matches.sort(key=lambda item: (_path_key(item[0].primary_category_path), item[0].title.casefold(), item[0].title, item[0].resource_id))
    return {
        "catalog": {
            "updated_at": snapshot.fetched_at,
            "fetched_at": snapshot.fetched_at,
            "checked_at": snapshot.checked_at,
            "source": snapshot.source,
            "stale": is_stale(snapshot),
        },
        "total": len(matches),
        "limit": limit,
        "offset": offset,
        "items": [book.to_search_dict([i for i in indices if i >= 0]) for book, indices in matches[offset:offset + limit]],
    }


class CatalogService:
    def __init__(self, provider=None, cache_path: Path | None = None) -> None:
        self.provider = provider or SmartEduProvider()
        self.cache = CatalogCache(cache_path or AppPaths.default().cache_file)
        self.snapshot: CatalogSnapshot | None = None
        self.last_warnings: list[str] = []

    def cached(self) -> CatalogSnapshot | None:
        self.snapshot = self.cache.load()
        if self.snapshot is not None:
            self.snapshot.stale = is_stale(self.snapshot)
        return self.snapshot

    def load(self, refresh: bool = False) -> CatalogSnapshot:
        previous = self.cached()
        self.last_warnings = []
        if self.cache.last_error:
            self.last_warnings.append(self.cache.last_error)
        if not refresh and previous is not None and not previous.stale:
            self.last_warnings.extend(previous.warnings)
            return previous
        try:
            updated = self.provider.fetch_catalog(previous=previous)
            updated.stale = False
            self.cache.save(updated)
            self.snapshot = updated
            self.last_warnings.extend(updated.warnings)
            return updated
        except AppError as error:
            if refresh or previous is None or error.code == "CANCELLED":
                raise
            previous.stale = True
            self.snapshot = previous
            self.last_warnings.extend(previous.warnings)
            self.last_warnings.append("目录刷新失败，正在使用旧缓存；内容可能不是最新。")
            return previous

    def status(self) -> dict:
        snapshot = self.cached()
        return {
            "exists": self.cache.path.exists(),
            "usable": snapshot is not None,
            "path": str(self.cache.path),
            "fetched_at": snapshot.fetched_at if snapshot else None,
            "checked_at": snapshot.checked_at if snapshot else None,
            "source": snapshot.source if snapshot else None,
            "stale": is_stale(snapshot) if snapshot else None,
            "books": len(snapshot.books) if snapshot else 0,
            "warning": self.cache.last_error,
        }

    def search(self, query: str, **filters) -> dict:
        snapshot = self.load()
        for dimension in ("stage", "subject", "edition", "grade"):
            if filters.get(dimension) is not None and not any(
                tags.get(dimension) for book in snapshot.books for tags in book.path_tags
            ):
                names = {"stage": "学段", "subject": "学科", "edition": "版本", "grade": "年级"}
                self.last_warnings.append(f"当前平台目录未提供可确认的{names[dimension]}元数据，该过滤条件没有匹配结果。")
        return search_books(snapshot, query, **filters)
