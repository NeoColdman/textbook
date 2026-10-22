"""Normalize the platform's tagged catalog into independent models.

Catalog endpoint selection and hierarchy traversal adapted from
tchMaterial-parser, Copyright (c) 2026 肥宅水水呀 (MIT).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from textbook_cli.core.errors import AppError
from textbook_cli.core.models import Book, CatalogSnapshot

from .http import SmartEduClient, validate_url

BASE_URL = "https://s-file-1.ykt.cbern.com.cn"
TAGS_URL = f"{BASE_URL}/zxx/ndrs/tags/tch_material_tag.json"
VERSION_URL = f"{BASE_URL}/zxx/ndrs/resources/tch_material/version/data_version.json"
DIMENSIONS = {
    "zxxxd": "stage",
    "zxxxk": "subject",
    "zxxbb": "edition",
    "zxxnj": "grade",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def path_sort_key(path: tuple[str, ...]):
    return tuple(part.casefold() for part in path), path


def _schema(message: str) -> AppError:
    return AppError("PROVIDER_SCHEMA_CHANGED", message)


def _dimension_name(value: object) -> str | None:
    """按维度 ID 查维度名；字段为数组、对象等不可哈希值时抛结构化错误而非 TypeError。

    TypeError 不属于 AppError，会绕过记录隔离与旧缓存回退，因此必须先校验类型。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise _schema("平台标签维度 ID 不是字符串。")
    return DIMENSIONS.get(value)


def parse_categories(data: object) -> tuple[dict, dict[str, dict]]:
    if not isinstance(data, dict) or not isinstance(data.get("hierarchies"), list):
        raise _schema("平台分类缺少 hierarchies 数组。")
    tags: dict[str, dict] = {}

    def parse(groups: list, depth: int = 0) -> dict:
        if depth > 32:
            raise _schema("平台分类层级超过允许深度。")
        result = {}
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("children"), list):
                raise _schema("平台分类分组结构无效。")
            dimension = _dimension_name(group.get("tag_dimension_id"))
            for node in group["children"]:
                if not isinstance(node, dict) or not node.get("tag_id") or not node.get("tag_name"):
                    raise _schema("平台分类节点缺少 ID 或名称。")
                tag_id, name = str(node["tag_id"]), str(node["tag_name"])
                node_dimension = _dimension_name(node.get("tag_dimension_id")) or dimension
                children = node.get("hierarchies") or []
                if not isinstance(children, list):
                    raise _schema("平台分类子层级结构无效。")
                tags[tag_id] = {"name": name, "dimension": node_dimension}
                result[tag_id] = {
                    "id": tag_id,
                    "name": name,
                    "dimension": node_dimension,
                    "children": parse(children, depth + 1),
                }
        return result

    return parse(data["hierarchies"]), tags


def record_paths(record: dict, tag_index: dict[str, dict]) -> tuple[tuple, tuple]:
    """Build aligned paths and tags, never infer a grade from a book title."""
    local_tags = dict(tag_index)
    raw_tags = record.get("tag_list")
    # 先验证字段类型：tag_list 为数字等非数组值时按结构异常处理，
    # 让调用方的记录隔离和旧缓存回退继续生效，而不是抛 TypeError。
    if raw_tags is not None and not isinstance(raw_tags, list):
        raise _schema("教材 tag_list 不是数组。")
    for tag in raw_tags or []:
        if not isinstance(tag, dict):
            continue
        tag_id, name = tag.get("tag_id"), tag.get("tag_name")
        if tag_id and name:
            previous = local_tags.get(str(tag_id), {})
            local_tags[str(tag_id)] = {
                "name": str(name),
                "dimension": _dimension_name(tag.get("tag_dimension_id")) or previous.get("dimension"),
            }
    raw_paths = record.get("tag_paths") or []
    if not isinstance(raw_paths, list):
        raise _schema("教材 tag_paths 不是数组。")
    pairs = {}
    for raw in raw_paths:
        if not isinstance(raw, str):
            raise _schema("教材分类路径不是字符串。")
        names, dimensions = [], {}
        for tag_id in raw.split("/"):
            tag = local_tags.get(tag_id)
            if not tag:
                continue  # The platform's outer container ID need not be in tags.
            names.append(tag["name"])
            if tag.get("dimension"):
                dimensions[tag["dimension"]] = tag["name"]
        if names:
            pairs[tuple(names)] = dimensions
    # Detail JSON can contain dimension tags without a hierarchy path.
    if not pairs and not raw_paths:
        dimensions = {}
        for tag in raw_tags or []:
            if isinstance(tag, dict):
                dimension = _dimension_name(tag.get("tag_dimension_id"))
                if dimension and tag.get("tag_name"):
                    dimensions[dimension] = str(tag["tag_name"])
        if dimensions:
            path = tuple(dimensions[name] for name in ("stage", "subject", "edition", "grade") if name in dimensions)
            pairs[path] = dimensions
    paths = tuple(sorted(pairs, key=path_sort_key))
    return paths, tuple(pairs[path] for path in paths)


def book_from_record(record: dict, tag_index: dict[str, dict] | None = None) -> Book:
    resource_id = record.get("content_id") or record.get("id")
    title = record.get("title") or record.get("name")
    if (
        not isinstance(resource_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", resource_id) is None
        or not isinstance(title, str)
        or not title.strip()
    ):
        raise _schema("教材记录缺少资源 ID 或标题。")
    resource_type = record.get("resource_type_code") or "assets_document"
    if resource_type != "assets_document":
        raise AppError("UNSUPPORTED_RESOURCE", "首版仅支持普通电子教材 PDF。")
    paths, path_tags = record_paths(record, tag_index or {})
    return Book(
        resource_id=resource_id,
        title=title,
        catalog_id=str(record.get("id") or resource_id),
        resource_type=resource_type,
        category_paths=paths,
        path_tags=path_tags,
    )


def _merge_book(first: Book, other: Book) -> Book:
    pairs = {path: dict(tags) for path, tags in zip(first.category_paths, first.path_tags)}
    for path, tags in zip(other.category_paths, other.path_tags):
        pairs.setdefault(path, {}).update(tags)
    paths = tuple(sorted(pairs, key=path_sort_key))
    return Book(
        resource_id=first.resource_id,
        catalog_id=first.catalog_id,
        title=first.title,
        resource_type=first.resource_type,
        category_paths=paths,
        path_tags=tuple(pairs[path] for path in paths),
    )


def fetch_catalog(client: SmartEduClient, previous: CatalogSnapshot | None = None) -> CatalogSnapshot:
    raw_categories = client.get_json(TAGS_URL)
    categories, tag_index = parse_categories(raw_categories)
    category_digest = hashlib.sha256(
        json.dumps(categories, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    index = client.get_json(VERSION_URL)
    if not isinstance(index, dict):
        raise _schema("平台目录版本索引不是对象。")
    urls = index.get("urls")
    if isinstance(urls, str):
        urls = [url.strip() for url in urls.split(",") if url.strip()]
    if not isinstance(urls, list) or not urls or not all(isinstance(url, str) for url in urls):
        raise _schema("平台目录版本索引没有有效的分片地址。")
    partition_urls = tuple(dict.fromkeys(urls))
    for url in partition_urls:
        validate_url(url)
    module_version = str(index.get("module_version") or index.get("version") or index.get("data_version") or "")
    now = utc_now()
    if (
        previous is not None
        and previous.schema_version == 1
        and previous.module_version == module_version
        and set(previous.partition_urls) == set(partition_urls)
        and previous.category_digest == category_digest
    ):
        return CatalogSnapshot(
            books=previous.books,
            categories=previous.categories,
            module_version=module_version,
            partition_urls=partition_urls,
            category_digest=category_digest,
            fetched_at=previous.fetched_at,
            checked_at=now,
            source=VERSION_URL,
            warnings=list(previous.warnings),
        )
    books: dict[str, Book] = {}
    invalid, considered, unsupported = 0, 0, 0
    for url in partition_urls:
        records = client.get_json(url)
        if not isinstance(records, list):
            raise _schema("平台教材分片不是记录数组；旧目录未被替换。")
        for record in records:
            if not isinstance(record, dict):
                considered += 1
                invalid += 1
                continue
            # The official shards intentionally mix ordinary textbooks and
            # thematic courses. Unsupported resource types are not bad schema.
            if (record.get("resource_type_code") or "assets_document") != "assets_document":
                unsupported += 1
                continue
            # Resources without paths in the catalog are not ordinary textbooks.
            if not record.get("tag_paths"):
                continue
            considered += 1
            try:
                book = book_from_record(record, tag_index)
                if not book.category_paths:
                    invalid += 1
                    continue
                books[book.resource_id] = _merge_book(books[book.resource_id], book) if book.resource_id in books else book
            except AppError:
                invalid += 1
    if not books:
        raise _schema("完整目录中没有可识别的普通电子教材，平台结构可能已变化；旧目录未被替换。")
    if invalid > max(10, considered // 100):
        raise _schema(f"目录中有 {invalid} 条普通教材记录结构异常，超过可隔离范围；旧目录未被替换。")
    warnings = [f"已隔离 {invalid} 条结构无效的普通教材记录。"] if invalid else []
    if unsupported:
        warnings.append(f"已排除 {unsupported} 条专题课程等非普通电子教材资源，首版仅支持普通电子教材 PDF。")
    return CatalogSnapshot(
        books=list(books.values()),
        categories=categories,
        module_version=module_version,
        partition_urls=partition_urls,
        category_digest=category_digest,
        fetched_at=now,
        checked_at=now,
        source=VERSION_URL,
        warnings=warnings,
    )
