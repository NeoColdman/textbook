"""用户显式触发时，从本机 Chromium 系浏览器本地存储读取平台凭据。

只读取键名与来源域都匹配平台模式的记录，凭据值不进入日志或输出。
解析基于 LevelDB 表/日志格式的最小实现，不引入第三方依赖；
不支持时抛出可操作的 AppError，绝不静默降级。
"""

from __future__ import annotations

import json
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..core.errors import AppError
from ..core.models import Credentials

_TOKEN_MARK = b"ND_UC_AUTH"
_TOKEN_SUFFIX = b"&token"
# 平台自有域名：对解析出的主机名按点边界精确匹配，而不是字节子串包含，
# 因此 smartedu.attacker.example 之类同名前缀的第三方域名不会通过。
_ALLOWED_ORIGIN_SUFFIXES = ("smartedu.cn", "eduyun.cn", "ykt.cbern.com.cn")
_MAGIC = 0xDB4775248B80FB57
_MAX_VALUE_BYTES = 64 * 1024
_LOG_BLOCK_SIZE = 32 * 1024  # LevelDB 预写日志的固定块大小


@dataclass
class BrowserCredential:
    credentials: Credentials
    browser: str
    profile: str
    sequence: int


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _snappy_decompress(src: bytes) -> bytes:
    out = bytearray()
    declared, pos = _varint(src, 0)
    while pos < len(src) and len(out) < declared:
        tag = src[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:  # literal
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                extra = length - 59
                length = int.from_bytes(src[pos:pos + extra], "little") + 1
                pos += extra
            out += src[pos:pos + length]
            pos += length
        else:
            if kind == 1:
                length = ((tag >> 2) & 7) + 4
                offset = ((tag >> 5) << 8) | src[pos]
                pos += 1
            elif kind == 2:
                length = (tag >> 2) + 1
                offset = int.from_bytes(src[pos:pos + 2], "little")
                pos += 2
            else:
                length = (tag >> 2) + 1
                offset = int.from_bytes(src[pos:pos + 4], "little")
                pos += 4
            if offset <= 0 or offset > len(out):
                raise ValueError("snappy copy offset out of range")
            for _ in range(length):
                out.append(out[-offset])
    if len(out) != declared:
        raise ValueError("snappy length mismatch")
    return bytes(out)


def _block_entries(body: bytes):
    if len(body) < 4:
        return
    restarts = struct.unpack("<I", body[-4:])[0]
    limit = len(body) - 4 - 4 * restarts
    if limit < 0:
        return
    pos, key = 0, b""
    while pos < limit:
        try:
            shared, pos = _varint(body, pos)
            non_shared, pos = _varint(body, pos)
            value_len, pos = _varint(body, pos)
        except IndexError:
            return
        if pos + non_shared + value_len > len(body):
            return
        key = key[:shared] + body[pos:pos + non_shared]
        pos += non_shared
        yield key, body[pos:pos + value_len]
        pos += value_len


def _iter_table_records(data: bytes):
    """解析 .ldb/.sst 表文件；产出 (seq, user_key, kind, value)。损坏条目跳过。

    kind 1 为写入、0 为删除标记。删除标记不能在这里丢弃：同一键的旧写入
    是否仍有效取决于全库最新状态，需由调用方按键和序号统一还原。
    """
    if len(data) < 48 or int.from_bytes(data[-8:], "little") != _MAGIC:
        return
    pos = len(data) - 48
    try:
        _meta_off, pos = _varint(data, pos)
        _meta_size, pos = _varint(data, pos)
        index_off, pos = _varint(data, pos)
        index_size, pos = _varint(data, pos)
    except IndexError:
        return
    if index_off + index_size + 1 > len(data) - 8:
        return
    compression = data[index_off + index_size]
    index_body = data[index_off:index_off + index_size]
    if compression == 1:
        try:
            index_body = _snappy_decompress(index_body)
        except (ValueError, IndexError):
            return
    elif compression != 0:
        return
    for _key, handle in _block_entries(index_body):
        try:
            block_off, p2 = _varint(handle, 0)
            block_size, p2 = _varint(handle, p2)
        except IndexError:
            continue
        if block_off + block_size + 1 > len(data) - 8 or block_size <= 0:
            continue
        compression = data[block_off + block_size]
        block = data[block_off:block_off + block_size]
        if compression == 1:
            try:
                block = _snappy_decompress(block)
            except (ValueError, IndexError):
                continue
        elif compression != 0:
            continue
        for key, value in _block_entries(block):
            if len(key) < 8:
                continue
            tag = int.from_bytes(key[-8:], "little")
            kind = tag & 0xFF
            if kind not in (0, 1):  # 0 为删除标记，1 为写入
                continue
            yield tag >> 8, key[:-8], kind, value


def _iter_log_records(data: bytes):
    """解析 .log 预写日志；产出 (seq, user_key, kind, value)。

    日志文件按 32 KiB 分块；块尾放不下一个 7 字节记录头的剩余空间是填充，
    必须对齐到下一块边界，否则会把填充当作记录头读入并失去同步，
    导致后续块中的凭据记录被遗漏。
    """
    pos, fragment = 0, b""
    total = len(data)
    while pos < total:
        block_end = min((pos // _LOG_BLOCK_SIZE + 1) * _LOG_BLOCK_SIZE, total)
        if block_end - pos < 7:  # 块尾填充：跳到下一块边界
            pos = block_end
            continue
        length = int.from_bytes(data[pos + 4:pos + 6], "little")
        rtype = data[pos + 6]
        if pos + 7 + length > block_end:
            # 记录声明长度越过块边界，头部必然损坏；丢弃跨块片段并在块边界重新同步。
            fragment = b""
            pos = block_end
            continue
        payload = data[pos + 7:pos + 7 + length]
        pos += 7 + length
        if rtype == 0:
            continue
        if rtype == 1:  # FULL
            record = fragment + payload
            fragment = b""
        elif rtype == 2:  # FIRST
            fragment = payload
            continue
        elif rtype == 3:  # MIDDLE
            fragment += payload
            continue
        elif rtype == 4:  # LAST
            record = fragment + payload
            fragment = b""
        else:
            fragment = b""
            continue
        if len(record) < 12:
            continue
        base_seq = int.from_bytes(record[0:8], "little")
        count = int.from_bytes(record[8:12], "little")
        p2 = 12
        for index in range(count):
            try:
                kind = record[p2]
                p2 += 1
                key_len, p2 = _varint(record, p2)
                key = record[p2:p2 + key_len]
                p2 += key_len
                if kind not in (0, 1):  # 0 为删除标记，1 为写入
                    continue
                if kind == 1:
                    value_len, p2 = _varint(record, p2)
                    value = record[p2:p2 + value_len]
                    p2 += value_len
                else:
                    value = b""
            except IndexError:
                break
            yield base_seq + index, key, kind, value


def _value_candidates(raw: bytes):
    """Chrome 以标记字节区分编码，但实测存在标记与内容不一致的情况，按顺序给出候选。"""
    if raw[:1] == b"\x01":
        yield raw[1:].decode("utf-16-le", "ignore")
        yield raw[1:].decode("utf-8", "ignore")
    elif raw[:1] == b"\x00":
        yield raw[1:].decode("utf-8", "ignore")
        yield raw[1:].decode("utf-16-le", "ignore")
    else:
        yield raw.decode("utf-8", "ignore")
        yield raw.decode("utf-16-le", "ignore")


def _token_key_origin(key: bytes) -> str | None:
    """Chromium 本地存储键以 _<origin>\x00\x01 开头；解析并返回来源主机名。"""
    origin = key.split(b"\x00", 1)[0]
    if not origin.startswith(b"_"):
        return None
    try:
        parts = urlsplit(origin[1:].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname


def _origin_allowed(hostname: str) -> bool:
    """主机名必须恰好是平台域名或其子域；点边界匹配，拒绝同名前缀的第三方域名。"""
    return any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in _ALLOWED_ORIGIN_SUFFIXES
    )


def _credential_from_record(key: bytes, raw: bytes) -> Credentials | None:
    if _TOKEN_MARK not in key or not key.endswith(_TOKEN_SUFFIX):
        return None
    origin = _token_key_origin(key)
    if origin is None or not _origin_allowed(origin):
        return None
    for text in _value_candidates(raw):
        try:
            outer = json.loads(text)
            inner = json.loads(outer["value"]) if isinstance(outer, dict) and isinstance(outer.get("value"), str) else outer
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(inner, dict):
            continue
        token, mac_key, diff = inner.get("access_token"), inner.get("mac_key"), inner.get("diff")
        if isinstance(token, str) and token.strip() and isinstance(mac_key, str) and mac_key.strip() and type(diff) is int:
            try:
                return Credentials(token.strip(), mac_key, diff)
            except (TypeError, ValueError):
                return None
    return None


def _browser_bases() -> list[tuple[str, Path]]:
    """各平台 Chromium 系浏览器的用户数据根目录。"""
    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / "Application Support"
        return [
            ("Chrome", root / "Google" / "Chrome"),
            ("Chromium", root / "Chromium"),
            ("Edge", root / "Microsoft Edge"),
        ]
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [
            ("Chrome", local / "Google" / "Chrome" / "User Data"),
            ("Chromium", local / "Chromium" / "User Data"),
            ("Edge", local / "Microsoft" / "Edge" / "User Data"),
        ]
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [
        ("Chrome", config / "google-chrome"),
        ("Chromium", config / "chromium"),
        ("Edge", config / "microsoft-edge"),
    ]


def _is_token_key(key: bytes) -> bool:
    """键名结构上是否为平台凭据记录；用于在还原最新状态前缩小保留范围。"""
    return _TOKEN_MARK in key and key.endswith(_TOKEN_SUFFIX)


def group_candidates(candidates: list[BrowserCredential]) -> list[list[BrowserCredential]]:
    """按浏览器与配置分组；组内按写入序号倒序，跨组顺序不代表写入先后。

    LevelDB 的序号只在同一个数据库内有意义，不能跨浏览器或配置比较，
    因此跨组的“最新凭据”无法判断，应让用户选择来源。
    """
    groups: dict[tuple[str, str], list[BrowserCredential]] = {}
    for item in candidates:
        groups.setdefault((item.browser, item.profile), []).append(item)
    return [sorted(group, key=lambda item: item.sequence, reverse=True) for _, group in sorted(groups.items())]


def import_from_browser() -> list[BrowserCredential]:
    """扫描本机 Chromium 系浏览器的本地存储，返回平台凭据候选。

    候选按浏览器配置分组（见 group_candidates）：同一配置内按写入序号倒序；
    不同配置之间的顺序不代表写入先后，多个来源时应让用户选择。
    """
    candidates: list[BrowserCredential] = []
    scanned = False
    for browser, base in _browser_bases():
        if not base.is_dir():
            continue
        for profile_dir in sorted(base.iterdir()):
            leveldb = profile_dir / "Local Storage" / "leveldb"
            if not leveldb.is_dir():
                continue
            scanned = True
            # 先按数据库、键和序号还原每个键的最新状态，再提取凭据；
            # 这样先写入后删除的记录不会把旧凭据带回结果。
            latest: dict[bytes, tuple[int, int, bytes]] = {}
            for path in sorted(leveldb.iterdir()):
                suffix = path.suffix.lower()
                if suffix not in (".ldb", ".sst", ".log") or not path.is_file():
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                records = _iter_table_records(data) if suffix != ".log" else _iter_log_records(data)
                for sequence, key, kind, value in records:
                    if not _is_token_key(key):
                        continue
                    if len(value) > _MAX_VALUE_BYTES:
                        value = b""  # 过大内容不可能解析为凭据，但仍作为最新状态阻止旧记录复活
                    previous = latest.get(key)
                    if previous is None or sequence > previous[0]:
                        latest[key] = (sequence, kind, value)
            for key, (sequence, kind, value) in latest.items():
                if kind != 1 or not value:
                    continue  # 最新状态是删除标记，或没有可用值
                credentials = _credential_from_record(key, value)
                if credentials is not None:
                    candidates.append(BrowserCredential(credentials, browser, profile_dir.name, sequence))
    if not scanned:
        raise AppError(
            "CREDENTIAL_STORE_ERROR",
            "未找到本机 Chrome/Chromium/Edge 的本地存储目录；请确认浏览器已安装。",
        )
    # 保持扁平列表；同一配置的候选相邻且按序号倒序，跨配置顺序需调用方用
    # group_candidates 分组处理，不得直接取 candidates[0] 宣称“最新”。
    return [item for group in group_candidates(candidates) for item in group]
