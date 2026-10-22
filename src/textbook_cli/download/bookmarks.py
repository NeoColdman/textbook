"""Optional, separate-output PDF processing.

Adapted from tchMaterial-parser (MIT), copyright (c) 2026 肥宅水水呀.
See THIRD_PARTY_NOTICES.md and licenses/tchMaterial-parser-MIT.txt.
"""

from __future__ import annotations

import os
from pathlib import Path


def add_bookmarks(source: Path, destination: Path, chapters: list[dict]) -> int:
    """将章节写入 PDF 书签并返回实际写入的书签数量。

    Delayed import keeps pypdf optional for ordinary downloads and core imports.
    """
    from pypdf import PdfReader, PdfWriter, apply_configuration, get_configuration

    stream_limit = max(get_configuration().maximum_declared_stream_length, Path(source).stat().st_size)
    with apply_configuration(maximum_declared_stream_length=stream_limit):
        reader = PdfReader(source)
        writer = PdfWriter()
        writer.append_pages_from_reader(reader)
        if reader.metadata:
            writer.add_metadata({str(key): str(value) for key, value in reader.metadata.items() if value is not None})
        written = 0

        def visit(nodes: list[dict], parent=None) -> None:
            nonlocal written
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                try:
                    page = int(node.get("page_index")) - 1
                except (ValueError, TypeError):
                    page = None
                bookmark = parent
                if page is not None and 0 <= page < len(writer.pages):
                    # 页码无效的父节点只跳过自身书签；其页码有效的子节点仍要写入，
                    # 不能因父节点缺页而把整棵子树一并丢弃。
                    bookmark = writer.add_outline_item(str(node.get("title") or "未知章节"), page, parent=parent)
                    written += 1
                if isinstance(node.get("children"), list):
                    visit(node["children"], bookmark)

        visit(chapters)
        with Path(destination).open("wb") as target:
            writer.write(target)
            target.flush()
            os.fsync(target.fileno())
        return written
