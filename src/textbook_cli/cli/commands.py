"""Scriptable commands with one JSON object per invocation."""

from __future__ import annotations

import getpass
import os
import signal
import sys
import threading
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import typer
from typer.core import TyperGroup

try:  # Typer 0.27 embeds its parser; older supported versions use Click.
    from typer import _click as click
except ImportError:
    import click

try:  # Typer keeps Abort in its own public exceptions module across versions.
    from typer.exceptions import Abort
except ImportError:  # pragma: no cover - very old Typer re-exports it via Click.
    from click import Abort

from textbook_cli import __version__
from textbook_cli.core.catalog import CatalogService
from textbook_cli.core.downloads import DownloadService
from textbook_cli.core.errors import AppError, redact
from textbook_cli.providers.smartedu.resolver import SmartEduProvider, normalize_input
from textbook_cli.storage.browser_import import group_candidates, import_from_browser
from textbook_cli.storage.credentials import CredentialStore, parse_credentials, read_credentials_file
from textbook_cli.storage.paths import AppPaths

from .renderers import ProgressRenderer, console, download_data, download_warnings, emit_json, envelope, plain, show_download, show_error, show_search, warn


def is_interactive() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)()) and bool(getattr(sys.stderr, "isatty", lambda: False)())


@dataclass
class Runtime:
    provider: object
    catalog: object
    credentials: object
    paths: AppPaths
    downloader_factory: Callable = DownloadService

    @classmethod
    def default(cls) -> Runtime:
        paths = AppPaths.default()
        provider = SmartEduProvider()
        return cls(provider, CatalogService(provider=provider, cache_path=paths.cache_file), CredentialStore(), paths)


runtime_factory: Callable[[], Runtime] = Runtime.default


def _json_context(args: list[str]) -> tuple[bool, str]:
    command = next((arg for arg in args if arg in {"search", "download"}), "")
    return "--json" in args and bool(command) and not any(arg in {"--help", "-h", "--version"} for arg in args), command


class TextbookGroup(TyperGroup):
    """Catch parser failures before callbacks, including JSON usage failures."""

    def main(self, args=None, prog_name=None, complete_var=None, standalone_mode=True, **extra):
        arguments = list(sys.argv[1:] if args is None else args)
        json_mode, command = _json_context(arguments)
        try:
            result = super().main(args=arguments, prog_name=prog_name, complete_var=complete_var, standalone_mode=False, **extra)
            code = result if isinstance(result, int) else 0
        except click.ClickException as exc:
            code = exc.exit_code
            message = redact(exc.format_message())
            if json_mode:
                emit_json(envelope(command, code=code, error={"code": "INVALID_INPUT", "message": message}))
                show_error(message)
            else:
                show_error(message)
        except AppError as exc:
            code = exc.exit_code
            message = redact(str(exc))
            if json_mode:
                emit_json(envelope(command, code=code, error={"code": exc.code, "message": message}))
            show_error(message)
        except (KeyboardInterrupt, Abort):
            code = 130
            if json_mode:
                emit_json(envelope(command, code=code, error={"code": "CANCELLED", "message": "操作已取消。"}))
            show_error("操作已取消。")
        except Exception as exc:
            code = 1
            # Arbitrary exception text may contain secrets, URLs or user data.
            message = f"操作失败（{type(exc).__name__}），请检查本地环境和输出目录。"
            if json_mode:
                emit_json(envelope(command, code=code, error={"code": "INTERNAL_ERROR", "message": message}))
            show_error(message)
        if standalone_mode:
            raise SystemExit(code)
        return code


app = typer.Typer(cls=TextbookGroup, invoke_without_command=True, no_args_is_help=False, help="搜索、选择和下载国家中小学智慧教育平台电子教材。", context_settings={"help_option_names": ["-h", "--help"]}, pretty_exceptions_enable=False)
catalog_app = typer.Typer(help="管理本地教材目录缓存。", no_args_is_help=True)
auth_app = typer.Typer(help="管理用户自行取得的登录凭据。", no_args_is_help=True)
app.add_typer(catalog_app, name="catalog")
app.add_typer(auth_app, name="auth")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"textbook {__version__}")
        raise typer.Exit(0)


@app.callback()
def root(ctx: typer.Context, version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="显示版本。")) -> None:
    ctx.obj = runtime_factory()
    if ctx.invoked_subcommand is None:
        if not is_interactive():
            # Recent Typer versions render help while get_help() is called.
            with redirect_stdout(sys.stderr):
                help_text = ctx.get_help()
            if help_text:
                typer.echo(help_text, err=True)
            raise typer.Exit(2)
        from .interactive import run_wizard

        raise typer.Exit(run_wizard(ctx.obj))


@app.command()
def browse(ctx: typer.Context, query: str = typer.Option("", "--query", help="从指定搜索结果开始。")) -> None:
    """进入分类浏览和多选向导。"""
    if not is_interactive():
        raise AppError("INVALID_INPUT", "browse 需要交互终端；脚本请使用 search 或 download。", exit_code=2)
    from .interactive import run_wizard

    raise typer.Exit(run_wizard(ctx.obj, initial_query=query, start_browse=True))


@app.command()
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="按空格分隔的全部关键词。"),
    stage: Optional[str] = typer.Option(None, "--stage", help="完整学段名称。"),
    subject: Optional[str] = typer.Option(None, "--subject", help="完整学科名称。"),
    edition: Optional[str] = typer.Option(None, "--edition", help="完整版本名称。"),
    grade: Optional[str] = typer.Option(None, "--grade", help="完整年级名称。"),
    limit: int = typer.Option(20, "--limit", min=1, max=1000),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出一个 JSON 结果对象。"),
) -> None:
    """搜索教材名称与分类路径。"""
    service = ctx.obj.catalog
    data = service.search(query, stage=stage, subject=subject, edition=edition, grade=grade, limit=limit, offset=offset)
    warnings = list(getattr(service, "last_warnings", []) or [])
    for message in warnings:
        warn(redact(message))
    if json_output:
        emit_json(envelope("search", data, warnings=warnings))
    else:
        show_search(plain(data))


def _read_inputs(path: str) -> list[tuple[str, str]]:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise AppError("INVALID_INPUT", "无法读取教材输入文件，请确认路径和 UTF-8 编码。", exit_code=2) from None
    return [(line.strip(), f"输入文件第 {index} 行") for index, line in enumerate(text.splitlines(), 1) if line.strip()]


def prepare_inputs(runtime: Runtime, urls: list[str], ids: list[str], input_file: str | None) -> tuple[list[str], dict]:
    """Validate the entire queue before any network or file publishing occurs."""
    values = [(url, "页面链接") for url in urls] + [(identifier, "资源 ID") for identifier in ids]
    for url in urls:
        if not url.startswith("https://"):
            raise AppError("INVALID_INPUT", "位置参数必须是平台教材页面 HTTPS 链接；资源 ID 请使用 --id。", exit_code=2)
    if input_file is not None:
        values.extend(_read_inputs(input_file))
    if not values:
        raise AppError("INVALID_INPUT", "请提供教材页面链接、--id 或 --input。", exit_code=2)
    cached = runtime.catalog.cached() if hasattr(runtime.catalog, "cached") else getattr(runtime.catalog, "snapshot", None)
    books = {book.resource_id: book for book in getattr(cached, "books", [])}
    aliases: dict[str, set[str]] = {}
    for book in books.values():
        if book.catalog_id:
            aliases.setdefault(book.catalog_id, set()).add(book.resource_id)
    normalized = []
    for value, location in values:
        try:
            identifier = normalize_input(value)
        except AppError:
            raise AppError("INVALID_INPUT", f"{location}不是受支持的教材链接或资源 ID。", exit_code=2) from None
        if identifier in aliases:
            candidates = aliases[identifier] | ({identifier} if identifier in books else set())
            if len(candidates) != 1:
                raise AppError("INVALID_INPUT", f"{location}存在资源 ID 别名冲突，请使用规范页面链接。", exit_code=2)
            identifier = next(iter(candidates))
        if identifier not in normalized:
            normalized.append(identifier)
    return normalized, books


@contextmanager
def cancellation_signals(cancel: threading.Event):
    previous = None
    enabled = threading.current_thread() is threading.main_thread()
    if enabled:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda signum, frame: cancel.set())
    try:
        yield
    finally:
        if enabled:
            signal.signal(signal.SIGINT, previous)


def saved_credentials(runtime: Runtime):
    try:
        return runtime.credentials.load(), []
    except AppError:
        return None, ["系统凭据库不可用；本次先尝试公开资源。需要授权时请用 --credentials-file 或向导中的临时凭据。"]


def execute_download(runtime: Runtime, identifiers: list[str], *, output: Path, bookmarks: bool = False, jobs: int = 2, overwrite: bool = False, credentials=None, books: dict | None = None):
    cancel = threading.Event()
    with ProgressRenderer() as renderer, cancellation_signals(cancel):
        service = runtime.downloader_factory(provider=runtime.provider, credentials=credentials, event_callback=renderer)
        return service.run(identifiers, output=output, bookmarks=bookmarks, jobs=jobs, overwrite=overwrite, cancel=cancel, books=books)


@app.command()
def download(
    ctx: typer.Context,
    urls: Optional[List[str]] = typer.Argument(None, help="教材预览页面 HTTPS 链接，可提供多个。"),
    ids: Optional[List[str]] = typer.Option(None, "--id", help="资源 ID，可重复。"),
    input_file: Optional[str] = typer.Option(None, "--input", help="UTF-8 教材清单；- 表示标准输入。"),
    output: Path = typer.Option(Path("textbooks"), "--output", "-o", help="输出根目录。"),
    bookmarks: bool = typer.Option(False, "--bookmarks", help="为 PDF 添加平台章节书签。"),
    jobs: int = typer.Option(2, "--jobs", min=1, max=3, help="同时下载文件数。"),
    overwrite: bool = typer.Option(False, "--overwrite", help="授权重新下载并替换命中目标。"),
    credentials_file: Optional[Path] = typer.Option(None, "--credentials-file", help="仅本次使用的三字段凭据 JSON 文件。"),
    json_output: bool = typer.Option(False, "--json", help="输出一个 JSON 结果对象。"),
) -> None:
    """校验并批量下载选定教材 PDF。"""
    runtime = ctx.obj
    identifiers, books = prepare_inputs(runtime, list(urls or []), list(ids or []), input_file)
    if credentials_file is not None:
        if str(credentials_file) == "-":
            raise AppError("INVALID_INPUT", "--credentials-file 不接受标准输入，请提供凭据文件路径。", exit_code=2)
        credentials, warnings = read_credentials_file(credentials_file), []
    else:
        credentials, warnings = saved_credentials(runtime)
    for message in warnings:
        warn(message)
    result = execute_download(runtime, identifiers, output=output, bookmarks=bookmarks, jobs=jobs, overwrite=overwrite, credentials=credentials, books=books)
    code = result.exit_code
    warnings = download_warnings(result, credentials, initial=warnings)
    data = download_data(result, credentials)
    for message in warnings:
        warn(message)
    errors = {1: ("DOWNLOAD_FAILED", "下载未完成，请查看逐项结果。"), 3: ("AUTH_REJECTED", "所选资源需要有效凭据或访问权限。"), 4: ("PARTIAL_FAILURE", "部分教材下载失败，请查看逐项结果。"), 130: ("CANCELLED", "下载已取消，已完成的文件已保留。")}
    error = dict(zip(("code", "message"), errors[code])) if code in errors else None
    if json_output:
        emit_json(envelope("download", data, code=code, warnings=warnings, error=error))
    else:
        show_download(data)
    raise typer.Exit(code)


@catalog_app.command("refresh")
def catalog_refresh(ctx: typer.Context) -> None:
    """完整检查和刷新目录，失败时保留旧缓存。"""
    snapshot = ctx.obj.catalog.load(refresh=True)
    for message in getattr(snapshot, "warnings", []):
        warn(redact(message))
    console().print(f"目录刷新完成：{len(snapshot.books)} 本；最近检查 {snapshot.checked_at}")


@catalog_app.command("status")
def catalog_status(ctx: typer.Context) -> None:
    """显示缓存状态，不联网。"""
    _show_mapping(ctx.obj.catalog.status())


def _show_mapping(data: dict) -> None:
    for key, value in data.items():
        console().print(f"{key}: {value}")


@auth_app.command("import")
def auth_import(ctx: typer.Context, file: Optional[str] = typer.Option(None, "--file", help="UTF-8 JSON 文件；- 表示标准输入。")) -> None:
    """导入用户自行取得的凭据并保存到系统凭据库。"""
    if file is not None:
        if file == "-":
            credentials = parse_credentials(sys.stdin.read())
        else:
            credentials = read_credentials_file(Path(file))
    else:
        if not is_interactive():
            raise AppError("INVALID_INPUT", "非交互环境请通过 auth import --file 提供凭据文件。", exit_code=2)
        credentials = parse_credentials(getpass.getpass("请粘贴三字段凭据 JSON（输入隐藏）：", stream=sys.stderr))
    ctx.obj.credentials.save(credentials)
    console().print("登录凭据已保存到系统凭据库。凭据是否有效将在实际访问时确认。")


def _choose_browser_candidate(groups: list) -> object:
    """多个浏览器配置都发现凭据时让用户选择来源；序号跨配置不可比。"""
    console().print("在多个浏览器配置中发现平台凭据，无法判断写入先后，请选择来源：")
    for index, group in enumerate(groups, 1):
        newest = group[0]
        console().print(f"  {index}. {newest.browser}（{newest.profile} 配置）")
    choices = {str(index): group[0] for index, group in enumerate(groups, 1)}
    while True:
        answer = typer.prompt("来源编号").strip()
        if answer in choices:
            return choices[answer]
        console().print("请输入列表中的编号。")


@auth_app.command("grab")
def auth_grab(ctx: typer.Context) -> None:
    """从本机浏览器本地存储自动提取平台凭据并保存到系统凭据库。"""
    candidates = import_from_browser()
    if not candidates:
        raise AppError(
            "CREDENTIAL_STORE_ERROR",
            "未能在本机 Chrome/Chromium/Edge 本地存储中找到平台凭据记录；请先在浏览器登录平台，或使用 auth import 手动导入。",
        )
    # 写入序号只在同一浏览器配置的数据库内有序，跨配置不能比较；
    # 多个配置都有凭据时不做猜测，让用户选择来源。
    groups = group_candidates(candidates)
    if len(groups) == 1:
        newest = groups[0][0]
        note = f"本配置共 {len(groups[0])} 条记录，已采用写入时间最新的一组。" if len(groups[0]) > 1 else ""
    elif is_interactive():
        newest = _choose_browser_candidate(groups)
        note = ""
    else:
        raise AppError(
            "CREDENTIAL_STORE_ERROR",
            "在多个浏览器配置中发现平台凭据，且无法判断写入先后；"
            "请在终端中重新运行 auth grab 以选择来源，或使用 auth import 手动导入。",
        )
    ctx.obj.credentials.save(newest.credentials)
    console().print(f"已从 {newest.browser}（{newest.profile} 配置）导入凭据并保存到系统凭据库。")
    if note:
        console().print(note)


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """报告本地凭据状态，不验证平台有效性。"""
    _show_mapping(ctx.obj.credentials.status())


@auth_app.command("clear")
def auth_clear(ctx: typer.Context) -> None:
    """清除本项目保存的凭据。"""
    ctx.obj.credentials.clear()
    console().print("本项目保存的登录凭据已清除。")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """仅检查本地环境，不联网、不输出秘密。"""
    runtime = ctx.obj
    data = {"version": __version__, "config_dir": str(runtime.paths.config_dir), "cache_dir": str(runtime.paths.cache_dir)}
    failed = False
    for key, path in (("config_writable", runtime.paths.config_dir), ("cache_writable", runtime.paths.cache_dir), ("output_writable", Path.cwd() / "textbooks")):
        ancestor = Path(path)
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        writable = ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK)
        data[key] = writable
        failed |= not writable
    # Backends are inspected without loading credential values.
    if hasattr(runtime.credentials, "backend_status"):
        data["credential_backend"] = runtime.credentials.backend_status()
    else:
        data["credential_backend"] = "使用 auth status 查看系统凭据库状态"
    data["catalog"] = runtime.catalog.status()
    _show_mapping(data)
    raise typer.Exit(1 if failed else 0)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
