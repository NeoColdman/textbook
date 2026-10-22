"""Catalog and resolver contracts using minimal public metadata and synthetic data."""

import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from textbook_cli.core.catalog import CatalogService, is_stale, search_books
from textbook_cli.core.errors import AppError, Cancelled
from textbook_cli.core.models import Book, CatalogSnapshot, Credentials
from textbook_cli.providers.smartedu import SmartEduProvider
from textbook_cli.providers.smartedu.catalog import TAGS_URL, VERSION_URL, book_from_record, parse_categories
from textbook_cli.providers.smartedu.resolver import DETAIL_URL, normalize_input
from textbook_cli.storage.cache import CatalogCache

pytestmark = pytest.mark.contract

FIXTURES = Path(__file__).parents[1] / "fixtures" / "smartedu"
PART = "https://s-file-1.ykt.cbern.com.cn/part_100.json"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeJSON:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def sample_provider():
    detail = fixture("live_sample_detail")
    return SmartEduProvider(FakeJSON({
        TAGS_URL: fixture("live_sample_tags"), VERSION_URL: {"module_version": 123, "urls": PART},
        PART: [detail], DETAIL_URL.format(resource_id=detail["id"]): detail,
    }))


def snapshot_at(hours=0):
    now = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return CatalogSnapshot(books=[], fetched_at=now, checked_at=now, source="synthetic")


def multipath_snapshot():
    book = Book(
        "a001", "义务教育教科书 七年级下册", category_paths=(
            ("小学", "科学", "统编版"), ("初中", "数学", "人教版", "七年级"),
        ), path_tags=(
            {"stage": "小学", "subject": "科学", "edition": "统编版"},
            {"stage": "初中", "subject": "数学", "edition": "人教版", "grade": "七年级"},
        ),
    )
    result = snapshot_at()
    result.books = [book, Book("b001", "无年级元数据 七年级下册")]
    return result


def test_real_minimal_fixture_normalizes_dynamic_categories_and_dimensions():
    provider = sample_provider()
    snapshot = provider.fetch_catalog()
    assert snapshot.module_version == "123"
    assert len(snapshot.books) == 1
    book = snapshot.books[0]
    tags = book.path_tags[0]
    assert tags == {"stage": "小学", "subject": "道德与法治", "edition": "统编版", "grade": "一年级"}
    assert book.primary_category_path[-1] == "上册"
    assert snapshot.fetched_at == snapshot.checked_at
    assert snapshot.categories


def test_unchanged_version_reuses_only_when_partitions_and_categories_also_match():
    provider = sample_provider()
    first = provider.fetch_catalog()
    provider.client.calls.clear()
    second = provider.fetch_catalog(first)
    assert second.fetched_at == first.fetched_at
    assert second.checked_at >= first.checked_at
    assert [url for url, _ in provider.client.calls] == [TAGS_URL, VERSION_URL]
    provider.client.mapping[TAGS_URL]["hierarchies"][0]["children"][0]["tag_name"] += "更新"
    provider.client.calls.clear()
    third = provider.fetch_catalog(second)
    assert PART in [url for url, _ in provider.client.calls]
    assert third.category_digest != second.category_digest


def test_multiple_paths_and_duplicate_records_merge_by_resource_identity():
    provider = sample_provider()
    record = fixture("live_sample_detail")
    record["content_id"] = "canonical-id"
    provider.client.mapping[PART] += [record, copy.deepcopy(record)]
    snapshot = provider.fetch_catalog()
    assert len(snapshot.books) == 2
    assert len(next(b for b in snapshot.books if b.resource_id == "canonical-id").category_paths) == 1


def test_required_partition_failure_and_empty_catalog_do_not_return_partial_snapshot():
    provider = sample_provider()
    provider.client.mapping[PART] = AppError("NETWORK_ERROR", "offline")
    with pytest.raises(AppError):
        provider.fetch_catalog()
    provider.client.mapping[PART] = []
    with pytest.raises(AppError) as caught:
        provider.fetch_catalog()
    assert caught.value.code == "PROVIDER_SCHEMA_CHANGED"


def test_invalid_records_are_isolated_and_empty_categories_never_become_books():
    provider = sample_provider()
    provider.client.mapping[PART] += [{"id": "missing-title", "tag_paths": ["anything"]}, {"id": "folder", "title": "空分类"}]
    snapshot = provider.fetch_catalog()
    assert len(snapshot.books) == 1
    assert len(snapshot.warnings) == 1


def test_normal_unsupported_types_over_one_percent_do_not_count_as_bad_schema():
    provider = sample_provider()
    ordinary = fixture("live_sample_detail")
    courses = [{**ordinary, "id": f"course-{index}", "resource_type_code": "thematic_course"} for index in range(30)]
    provider.client.mapping[PART] = [ordinary, *courses]
    snapshot = provider.fetch_catalog()
    assert [book.resource_id for book in snapshot.books] == [ordinary["id"]]
    assert any("30 条专题课程" in warning for warning in snapshot.warnings)
    assert not any("结构无效" in warning for warning in snapshot.warnings)


def test_many_invalid_ordinary_records_have_distinct_schema_error():
    provider = sample_provider()
    provider.client.mapping[PART] += [{"id": f"broken-{index}", "tag_paths": ["something"]} for index in range(11)]
    with pytest.raises(AppError) as caught:
        provider.fetch_catalog()
    assert caught.value.code == "PROVIDER_SCHEMA_CHANGED"
    assert "11 条普通教材记录结构异常" in caught.value.message
    assert "没有可识别" not in caught.value.message


def test_non_array_tag_list_is_a_schema_error_not_type_error():
    record = fixture("live_sample_detail")
    with pytest.raises(AppError) as caught:
        book_from_record({**record, "tag_list": 5}, {})
    assert caught.value.code == "PROVIDER_SCHEMA_CHANGED"


def test_record_with_non_array_tag_list_is_isolated_and_cache_fallback_survives():
    provider = sample_provider()
    record = fixture("live_sample_detail")
    provider.client.mapping[PART] = [record, {**record, "id": "broken-tag-list", "tag_list": 5}]
    snapshot = provider.fetch_catalog()
    assert [book.resource_id for book in snapshot.books] == [record["id"]]
    assert any("结构无效" in warning for warning in snapshot.warnings)


def test_unhashable_dimension_id_in_record_is_schema_error_not_type_error():
    """tag_dimension_id 为数组/对象时 DIMENSIONS.get 会因键不可哈希抛 TypeError，需转为结构化错误。"""
    record = fixture("live_sample_detail")
    record["tag_list"] = [{"tag_id": "t1", "tag_name": "数学", "tag_dimension_id": ["zxxxk"]}]
    with pytest.raises(AppError) as caught:
        book_from_record(record, {})
    assert caught.value.code == "PROVIDER_SCHEMA_CHANGED"


def test_record_with_unhashable_dimension_id_is_isolated_and_cache_fallback_survives():
    provider = sample_provider()
    record = fixture("live_sample_detail")
    provider.client.mapping[PART] = [
        record,
        {**record, "id": "broken-dimension", "tag_list": [{"tag_id": "t1", "tag_name": "数学", "tag_dimension_id": {"zxxxk": "数学"}}]},
    ]
    snapshot = provider.fetch_catalog()
    assert [book.resource_id for book in snapshot.books] == [record["id"]]
    assert any("结构无效" in warning for warning in snapshot.warnings)


def test_unhashable_dimension_id_in_category_parsing_is_structured_error():
    """分类标签文件中的维度 ID 类型异常同样转为结构化错误，触发旧缓存回退而非崩溃。"""
    tags = fixture("live_sample_tags")
    tags["hierarchies"][0]["tag_dimension_id"] = ["zxxxd"]  # 分组层级（原第 54 行）
    with pytest.raises(AppError) as caught:
        parse_categories(tags)
    assert caught.value.code == "PROVIDER_SCHEMA_CHANGED"
    tags = fixture("live_sample_tags")
    tags["hierarchies"][0]["children"][0]["tag_dimension_id"] = {"zxxxd": "小学"}  # 节点层级（原第 59 行）
    with pytest.raises(AppError) as caught:
        parse_categories(tags)
    assert caught.value.code == "PROVIDER_SCHEMA_CHANGED"


def test_search_joint_filters_stay_on_one_path_and_return_all_path_information():
    snapshot = multipath_snapshot()
    result = search_books(snapshot, "数学 下册", stage="初中", grade="七年级")
    assert result["total"] == 1
    item = result["items"][0]
    assert item["stage"] == "初中"
    assert len(item["category_paths"]) == 2
    assert len(item["matched_category_paths"]) == 1
    assert search_books(snapshot, "", stage="小学", subject="数学")["total"] == 0
    assert search_books(snapshot, "小学 数学")["total"] == 0
    # The second book's title is not evidence of its grade.
    assert search_books(snapshot, "七年级", grade="七年级")["total"] == 1


def test_search_is_unique_stable_and_paginates_after_filtering():
    snapshot = multipath_snapshot()
    assert search_books(snapshot, "下册", limit=1)["total"] == 2
    assert len(search_books(snapshot, "下册", limit=1)["items"]) == 1
    assert search_books(snapshot, "下册", offset=2)["items"] == []
    for values in ({"limit": 0}, {"limit": 1001}, {"offset": -1}):
        with pytest.raises(AppError) as caught:
            search_books(snapshot, "", **values)
        assert caught.value.exit_code == 2


def test_search_time_alias_and_absent_dimension_warning(tmp_path):
    snapshot = snapshot_at()
    snapshot.books = [Book("book-1", "没有年级标签", category_paths=(("初中",),), path_tags=({"stage": "初中"},))]
    path = tmp_path / "catalog.json"
    CatalogCache(path).save(snapshot)
    service = CatalogService(cache_path=path)
    result = service.search("", grade="七年级")
    assert result["total"] == 0
    assert result["catalog"]["updated_at"] == result["catalog"]["fetched_at"]
    assert any("年级元数据" in warning for warning in service.last_warnings)


def test_cache_rejects_invalid_timestamp_and_inconsistent_path_tags(tmp_path):
    cache = CatalogCache(tmp_path / "catalog.json")
    data = multipath_snapshot().to_dict()
    data["checked_at"] = "not-a-timestamp"
    cache.path.write_text(json.dumps(data))
    assert cache.load() is None
    data = multipath_snapshot().to_dict()
    data["books"][0]["path_tags"] = []
    cache.path.write_text(json.dumps(data))
    assert cache.load() is None


def test_cache_round_trip_preserves_paths_tags_and_timestamps(tmp_path):
    cache = CatalogCache(tmp_path / "catalog.json")
    snapshot = multipath_snapshot()
    cache.save(snapshot)
    loaded = cache.load()
    assert loaded.to_dict() == snapshot.to_dict()
    assert isinstance(loaded.books[0].category_paths[0], tuple)
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_or_incompatible_cache_is_not_usable(tmp_path):
    cache = CatalogCache(tmp_path / "catalog.json")
    for content in ("<html/>", '{"schema_version":999}', '{"schema_version":1}'):
        cache.path.write_text(content)
        assert cache.load() is None
        assert cache.last_error


def test_failed_atomic_write_preserves_previous_cache(tmp_path, monkeypatch):
    cache = CatalogCache(tmp_path / "catalog.json")
    cache.save(snapshot_at())
    before = cache.path.read_bytes()

    def fail_replace(*_args):
        raise OSError("synthetic failure")

    monkeypatch.setattr("textbook_cli.storage.cache.os.replace", fail_replace)
    with pytest.raises(AppError):
        cache.save(snapshot_at(hours=3))
    assert cache.path.read_bytes() == before


def test_stale_fallback_does_not_change_checked_time_and_explicit_refresh_fails(tmp_path):
    old = snapshot_at(hours=25)
    path = tmp_path / "catalog.json"
    CatalogCache(path).save(old)

    class Offline:
        def fetch_catalog(self, previous=None):
            raise AppError("NETWORK_ERROR", "offline")

    service = CatalogService(Offline(), path)
    loaded = service.load()
    assert loaded.checked_at == old.checked_at and loaded.stale
    assert service.last_warnings
    assert service.status()["stale"] is True
    with pytest.raises(AppError):
        service.load(refresh=True)
    assert CatalogCache(path).load().checked_at == old.checked_at


def test_fresh_cache_and_cached_only_do_not_call_provider(tmp_path):
    class NoNetwork:
        def fetch_catalog(self, previous=None):
            pytest.fail("unexpected network")

    path = tmp_path / "catalog.json"
    service = CatalogService(NoNetwork(), path)
    assert service.cached() is None
    assert service.status()["exists"] is False
    CatalogCache(path).save(snapshot_at(hours=1))
    assert service.load().stale is False
    assert not is_stale(service.cached())


@pytest.mark.parametrize("value", [
    "https://evil.example/tchMaterial/detail?contentId=a001",
    "http://basic.smartedu.cn/tchMaterial/detail?contentId=a001",
    "https://basic.smartedu.cn/tchMaterial/detail?contentId=../secret",
    "https://basic.smartedu.cn/tchMaterial/detail?contentId=a&contentId=b",
    "https://basic.smartedu.cn/syncClassroom/detail?resourceId=a001",
])
def test_normalize_input_rejects_unsupported_or_unsafe_inputs(value):
    with pytest.raises(AppError) as caught:
        normalize_input(value)
    assert caught.value.exit_code == 2


def test_normalize_input_extracts_only_ordinary_book_id():
    assert normalize_input("a001-abc") == "a001-abc"
    assert normalize_input("https://basic.smartedu.cn/tchMaterial/detail?contentId=a001") == "a001"
    with pytest.raises(AppError) as caught:
        normalize_input("https://basic.smartedu.cn/tchMaterial/detail?contentId=a001&contentType=thematic_course")
    assert caught.value.code == "UNSUPPORTED_RESOURCE"


def test_resolver_real_metadata_is_pdf_only_with_size_revision_and_etag_warning():
    provider = sample_provider()
    detail = fixture("live_sample_detail")
    [artifact] = provider.resolve(detail["id"])
    assert artifact.expected_size == 22620491
    assert artifact.file_format == "pdf"
    assert len(artifact.candidate_urls) == 3
    assert artifact.expected_md5 is None  # Multipart ETag is not a plain MD5.
    assert artifact.upstream_revision
    assert artifact.relative_dir == ("小学", "道德与法治", "统编版")
    assert artifact.warnings
    assert len(provider.client.calls) == 1  # No audio request or mapping unless requested.


def test_resolver_artifact_identity_is_stable_across_mirrors_and_query_signature_changes():
    provider = sample_provider()
    detail = fixture("live_sample_detail")
    [before] = provider.resolve(detail["id"])
    current = provider.client.mapping[DETAIL_URL.format(resource_id=detail["id"])]
    for item in current["ti_items"]:
        if item["ti_format"] == "pdf":
            item["ti_storage"] = item["ti_storages"][1] + "?Signature=rotating"
            item["ti_storages"] = []
    [after] = provider.resolve(detail["id"])
    assert before.artifact_id == after.artifact_id


def test_resolver_bookmarks_join_mapping_and_tree_and_preserve_warning_on_failure():
    provider = sample_provider()
    detail = fixture("live_sample_detail")
    mapping_item = detail["ti_items"][0]
    mapping_url = mapping_item["ti_storages"][0]
    provider.client.mapping[mapping_url] = {"ebook_id": "ebook-test", "mappings": [{"node_id": "unit-1", "page_number": 3}]}
    tree_url = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrv2/national_lesson/trees/ebook-test.json"
    provider.client.mapping[tree_url] = [{"id": "unit-1", "title": "第一单元", "child_nodes": []}]
    credentials = Credentials("fake-token", "fake-key")
    [artifact] = provider.resolve(detail["id"], bookmarks=True, credentials=credentials)
    assert artifact.chapters == [{"title": "第一单元", "page_index": 3, "children": []}]
    assert next(kwargs for url, kwargs in provider.client.calls if url == mapping_url)["credentials"] is credentials
    provider.client.mapping[mapping_url] = AppError("NETWORK_ERROR", "offline")
    [artifact] = provider.resolve(detail["id"], bookmarks=True)
    assert artifact.chapters == [] and artifact.warnings


def test_resolver_mapping_falls_back_to_page_index_if_tree_is_unavailable():
    provider = sample_provider()
    detail = fixture("live_sample_detail")
    provider.client.mapping[detail["ti_items"][0]["ti_storages"][0]] = {
        "ebook_id": "ebook-test", "mappings": [{"node_id": "unit-1", "page_number": 2}],
    }
    provider.client.mapping["https://s-file-1.ykt.cbern.com.cn/zxx/ndrv2/national_lesson/trees/ebook-test.json"] = AppError("NETWORK_ERROR", "offline")
    [artifact] = provider.resolve(detail["id"], bookmarks=True)
    assert artifact.chapters[0]["page_index"] == 2
    assert artifact.warnings


def test_resolver_invalid_chapter_tree_degrades_to_warning_instead_of_type_error():
    """child_nodes=null 等异常章节结构只降级为警告；开启书签的下载不被阻断。"""
    provider = sample_provider()
    detail = fixture("live_sample_detail")
    provider.client.mapping[detail["ti_items"][0]["ti_storages"][0]] = {
        "ebook_id": "ebook-test", "mappings": [{"node_id": "unit-1", "page_number": 2}],
    }
    provider.client.mapping["https://s-file-1.ykt.cbern.com.cn/zxx/ndrv2/national_lesson/trees/ebook-test.json"] = {"child_nodes": None}
    [artifact] = provider.resolve(detail["id"], bookmarks=True)
    assert artifact.chapters[0]["page_index"] == 2
    assert any("章节标题获取失败" in warning for warning in artifact.warnings)


def test_resolver_bad_schema_offhost_files_and_cancellation_are_explicit():
    provider = sample_provider()
    detail = fixture("live_sample_detail")
    target = DETAIL_URL.format(resource_id=detail["id"])
    provider.client.mapping[target] = {"title": "missing-items"}
    with pytest.raises(AppError) as caught:
        provider.resolve(detail["id"])
    assert caught.value.code == "PROVIDER_SCHEMA_CHANGED"
    provider.client.mapping[target] = detail
    detail["ti_items"][1]["ti_storage"] = "https://evil.example/book.pdf"
    with pytest.raises(AppError):
        provider.resolve(detail["id"])
    event = threading.Event()
    event.set()
    with pytest.raises(Cancelled):
        provider.resolve(detail["id"], cancel=event)


def test_tag_dimensions_are_taken_from_metadata_not_assumed_depth():
    tags, index = parse_categories(fixture("live_sample_tags"))
    assert tags
    book = book_from_record(fixture("live_sample_detail"), index)
    assert book.path_tags[0]["grade"] == "一年级"
