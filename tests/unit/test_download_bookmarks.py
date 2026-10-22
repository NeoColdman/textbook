"""MIT-adapted regression cases from tchMaterial-parser's bookmark tests."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from textbook_cli.download.bookmarks import add_bookmarks

pytestmark = pytest.mark.contract


@pytest.fixture
def image_pdf(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=32, height=32)
    image = DecodedStreamObject()
    image.set_data(b"\x10\x20\x30\x40" * 1024)
    image.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(32), NameObject("/Height"): NumberObject(32),
        NameObject("/ColorSpace"): NameObject("/DeviceCMYK"), NameObject("/BitsPerComponent"): NumberObject(8),
    })
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image)})})
    content = DecodedStreamObject()
    content.set_data(b"q 32 0 0 32 0 0 cm /Im0 Do Q\n")
    page[NameObject("/Contents")] = writer._add_object(content)
    source = tmp_path / "source.pdf"
    writer.write(source)
    return source


def test_children_of_pageless_parent_are_still_written(image_pdf, tmp_path):
    """父章节缺页码时只跳过其自身书签，页码有效的子节点必须保留。"""
    from pypdf import PdfReader

    chapters = [{"title": "无页码父章", "children": [{"title": "子节", "page_index": 1}]}]
    destination = tmp_path / "bookmarked.pdf"
    written = add_bookmarks(image_pdf, destination, chapters)
    assert written == 1
    [outline] = PdfReader(destination).outline
    assert outline.title == "子节"


def test_out_of_range_parent_keeps_valid_children(image_pdf, tmp_path):
    from pypdf import PdfReader

    chapters = [{"title": "越界父章", "page_index": 99, "children": [{"title": "有效子节", "page_index": 1}]}]
    written = add_bookmarks(image_pdf, tmp_path / "bookmarked.pdf", chapters)
    assert written == 1
    [outline] = PdfReader(tmp_path / "bookmarked.pdf").outline
    assert outline.title == "有效子节"


def test_no_valid_pages_writes_nothing_and_reports_zero(image_pdf, tmp_path):
    from pypdf import PdfReader

    chapters = ["junk", {"title": "缺页码"}, {"title": "越界", "page_index": 9}]
    assert add_bookmarks(image_pdf, tmp_path / "bookmarked.pdf", chapters) == 0
    assert PdfReader(tmp_path / "bookmarked.pdf").outline == []


def test_large_image_and_nested_bookmarks_preserve_source(image_pdf, tmp_path):
    from pypdf import PdfReader, apply_configuration, get_configuration

    original_bytes = image_pdf.read_bytes()
    original_data = PdfReader(image_pdf).pages[0]["/Resources"]["/XObject"]["/Im0"].get_data()
    chapters = [{"title": "第一章", "page_index": 1, "children": [{"title": "第一节", "page_index": 1}]}]
    destination = tmp_path / "bookmarked.pdf"
    with apply_configuration(maximum_declared_stream_length=1024) as configuration:
        add_bookmarks(image_pdf, destination, chapters)
        assert get_configuration() == configuration
    result = PdfReader(destination)
    assert result.outline[0].title == "第一章" and result.outline[1][0].title == "第一节"
    assert result.pages[0]["/Resources"]["/XObject"]["/Im0"].get_data() == original_data
    assert image_pdf.read_bytes() == original_bytes


def test_unbounded_declared_length_does_not_modify_source(image_pdf, tmp_path):
    from pypdf import apply_configuration, get_configuration
    from pypdf.errors import LimitReachedError

    source = image_pdf.read_bytes().replace(b"/Length 4096", b"/Length 9999", 1)
    image_pdf.write_bytes(source)
    with apply_configuration(maximum_declared_stream_length=1024) as configuration:
        with pytest.raises(LimitReachedError):
            add_bookmarks(image_pdf, tmp_path / "modified.pdf", [{"title": "第一章", "page_index": 1}])
        assert get_configuration() == configuration
    assert image_pdf.read_bytes() == source


def test_large_pdf_limit_is_local_to_processing_context(image_pdf, tmp_path, monkeypatch):
    import pypdf
    from pypdf import apply_configuration, get_configuration
    from pypdf.errors import LimitReachedError

    original_reader = pypdf.PdfReader
    entered, release = threading.Event(), threading.Event()

    def waiting_reader(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(pypdf, "PdfReader", waiting_reader)
    with apply_configuration(maximum_declared_stream_length=1024) as configuration:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(copy_context().run, add_bookmarks, image_pdf, tmp_path / "modified.pdf", [{"title": "first", "page_index": 1}])
            try:
                assert entered.wait(5)
                assert get_configuration() == configuration
                with pytest.raises(LimitReachedError):
                    original_reader(image_pdf).pages[0]["/Resources"]["/XObject"]["/Im0"].get_data()
            finally:
                release.set()
            future.result(timeout=5)
