"""浏览器凭据导入的最小 LevelDB 解析契约；全部使用合成数据，不读取真实浏览器。"""

import json
import struct
from pathlib import Path

import pytest

import textbook_cli.storage.browser_import as browser_import
from textbook_cli.core.models import Credentials
from textbook_cli.storage.browser_import import (
    _LOG_BLOCK_SIZE,
    BrowserCredential,
    _credential_from_record,
    _iter_log_records,
    _iter_table_records,
    _snappy_decompress,
    group_candidates,
    import_from_browser,
)

pytestmark = pytest.mark.contract

MAGIC = 0xDB4775248B80FB57
KEY = b"_https://basic.smartedu.cn\x00\x01ND_UC_AUTH-abc&ncet-xedu&token"


def _varint_bytes(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _block(entries):
    body = b"".join(
        _varint_bytes(0) + _varint_bytes(len(k)) + _varint_bytes(len(v)) + k + v
        for k, v in entries
    )
    return body + struct.pack("<I", 0)  # 单重启点


def _write_ldb(path: Path, entries):
    data_block = _block(entries)
    data_part = data_block + b"\x00" + b"\x00" * 4  # 块内容 + 无压缩标记 + 占位校验
    index_block = _block([(b"\x00", _varint_bytes(0) + _varint_bytes(len(data_block)))])
    index_part = index_block + b"\x00" + b"\x00" * 4
    footer = _varint_bytes(0) + _varint_bytes(0) + _varint_bytes(len(data_part)) + _varint_bytes(len(index_block))
    footer += b"\x00" * (40 - len(footer)) + struct.pack("<Q", MAGIC)
    path.write_bytes(data_part + index_part + footer)


def _entry(kind: int, key: bytes, value: bytes = b"") -> bytes:
    out = bytes([kind]) + _varint_bytes(len(key)) + key
    if kind == 1:
        out += _varint_bytes(len(value)) + value
    return out


def _batch_payload(sequence: int, entries) -> bytes:
    payload = struct.pack("<Q", sequence) + struct.pack("<I", len(entries))
    for kind, key, value in entries:
        payload += _entry(kind, key, value)
    return payload


def _pad_batch(sequence: int, entries, size: int) -> bytes:
    """构建恰好 size 字节的 WriteBatch 载荷；用一个无关键值对补足长度。"""
    payload = struct.pack("<Q", sequence) + struct.pack("<I", len(entries) + 1)
    for kind, key, value in entries:
        payload += _entry(kind, key, value)
    for varint_len in (1, 2, 3, 4, 5):
        filler = size - len(payload) - (1 + 1 + 1 + varint_len)
        if filler >= 0 and len(_varint_bytes(filler)) == varint_len:
            payload += _entry(1, b"x", b"z" * filler)
            assert len(payload) == size
            return payload
    raise AssertionError("无法在给定大小内构造 WriteBatch 载荷")


def _log_record(sequence: int, entries) -> bytes:
    payload = _batch_payload(sequence, entries)
    return struct.pack("<I", 0) + struct.pack("<H", len(payload)) + b"\x01" + payload


def _write_log(path: Path, sequence: int, entries) -> None:
    path.write_bytes(_log_record(sequence, entries))


def _token_value(**over) -> bytes:
    inner = {"access_token": "tok", "mac_key": "mk", "diff": 5, **over}
    return b"\x01" + json.dumps({"value": json.dumps(inner)}).encode("utf-8")


def _isolate_browser(tmp_path, monkeypatch) -> None:
    """替换浏览器目录发现函数：测试不依赖 Path.home、LOCALAPPDATA 或 XDG_CONFIG_HOME。"""
    monkeypatch.setattr(browser_import, "_browser_bases", lambda: [("Chrome", tmp_path)])


def test_snappy_literal_and_copy():
    assert _snappy_decompress(b"\x05\x00a\x01\x01") == b"aaaaa"


def test_table_parser_extracts_token_entry(tmp_path):
    seqtag = struct.pack("<Q", (77 << 8) | 1)
    _write_ldb(tmp_path / "000005.ldb", [(KEY + seqtag, _token_value())])
    records = list(_iter_table_records((tmp_path / "000005.ldb").read_bytes()))
    assert len(records) == 1
    sequence, key, kind, raw = records[0]
    assert sequence == 77 and key == KEY and kind == 1
    credentials = _credential_from_record(key, raw)
    assert credentials.access_token == "tok" and credentials.diff == 5


def test_table_parser_keeps_delete_tags_for_latest_state_resolution(tmp_path):
    delete_tag = struct.pack("<Q", (9 << 8) | 0)
    _write_ldb(tmp_path / "a.ldb", [(KEY + delete_tag, b"")])
    records = list(_iter_table_records((tmp_path / "a.ldb").read_bytes()))
    assert [(sequence, kind) for sequence, _key, kind, _value in records] == [(9, 0)]


def test_log_parser_extracts_token_entry(tmp_path):
    _write_log(tmp_path / "000006.log", 5000, [(1, KEY, _token_value(diff=7))])
    records = list(_iter_log_records((tmp_path / "000006.log").read_bytes()))
    assert records[0][0] == 5000 and records[0][2] == 1
    credentials = _credential_from_record(records[0][1], records[0][3])
    assert credentials.access_token == "tok" and credentials.diff == 7


def test_log_block_padding_does_not_hide_later_credentials(tmp_path, monkeypatch):
    """块尾不足 7 字节的填充不能被当作记录头；下一块的凭据记录必须仍能解析。"""
    level_dir = tmp_path / "Default" / "Local Storage" / "leveldb"
    level_dir.mkdir(parents=True)
    # 第一条记录恰好结束于块尾前 3 字节（放不下 7 字节记录头），下一条从 32KiB 块边界开始。
    first = struct.pack("<I", 0) + struct.pack("<H", _LOG_BLOCK_SIZE - 10) + b"\x01" + _pad_batch(
        100, [(1, KEY, _token_value(diff=1))], _LOG_BLOCK_SIZE - 7 - 3
    )
    assert len(first) == _LOG_BLOCK_SIZE - 3
    second = _log_record(200, [(1, KEY, _token_value(diff=2))])
    (level_dir / "000006.log").write_bytes(first + b"\x00" * 3 + second)
    _isolate_browser(tmp_path, monkeypatch)
    candidates = import_from_browser()
    # 同键两次写入，按序号取最新状态：应采用第二条（diff=2），第一条不得复活。
    assert len(candidates) == 1
    assert candidates[0].sequence == 200
    assert candidates[0].credentials.diff == 2


def test_deleted_credentials_are_not_imported(tmp_path, monkeypatch):
    """先写入后删除的键不得把旧凭据带回结果，无论删除记录在哪个文件。"""
    level_dir = tmp_path / "Default" / "Local Storage" / "leveldb"
    level_dir.mkdir(parents=True)
    _write_ldb(level_dir / "000005.ldb", [(KEY + struct.pack("<Q", (5 << 8) | 1), _token_value())])
    _write_log(level_dir / "000006.log", 9, [(0, KEY, b"")])
    _isolate_browser(tmp_path, monkeypatch)
    assert import_from_browser() == []


def test_delete_before_rewrite_imports_newest_credentials(tmp_path, monkeypatch):
    """删除之后重新登录写入：最新状态是写入，应导入新序号的新凭据。"""
    level_dir = tmp_path / "Default" / "Local Storage" / "leveldb"
    level_dir.mkdir(parents=True)
    _write_ldb(level_dir / "000005.ldb", [(KEY + struct.pack("<Q", (5 << 8) | 0), b"")])
    _write_log(level_dir / "000006.log", 9, [(1, KEY, _token_value(diff=3))])
    _isolate_browser(tmp_path, monkeypatch)
    candidates = import_from_browser()
    assert len(candidates) == 1
    assert candidates[0].sequence == 9
    assert candidates[0].credentials.diff == 3


def test_credentials_from_lookalike_and_foreign_origins_are_rejected(tmp_path, monkeypatch):
    """来源按解析出的主机名精确校验；smartedu.attacker.example 之类不能通过。"""
    level_dir = tmp_path / "Default" / "Local Storage" / "leveldb"
    level_dir.mkdir(parents=True)
    lookalike = b"_https://smartedu.attacker.example\x00\x01ND_UC_AUTH-x&ncet-xedu&token"
    foreign = b"_https://evil.example\x00\x01ND_UC_AUTH-x&token"
    prefixless = b"ND_UC_AUTH-x&token"
    seqtag = struct.pack("<Q", (7 << 8) | 1)
    _write_ldb(level_dir / "000005.ldb", [
        (lookalike + seqtag, _token_value(diff=1)),
        (foreign + seqtag, _token_value(diff=2)),
        (prefixless + seqtag, _token_value(diff=3)),
        (KEY + seqtag, _token_value(diff=4)),
    ])
    _isolate_browser(tmp_path, monkeypatch)
    candidates = import_from_browser()
    assert [item.credentials.diff for item in candidates] == [4]


def test_group_candidates_orders_within_database_only():
    candidates = [
        BrowserCredential(Credentials("a", "k", 1), "Chrome", "Default", 10),
        BrowserCredential(Credentials("b", "k", 2), "Edge", "Profile 1", 99),
        BrowserCredential(Credentials("c", "k", 3), "Chrome", "Default", 50),
    ]
    groups = group_candidates(candidates)
    assert [[item.credentials.access_token for item in group] for group in groups] == [["c", "a"], ["b"]]


def test_discovery_scans_chrome_profiles(tmp_path, monkeypatch):
    level_dir = tmp_path / "Default" / "Local Storage" / "leveldb"
    level_dir.mkdir(parents=True)
    seqtag = struct.pack("<Q", (88 << 8) | 1)
    _write_ldb(level_dir / "000010.ldb", [(KEY + seqtag, _token_value(diff=9))])
    _isolate_browser(tmp_path, monkeypatch)
    candidates = import_from_browser()
    assert len(candidates) == 1
    assert candidates[0].browser == "Chrome"
    assert candidates[0].profile == "Default"
    assert candidates[0].sequence == 88
    assert candidates[0].credentials.diff == 9


def test_missing_browser_storage_raises_actionable_error(tmp_path, monkeypatch):
    _isolate_browser(tmp_path, monkeypatch)
    with pytest.raises(Exception) as exc_info:
        import_from_browser()
    assert "未找到" in str(exc_info.value)
