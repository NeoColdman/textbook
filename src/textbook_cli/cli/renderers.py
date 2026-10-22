"""Presentation only: core services never print terminal output."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn, TransferSpeedColumn
from rich.text import Text

from textbook_cli.core.errors import redact

from .theme import TERMINAL_THEME


PHASES = {
    "queued": "排队中",
    "resolving": "解析中",
    "connecting": "连接中",
    "downloading": "下载中",
    "verifying": "校验中",
    "bookmarking": "写入书签",
    "committing": "保存中",
    "completed": "已完成",
    "skipped": "已验证并跳过",
    "failed": "失败",
    "cancelled": "已取消",
}


def plain(value: Any) -> Any:
    """Convert known public result objects to JSON-compatible values."""
    if hasattr(value, "to_dict"):
        return plain(value.to_dict())
    if dataclasses.is_dataclass(value):
        return plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def console(*, stderr: bool = False) -> Console:
    stream = sys.stderr if stderr else sys.stdout
    terminal = bool(getattr(stream, "isatty", lambda: False)())
    return Console(
        file=stream,
        no_color="NO_COLOR" in os.environ or not terminal,
        force_terminal=terminal and "NO_COLOR" not in os.environ,
        markup=False,
        highlight=False,
        theme=TERMINAL_THEME,
    )


def envelope(command: str, data: Any = None, *, code: int = 0, warnings: list[str] | None = None, error: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "command": command,
        "ok": code == 0,
        "data": plain(data),
        "warnings": list(warnings or []),
        "error": plain(error),
    }


def emit_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
    sys.stdout.flush()


def warn(message: str) -> None:
    text = Text("! 提示：", style="warning")
    text.append(message, style="default")
    console(stderr=True).print(text)


def show_error(message: str) -> None:
    text = Text("× 错误：", style="error")
    text.append(message, style="default")
    console(stderr=True).print(text)


def download_warnings(result: Any, credentials=None, initial=None) -> list[str]:
    """Keep per-file degradations visible without exposing active credentials."""
    messages = list(initial or []) + list(result.warnings)
    for item in result.results:
        messages.extend(item.warnings)
    return list(dict.fromkeys(redact(str(message), credentials) for message in messages))


def download_data(result: Any, credentials=None) -> dict:
    """Apply a final secret filter to every public download result string."""
    def sanitize(value):
        if isinstance(value, str):
            return redact(value, credentials)
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return sanitize(plain(result.to_dict()))


def show_search(data: dict) -> None:
    out = console()
    catalog = data.get("catalog", {})
    suffix = "（旧缓存）" if catalog.get("stale") else ""
    total, offset, items = data["total"], data["offset"], data["items"]
    heading = Text("● 搜索结果", style="accent bold")
    heading.append(f"  共 {total} 本；本页 {offset + 1 if items else 0}–{offset + len(items)}", style="default")
    out.print(heading)
    out.print(Text(f"  目录更新：{catalog.get('updated_at') or '未知'}；最近检查：{catalog.get('checked_at') or '未知'}{suffix}", style="muted"))
    if not items:
        out.print(Text("  没有匹配的教材，可缩短关键词或调整筛选条件。", style="muted"))
        return
    for index, item in enumerate(items, offset + 1):
        out.print()
        title = Text(f"  {index}. ", style="accent")
        title.append(item["title"], style="bold")
        out.print(title)
        path = " / ".join(item.get("category_path") or [])
        if path:
            out.print(Text("     " + path, style="muted"))
        out.print(Text("     资源 ID  " + item["resource_id"], style="muted"))


def show_download(data: dict) -> None:
    out = console()
    summary = data.get("summary", {})
    out.print(Text("● 下载结果", style="accent bold"))
    counts = Text("  ")
    for index, (key, label, style) in enumerate((
        ("completed", "完成", "success"),
        ("skipped", "跳过", "muted"),
        ("failed", "失败", "error"),
        ("cancelled", "取消", "warning"),
    )):
        if index:
            counts.append(" · ", style="muted")
        counts.append(f"{label} {summary.get(key, 0)}", style=style if summary.get(key) else "muted")
    out.print(counts)
    statuses = {
        "completed": ("✓", "success"),
        "skipped": ("↳", "muted"),
        "failed": ("×", "error"),
        "cancelled": ("–", "warning"),
    }
    for item in data.get("results", []):
        error = item.get("error")
        detail = (error.get("message", "") if error else None) or item.get("path") or "—"
        status = item.get("status", "未知")
        marker, style = statuses.get(status, ("·", "muted"))
        line = Text(f"  {marker} {PHASES.get(status, status)}", style=style)
        line.append("  " + (item.get("resource_id") or "—"), style="default")
        out.print(line)
        out.print(Text("    " + str(detail), style="error" if error else "muted"))


class ProgressRenderer(AbstractContextManager):
    """Consume events from the coordinator; Rich handles thread-safe updates."""

    def __init__(self) -> None:
        self._console = console(stderr=True)
        self._dynamic = bool(getattr(sys.stderr, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ
        self._progress = Progress(
            SpinnerColumn("dots", style="accent", finished_text="✓"),
            TextColumn("{task.description}", markup=False),
            BarColumn(bar_width=20, style="muted", complete_style="accent", finished_style="success", pulse_style="accent"),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=self._console,
            disable=not self._dynamic,
        )
        self._tasks: dict[str, int] = {}
        self._last: dict[str, tuple[str, int]] = {}

    def __enter__(self):
        self._progress.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._progress.stop()
        return False

    def __call__(self, event: Any) -> None:
        value = plain(event)
        if not isinstance(value, dict):
            return
        key = str(value.get("task_id") or f"{value.get('resource_id', '')}:{value.get('artifact_id', '')}")
        phase = str(value.get("phase") or value.get("stage") or value.get("status") or "queued")
        attempt = int(value.get("attempt") or 1)
        label = str(value.get("title") or value.get("resource_id") or key)
        description = f"{label} · {PHASES.get(phase, phase)}"
        if attempt > 1:
            description += f" · 第 {attempt} 次"
        if self._dynamic:
            if key not in self._tasks:
                self._tasks[key] = self._progress.add_task(description, total=value.get("total"))
            total = value.get("total", value.get("total_size"))
            received = value.get("downloaded", value.get("received", value.get("downloaded_size", value.get("bytes", 0))))
            self._progress.update(self._tasks[key], description=description, total=total, completed=received or 0)
            if phase in {"completed", "skipped", "failed", "cancelled"}:
                self._progress.stop_task(self._tasks[key])
        elif self._last.get(key) != (phase, attempt):
            self._console.print(Text("  · " + description))
        self._last[key] = (phase, attempt)
