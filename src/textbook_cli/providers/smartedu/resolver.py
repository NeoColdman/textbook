"""Resolve ordinary textbook pages into PDF artifacts.

Resource and bookmark metadata protocols adapted from tchMaterial-parser,
Copyright (c) 2026 肥宅水水呀, MIT; see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import hashlib
import re
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from textbook_cli.core.errors import AppError, Cancelled
from textbook_cli.core.models import Artifact, Book, Credentials

from .catalog import BASE_URL, book_from_record, fetch_catalog
from .http import SmartEduClient, mirror_urls, validate_url

RESOURCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
DETAIL_URL = f"{BASE_URL}/zxx/ndrv2/resources/tch_material/details/{{resource_id}}.json"


def normalize_input(value: str) -> str:
    value = value.strip()
    if RESOURCE_ID_RE.fullmatch(value):
        return value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise AppError("INVALID_INPUT", "教材链接或资源 ID 格式无效。", 2) from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "basic.smartedu.cn"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path.rstrip("/") != "/tchMaterial/detail"
    ):
        raise AppError("INVALID_INPUT", "只接受国家中小学智慧教育平台的 HTTPS 教材详情链接或规范资源 ID。", 2)
    params = parse_qs(parsed.query, keep_blank_values=True)
    content_type = params.get("contentType", ["assets_document"])
    if content_type != ["assets_document"]:
        raise AppError("UNSUPPORTED_RESOURCE", "首版只支持 assets_document 普通电子教材 PDF。", 2)
    ids = params.get("contentId", [])
    if len(ids) != 1 or not RESOURCE_ID_RE.fullmatch(ids[0]):
        raise AppError("INVALID_INPUT", "教材链接必须有一个有效的 contentId。", 2)
    return ids[0]


def _title(record: dict, fallback: str) -> str:
    value = record.get("global_title")
    if isinstance(value, dict):
        value = value.get("zh-CN") or value.get("en")
    return str(value or record.get("title") or record.get("name") or fallback)


def _item_urls(item: dict) -> tuple[str, ...]:
    values = []
    storage = item.get("ti_storage")
    if isinstance(storage, str) and storage:
        values.append(storage.replace("cs_path:${ref-path}", "https://r1-ndr-private.ykt.cbern.com.cn"))
    storages = item.get("ti_storages") or []
    if not isinstance(storages, list):
        raise AppError("PROVIDER_SCHEMA_CHANGED", "平台文件候选地址不是数组。")
    values.extend(url for url in storages if isinstance(url, str) and url)
    result = []
    for url in values:
        validate_url(url)
        for candidate in mirror_urls(url):
            if candidate not in result:
                result.append(candidate)
    return tuple(result)


def _size(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _digest(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{32}", value):
        return value.lower()
    return None


def _relative_dir(book: Book) -> tuple[str, ...]:
    for index, path in enumerate(book.category_paths):
        if path == book.primary_category_path:
            tags = book.path_tags[index] if index < len(book.path_tags) else {}
            return tuple(tags[key] for key in ("stage", "subject", "edition") if tags.get(key))
    return ()


class SmartEduProvider:
    def __init__(self, client: SmartEduClient | None = None) -> None:
        self.client = client or SmartEduClient()

    def fetch_catalog(self, previous=None):
        return fetch_catalog(self.client, previous=previous)

    def _get_json(self, url: str, credentials: Credentials | None, cancel: threading.Event | None):
        if credentials is None and cancel is None:
            return self.client.get_json(url)
        return self.client.get_json(url, credentials=credentials, cancel=cancel)

    def _chapters(
        self, items: list[dict], credentials: Credentials | None, cancel: threading.Event | None
    ) -> tuple[list[dict], list[str]]:
        mapping_item = next((item for item in items if item.get("ti_file_flag") == "ebook_mapping"), None)
        if mapping_item is None:
            return [], []
        try:
            urls = _item_urls(mapping_item)
            if not urls:
                raise AppError("PROVIDER_SCHEMA_CHANGED", "平台章节映射缺少下载地址。")
            mapping = self._get_json(urls[0], credentials, cancel)
            if not isinstance(mapping, dict) or not isinstance(mapping.get("mappings", []), list):
                raise AppError("PROVIDER_SCHEMA_CHANGED", "平台章节映射结构无效。")
            pages = {}
            for entry in mapping.get("mappings", []):
                if isinstance(entry, dict) and entry.get("node_id"):
                    page = _size(entry.get("page_number"))
                    if page is not None and page >= 1:
                        pages[str(entry["node_id"])] = page
            chapters = []
            ebook_id = mapping.get("ebook_id")
            tree_error = False
            if isinstance(ebook_id, str) and RESOURCE_ID_RE.fullmatch(ebook_id):
                try:
                    tree = self._get_json(
                        f"{BASE_URL}/zxx/ndrv2/national_lesson/trees/{ebook_id}.json", credentials, cancel
                    )
                    if isinstance(tree, list):
                        nodes = tree
                    elif isinstance(tree, dict) and isinstance(tree.get("child_nodes"), list):
                        nodes = tree["child_nodes"]
                    else:
                        # child_nodes 缺失或为 null 等异常结构：转为警告并回退页码索引，
                        # 不让 TypeError 绕过章节降级而阻断整个下载。
                        nodes, tree_error = [], True

                    def convert(branch: list, depth: int = 0) -> list[dict]:
                        if not isinstance(branch, list):
                            return []
                        if depth > 32:
                            raise AppError("PROVIDER_SCHEMA_CHANGED", "平台章节树层级过深。")
                        result = []
                        for node in branch:
                            if not isinstance(node, dict) or not node.get("title"):
                                continue
                            item = {"title": str(node["title"]), "page_index": pages.get(str(node.get("id")))}
                            children = node.get("child_nodes")
                            if isinstance(children, list):
                                item["children"] = convert(children, depth + 1)
                            result.append(item)
                        return result

                    chapters = convert(nodes)
                except Cancelled:
                    raise
                except AppError:
                    tree_error = True
            if not chapters:
                chapters = [
                    {"title": f"第 {index} 节 (P{page})", "page_index": page}
                    for index, (_node, page) in enumerate(sorted(pages.items(), key=lambda entry: entry[1]), 1)
                ]
            warnings = ["章节标题获取失败，已使用章节映射中的页码索引。"] if tree_error and chapters else []
            return chapters, warnings
        except Cancelled:
            raise
        except AppError:
            return [], ["平台章节映射获取失败；将保留可用 PDF，书签可能不可用。"]

    def resolve(
        self,
        resource_id: str,
        bookmarks: bool = False,
        credentials: Credentials | None = None,
        book: Book | None = None,
        cancel: threading.Event | None = None,
    ) -> list[Artifact]:
        resource_id = normalize_input(resource_id)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        data = self._get_json(DETAIL_URL.format(resource_id=resource_id), credentials, cancel)
        if not isinstance(data, dict):
            raise AppError("PROVIDER_SCHEMA_CHANGED", "教材详情不是有效对象。")
        if (data.get("resource_type_code") or "assets_document") != "assets_document":
            raise AppError("UNSUPPORTED_RESOURCE", "该资源不是普通电子教材。")
        title = _title(data, resource_id)
        record = {**data, "content_id": resource_id, "title": title}
        book = book or book_from_record(record)
        resource_records = [data] if isinstance(data.get("ti_items"), list) else []
        relations = data.get("relations")
        if isinstance(relations, dict):
            for values in relations.values():
                if isinstance(values, list):
                    resource_records.extend(value for value in values if isinstance(value, dict))
        if not resource_records:
            raise AppError("PROVIDER_SCHEMA_CHANGED", "教材详情缺少 ti_items 文件列表。")
        artifacts = {}
        for resource_record in resource_records:
            raw_items = resource_record.get("ti_items")
            if not isinstance(raw_items, list):
                continue
            items = [item for item in raw_items if isinstance(item, dict)]
            pdf_items = [item for item in items if str(item.get("ti_format", "")).lower() == "pdf"]
            sources = [item for item in pdf_items if item.get("ti_is_source_file") or item.get("ti_file_flag") == "source"]
            candidates = sources or [item for item in pdf_items if item.get("ti_file_flag") in (None, "pdf")]
            chapters, warnings = self._chapters(items, credentials, cancel) if bookmarks else ([], [])
            for item in candidates:
                urls = _item_urls(item)
                if not urls:
                    continue
                item_id = item.get("ti_id") or item.get("id")
                artifact_id = str(item_id) if item_id else hashlib.sha256(
                    unquote(urlsplit(urls[0]).path).encode("utf-8")
                ).hexdigest()[:24]
                artifact_title = _title(resource_record, title)
                if resource_record is not data and artifact_title != title:
                    artifact_title = f"{title} - {artifact_title}"
                revision_parts = [
                    value for value in (
                        item.get("revision") or item.get("version") or data.get("revision"),
                        data.get("version_id"),
                        data.get("update_time"),
                    ) if value is not None
                ]
                revision = ":".join(str(value) for value in revision_parts) or None
                item_warnings = list(warnings)
                if item.get("ti_md5") and _digest(item["ti_md5"]) is None:
                    item_warnings.append("平台 ti_md5 不是标准 MD5，未将其用作传输摘要；仍校验可用大小并记录本地 SHA-256。")
                artifact = Artifact(
                    resource_id=resource_id,
                    artifact_id=artifact_id,
                    title=artifact_title,
                    url=urls[0],
                    candidate_urls=urls,
                    expected_size=_size(item.get("ti_size")),
                    expected_md5=_digest(item.get("ti_md5")),
                    upstream_revision=revision,
                    chapters=chapters,
                    relative_dir=_relative_dir(book),
                    warnings=item_warnings,
                )
                artifacts.setdefault(artifact_id, artifact)
        if not artifacts:
            raise AppError("UNSUPPORTED_RESOURCE", "该教材没有可下载的 PDF 文件；音频与课程不在首版范围内。")
        return list(artifacts.values())
