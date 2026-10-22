"""服务之间共享的数据契约，保持可序列化且无外部副作用。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from urllib.parse import urlencode


def path_key(path: tuple[str, ...]):
    return tuple(part.casefold() for part in path), path


@dataclass(frozen=True)
class Credentials:
    access_token: str = field(repr=False)
    mac_key: str = field(repr=False)
    diff: int = 0


@dataclass
class Book:
    resource_id: str
    title: str
    catalog_id: str = ""
    resource_type: str = "assets_document"
    category_paths: tuple[tuple[str, ...], ...] = ()
    path_tags: tuple[dict[str, str], ...] = ()

    @property
    def primary_category_path(self) -> tuple[str, ...]:
        return min(self.category_paths, key=path_key) if self.category_paths else ()

    @property
    def page_url(self) -> str:
        return "https://basic.smartedu.cn/tchMaterial/detail?" + urlencode({
            "contentType": self.resource_type,
            "contentId": self.resource_id,
            "catalogType": "tchMaterial",
            "subCatalog": "tchMaterial",
        })

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Book:
        return cls(
            resource_id=data["resource_id"], title=data["title"],
            catalog_id=data.get("catalog_id", ""),
            resource_type=data.get("resource_type", "assets_document"),
            category_paths=tuple(tuple(p) for p in data.get("category_paths", [])),
            path_tags=tuple(dict(p) for p in data.get("path_tags", [])),
        )

    def to_search_dict(self, matched_indices: list[int] | None = None) -> dict:
        indices = list(range(len(self.category_paths))) if matched_indices is None else matched_indices
        indices.sort(key=lambda i: path_key(self.category_paths[i]))
        chosen = indices[0] if indices else None
        tags = self.path_tags[chosen] if chosen is not None and chosen < len(self.path_tags) else {}
        return {
            "resource_id": self.resource_id, "title": self.title,
            **{name: tags.get(name) for name in ("stage", "subject", "edition", "grade")},
            "category_path": list(self.category_paths[chosen]) if chosen is not None else [],
            "category_paths": [list(p) for p in self.category_paths],
            "matched_category_paths": [list(self.category_paths[i]) for i in indices],
            "primary_category_path": list(self.primary_category_path), "page_url": self.page_url,
        }


@dataclass
class CatalogSnapshot:
    books: list[Book]
    categories: dict = field(default_factory=dict)
    module_version: str = ""
    partition_urls: tuple[str, ...] = ()
    category_digest: str = ""
    fetched_at: str = ""
    checked_at: str = ""
    source: str = ""
    warnings: list[str] = field(default_factory=list)
    stale: bool = False
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> CatalogSnapshot:
        if data.get("schema_version") != 1:
            raise ValueError("目录缓存版本不兼容")
        return cls(
            books=[Book.from_dict(b) for b in data["books"]], categories=data.get("categories", {}),
            module_version=str(data.get("module_version", "")),
            partition_urls=tuple(data.get("partition_urls", [])), category_digest=data.get("category_digest", ""),
            fetched_at=data["fetched_at"], checked_at=data["checked_at"], source=data.get("source", ""),
            warnings=list(data.get("warnings", [])), stale=bool(data.get("stale", False)),
        )


@dataclass
class Artifact:
    resource_id: str
    artifact_id: str
    title: str
    url: str = field(repr=False)
    file_format: str = "pdf"
    candidate_urls: tuple[str, ...] = field(default=(), repr=False)
    expected_size: int | None = None
    expected_md5: str | None = None
    upstream_revision: str | None = None
    chapters: list[dict] = field(default_factory=list)
    relative_dir: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProgressEvent:
    task_id: str
    resource_id: str
    stage: str
    attempt: int = 1
    downloaded: int = 0
    total: int | None = None
    speed: float = 0.0
    message: str = ""


@dataclass
class DownloadResult:
    resource_id: str
    artifact_id: str | None
    status: str
    path: str | None = None
    bytes: int = 0
    bookmarks: str = "disabled"
    bookmarks_applied: bool = False
    error: dict | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchResult:
    requested: int
    results: list[DownloadResult]
    artifacts: int = 0
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.cancelled or any(r.status == "cancelled" for r in self.results):
            return 130
        failed = [r for r in self.results if r.status == "failed"]
        if not failed:
            return 0
        if any(r.status in ("completed", "skipped") for r in self.results):
            return 4
        if all((r.error or {}).get("code") in ("AUTH_REQUIRED", "AUTH_REJECTED") for r in failed):
            return 3
        return 1

    def to_dict(self) -> dict:
        return {
            "summary": {
                "requested": self.requested, "artifacts": self.artifacts,
                **{s: sum(r.status == s for r in self.results)
                   for s in ("completed", "skipped", "failed", "cancelled")},
            },
            "results": [r.to_dict() for r in self.results],
        }
