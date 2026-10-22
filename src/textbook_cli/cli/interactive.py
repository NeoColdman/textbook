"""Keyboard driven book selection, with a plain terminal fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, create_app_session
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import HSplit, Layout, ScrollOffsets, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import create_output
from rich import box
from rich.panel import Panel
from rich.text import Text

from textbook_cli import __version__
from textbook_cli.core.errors import AppError, redact
from textbook_cli.storage.browser_import import group_candidates, import_from_browser
from textbook_cli.storage.credentials import parse_credentials

from .prompts import PROMPT_STYLE, button_dialog, command_prompt, input_dialog, message_dialog, radiolist_dialog
from .renderers import console, download_data, download_warnings, show_download, warn

if TYPE_CHECKING:
    from .commands import Runtime


@dataclass
class Selection:
    selected: set[str] = field(default_factory=set)

    def toggle(self, identifiers: set[str]) -> None:
        if identifiers and identifiers <= self.selected:
            self.selected.difference_update(identifiers)
        else:
            self.selected.update(identifiers)

    def marker(self, identifiers: set[str]) -> str:
        count = len(identifiers & self.selected)
        return "[x]" if count and count == len(identifiers) else "[-]" if count else "[ ]"


@dataclass
class Row:
    label: str
    identifiers: set[str]
    category: tuple[str, ...] | None = None
    title: str = ""
    detail: str = ""


@dataclass
class WizardState:
    selection: Selection = field(default_factory=Selection)
    query: str = ""
    prefix: tuple[str, ...] = ()
    output: Path = field(default_factory=lambda: Path.cwd() / "textbooks")
    bookmarks: bool = False
    credentials: object = None
    credential_prompted: bool = False


def _label(text) -> str:
    return " ".join(str(text).split())


def category_rows(books: list, prefix: tuple[str, ...] = (), *, flat: bool = False) -> list[Row]:
    """Build rows from real paths; empty categories can never become books."""
    branches: dict[str, set[str]] = {}
    leaves: dict[str, object] = {}
    for book in books:
        if flat:
            leaves[book.resource_id] = book
            continue
        paths = book.category_paths or ((),)
        for path in paths:
            path = tuple(path)
            if path[:len(prefix)] != prefix:
                continue
            if len(path) > len(prefix):
                branches.setdefault(path[len(prefix)], set()).add(book.resource_id)
            else:
                leaves[book.resource_id] = book
    result = [Row(_label(name) + f" /（{len(ids)} 本）", ids, prefix + (name,)) for name, ids in sorted(branches.items(), key=lambda item: (item[0].casefold(), item[0]))]
    for book in sorted(leaves.values(), key=lambda item: (item.title.casefold(), item.resource_id)):
        path = " / ".join(book.primary_category_path)
        suffix = f" · {path}" if flat and path else ""
        title = _label(book.title)
        detail = f"{book.resource_id}" + (f" · {path}" if path else "")
        result.append(Row(f"{title} · {book.resource_id}{suffix}", {book.resource_id},
                          title=title, detail=detail))
    return result


def _dialog(application):
    keys = KeyBindings()

    @keys.add("c-c")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    @keys.add("escape")
    def back(event):
        event.app.exit(result=None)

    application.key_bindings = merge_key_bindings([application.key_bindings, keys])
    return application.run()


def _message(title: str, message: str) -> None:
    _dialog(message_dialog(title=title, text=message, ok_text="返回"))


def _multiline(title: str, *, secret: bool = False, description: str = "") -> str | None:
    return _dialog(input_dialog(title=title, text=description, password=secret, multiline=True))


def _matching_books(runtime: Runtime, snapshot, query: str) -> list:
    if not query.strip():
        return list(snapshot.books)
    identifiers = set()
    offset = 0
    while True:
        page = runtime.catalog.search(query, limit=1000, offset=offset)
        identifiers.update(item["resource_id"] for item in page["items"])
        offset += len(page["items"])
        if offset >= page["total"] or not page["items"]:
            break
    return [book for book in snapshot.books if book.resource_id in identifiers]


def header_text(state: WizardState, rows: list[Row], snapshot, *, selected_only: bool = False) -> str:
    location = " / ".join(state.prefix) or "所有分类"
    if selected_only:
        location = "全部已选教材（u 清空全部）"
    fetched = getattr(snapshot, "fetched_at", "未知")
    stale = " · 旧缓存" if getattr(snapshot, "stale", False) else ""
    visible = set().union(*(row.identifiers for row in rows)) if rows else set()
    visible_selected = len(visible & state.selection.selected)
    total = len(state.selection.selected)
    counts = f"已选 {total} 本"
    if not selected_only and visible_selected != total:
        counts += f"（当前结果 {visible_selected} 本）"
    return f"{location} · 搜索：{state.query or '无'} · {counts}\n目录更新：{fetched}{stale}"


def _chooser(books: list, state: WizardState, *, snapshot=None, selected_only=False) -> str:
    rows = category_rows(books, state.prefix, flat=selected_only)
    cursor = 0
    keys = KeyBindings()

    def fragments():
        if not rows:
            return [("class:muted", "  没有匹配的教材。按 / 修改搜索，或 Esc 返回。")]
        text = []
        for index, row in enumerate(rows):
            active = index == cursor
            marker = state.selection.marker(row.identifiers)
            text.append(("class:accent", "❯ " if active else "  "))
            text.append(("class:marker" if marker != "[ ]" else "class:muted", f"{marker} "))
            text.append(("class:selected" if active else "", row.title or row.label))
            if row.detail:
                text.append(("class:muted", f"\n      {row.detail}\n"))
            else:
                text.append(("class:muted", "  ›\n" if row.category is not None else "\n"))
        return text

    def header():
        context, _, metadata = header_text(state, rows, snapshot, selected_only=selected_only).partition("\n")
        return [("", context + "\n"), ("class:muted", metadata)]

    @keys.add("up")
    def up(event):
        nonlocal cursor
        cursor = max(0, cursor - 1)

    @keys.add("down")
    def down(event):
        nonlocal cursor
        cursor = min(max(0, len(rows) - 1), cursor + 1)

    @keys.add("pageup")
    def page_up(event):
        nonlocal cursor
        cursor = max(0, cursor - 15)

    @keys.add("pagedown")
    def page_down(event):
        nonlocal cursor
        cursor = min(max(0, len(rows) - 1), cursor + 15)

    @keys.add(" ")
    def toggle(event):
        if rows:
            state.selection.toggle(rows[cursor].identifiers)

    @keys.add("a")
    def all_visible(event):
        state.selection.selected.update(set().union(*(row.identifiers for row in rows)))

    @keys.add("u")
    def clear_visible(event):
        state.selection.selected.difference_update(set().union(*(row.identifiers for row in rows)))

    @keys.add("enter")
    def enter(event):
        if not rows:
            return
        row = rows[cursor]
        if row.category is not None:
            state.prefix = row.category
            event.app.exit(result="level")
        else:
            if not state.selection.selected:
                state.selection.selected.update(row.identifiers)
            event.app.exit(result="download")

    @keys.add("escape")
    @keys.add("b")
    def back(event):
        if state.prefix and not selected_only:
            state.prefix = state.prefix[:-1]
            event.app.exit(result="level")
        else:
            event.app.exit(result="back")

    @keys.add("/")
    def search(event):
        event.app.exit(result="search")

    @keys.add("v")
    def selected(event):
        event.app.exit(result="selected")

    @keys.add("d")
    def download(event):
        event.app.exit(result="download")

    @keys.add("q")
    def quit_selection(event):
        event.app.exit(result="back")

    @keys.add("c-c")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    control = FormattedTextControl(fragments, focusable=True, show_cursor=False,
                                  get_cursor_position=lambda: Point(x=0, y=sum(2 if row.detail else 1 for row in rows[:cursor])))
    footer = "↑↓ 移动 · 空格 勾选 · Enter 进入/继续 · / 搜索 · d 下载\na 全选当前 · u 清空当前 · v 全部已选 · Esc 返回 · Ctrl+C 退出"
    layout = Layout(HSplit([
        Window(FormattedTextControl([("class:accent", "❯ "), ("class:title", "全部已选" if selected_only else "浏览教材")]), height=1),
        Window(height=1),
        Window(FormattedTextControl(header), wrap_lines=True, dont_extend_height=True),
        Window(height=1, char="─", style="class:rule"),
        Window(control, wrap_lines=True, height=Dimension(min=2, max=14),
               dont_extend_height=True, scroll_offsets=ScrollOffsets(top=1, bottom=1)),
        Window(height=1, char="─", style="class:rule"),
        Window(FormattedTextControl([("class:muted", footer)]), wrap_lines=True, dont_extend_height=True),
    ]), focused_element=control)
    app = Application(layout=layout, key_bindings=keys, full_screen=False, erase_when_done=True,
                      mouse_support=False, style=PROMPT_STYLE)
    return app.run()


_BROWSER_LOGIN_HINT = (
    "获取凭据的步骤：\n"
    "1. 在浏览器打开 basic.smartedu.cn 并登录你的账号；\n"
    "2. 按 F12 打开开发者工具，切换到控制台（Console）；\n"
    "3. 运行上游 README 提供的凭据提取脚本，复制输出的整段 JSON；\n"
    "4. 回到本工具粘贴。凭据只保存在本机钥匙串或本次内存中，不会上传。"
)


def _load_saved_credentials(runtime) -> tuple[object | None, bool]:
    """读取系统凭据库中已保存的凭据；返回 (凭据, 后端是否可用)，不抛异常。"""
    try:
        return runtime.credentials.load(), True
    except AppError:
        return None, False


def _credential_status_line(runtime, state) -> str:
    """主菜单显示的凭据状态摘要，不包含任何秘密值。"""
    if state.credentials is not None:
        return "本次使用"
    saved, backend_ok = _load_saved_credentials(runtime)
    if not backend_ok:
        return "后端不可用"
    return "已保存" if saved is not None else "未导入"


def _accept_credentials(runtime, state, credentials, *, persist: bool) -> str | None:
    """载入凭据；持久保存失败时回退为仅本次使用。返回给用户的提示或 None。"""
    if not persist:
        state.credentials = credentials
        return "凭据已载入，仅本次使用；不会写入配置、清单或日志。"
    try:
        runtime.credentials.save(credentials)
    except AppError as exc:
        fallback = _dialog(button_dialog(title="未保存凭据", text=redact(str(exc)) + "\n可以明确选择仅在本次交互中使用。", buttons=[("仅本次使用", True), ("返回", False)]))
        if not fallback:
            return None
        state.credentials = credentials
        return "系统凭据库不可用；凭据仅本次使用，不会明文落盘。"
    state.credentials = credentials
    return "登录凭据已保存到系统凭据库。凭据是否有效将在实际下载时确认。"


def _prompt_credentials(runtime, state, *, persist: bool) -> None:
    """粘贴导入流程：解析失败显示原因与浏览器登录指引，可重新打开凭据菜单重试。"""
    raw = _multiline("导入登录凭据", secret=True,
                     description="粘贴包含 access_token、mac_key、diff 的 JSON（输入隐藏）。\n\n" + _BROWSER_LOGIN_HINT)
    if raw is None:
        return
    try:
        credentials = parse_credentials(raw)
    except AppError as exc:
        _message("凭据格式无效", redact(str(exc), state.credentials) + "\n\n" + _BROWSER_LOGIN_HINT)
        return
    message = _accept_credentials(runtime, state, credentials, persist=persist)
    if message:
        _message("凭据已载入", message)


def _choose_browser_source(groups: list):
    """多个浏览器配置都发现凭据时选择来源；写入序号跨配置不可比。"""
    values = [(group[0], f"{group[0].browser}（{group[0].profile} 配置）") for group in groups]
    return _dialog(radiolist_dialog(
        title="选择凭据来源",
        text="多个浏览器配置中都发现平台凭据，无法判断哪一组写入更晚，请选择要采用的来源。",
        values=values, ok_text="采用", cancel_text="返回",
    ))


def _prompt_browser_import(runtime, state, *, persist: bool) -> None:
    """扫描本机浏览器本地存储；成功后走统一载入流程，失败给出浏览器登录指引。"""
    try:
        candidates = import_from_browser()
    except AppError as exc:
        _message("未能自动导入", redact(str(exc), state.credentials) + "\n\n" + _BROWSER_LOGIN_HINT)
        return
    if not candidates:
        _message("未能自动导入", "未能在本机 Chrome/Chromium/Edge 本地存储中找到平台凭据记录。\n"
                 "请先在浏览器登录平台，然后重试；或改用粘贴导入。\n\n" + _BROWSER_LOGIN_HINT)
        return
    # 写入序号只在同一浏览器配置内有序；多个配置都有凭据时让用户选择来源。
    groups = group_candidates(candidates)
    note = ""
    if len(groups) == 1:
        newest = groups[0][0]
        if len(candidates) > 1:
            note = f"；共发现 {len(candidates)} 条记录，已采用本配置内最新一组"
    else:
        newest = _choose_browser_source(groups)
        if newest is None:
            return
    message = _accept_credentials(runtime, state, newest.credentials, persist=persist)
    if message:
        _message("凭据已载入", f"来源：{newest.browser}（{newest.profile} 配置）{note}。" + message)


def _credential_onboarding(runtime, state) -> None:
    """向导启动时检查一次凭据：缺失或后端不可用时引导导入，失败给出浏览器登录指引。"""
    if state.credential_prompted or state.credentials is not None:
        return
    saved, _ = _load_saved_credentials(runtime)
    if saved is not None:
        return
    state.credential_prompted = True
    choice = _dialog(button_dialog(
        title="凭据未导入",
        text="公开资源可直接下载；需要登录的教材需先导入凭据，也可以稍后再说。\n\n"
             "浏览器导入只读取本机 Chrome/Chromium/Edge 中平台域的本地存储记录。\n"
             "请先在 basic.smartedu.cn 登录；也可粘贴自行取得的 JSON。",
        buttons=[("从浏览器自动导入", "browser"), ("粘贴 JSON 导入", "import"), ("仅本次使用", "temporary"), ("稍后再说", "later")]))
    if choice in {None, "later"}:
        return
    if choice == "browser":
        _prompt_browser_import(runtime, state, persist=True)
        return
    _prompt_credentials(runtime, state, persist=choice == "import")


def _credential_menu(runtime: Runtime, state: WizardState) -> None:
    choice = _dialog(radiolist_dialog(title="凭据管理", text="凭据由用户自行从平台取得。工具不会自动登录。", values=[("browser", "从浏览器自动导入"), ("save", "粘贴 JSON 导入并保存到系统凭据库"), ("temporary", "粘贴 JSON，仅本次使用"), ("status", "查看本地状态"), ("clear", "清除已保存凭据")], ok_text="继续", cancel_text="返回"))
    if choice == "browser":
        _prompt_browser_import(runtime, state, persist=True)
    elif choice in {"save", "temporary"}:
        _prompt_credentials(runtime, state, persist=choice == "save")
    elif choice == "status":
        status = runtime.credentials.status()
        _message("本地凭据状态", "\n".join(f"{key}: {value}" for key, value in status.items()) + "\n本地存在不代表平台验证有效。")
    elif choice == "clear":
        runtime.credentials.clear()
        state.credentials = None
        _message("已清除", "本项目保存的凭据与本次交互副本已清除。")


def _review_download(runtime: Runtime, state: WizardState, identifiers: list[str], books: dict) -> int:
    from .commands import execute_download, saved_credentials

    while True:
        preview = "\n".join(_label(books[item].title) if item in books else item for item in identifiers[:8])
        if len(identifiers) > 8:
            preview += f"\n… 共 {len(identifiers)} 本"
        if state.credentials is not None:
            credential_line = "凭据：已导入（本次使用）"
        else:
            try:
                saved = runtime.credentials.load()
            except AppError:
                credential_line = "凭据：系统凭据库不可用，将仅尝试公开资源"
            else:
                credential_line = "凭据：使用系统凭据库中已保存的凭据" if saved else "凭据：未导入，将仅尝试公开资源；需要登录的教材会失败"
        action = _dialog(button_dialog(title="下载前确认", text=f"{preview}\n\n保存目录：{state.output}\nPDF 书签：{'开启' if state.bookmarks else '关闭'}\n并发：2；不覆盖现有冲突文件\n{credential_line}", buttons=[("开始下载", "start"), ("更改目录", "output"), ("书签开关", "bookmarks"), ("返回", "back")]))
        if action in {None, "back"}:
            return 0
        if action == "output":
            value = _dialog(input_dialog(title="保存目录", text="输入输出根目录", default=str(state.output), ok_text="保存", cancel_text="返回"))
            if value and value.strip():
                state.output = Path(value.strip()).expanduser()
            continue
        if action == "bookmarks":
            state.bookmarks = not state.bookmarks
            continue
        credentials = state.credentials
        if credentials is None:
            credentials, warnings = saved_credentials(runtime)
            for message in warnings:
                warn(message)
        result = execute_download(runtime, identifiers, output=state.output, bookmarks=state.bookmarks, credentials=credentials, books=books)
        show_download(download_data(result, credentials))
        for warning in download_warnings(result, credentials):
            warn(warning)
        if result.exit_code == 130:
            return 130
        failures = list(dict.fromkeys(item.resource_id for item in result.results if item.status == "failed"))

        def describe(item):
            line = f"{item.resource_id}：{item.status}"
            if item.error:
                line += f"\n    {item.error.get('code')}：{item.error.get('message')}"
            return line

        description = redact("\n".join(describe(item) for item in result.results[:12]), credentials)
        buttons = [("重试失败项", "retry")] if failures else []
        action = _dialog(button_dialog(title="下载结果", text=description or "没有可执行项目。", buttons=buttons + [("返回选书", "back")]))
        if action == "retry":
            identifiers = failures
            continue
        return result.exit_code


def _browse(runtime: Runtime, state: WizardState, *, selected_only: bool = False) -> int:
    direct_selected = selected_only
    snapshot = runtime.catalog.load()
    books = {book.resource_id: book for book in snapshot.books}
    for message in snapshot.warnings:
        warn(redact(message))
    while True:
        current = [books[key] for key in state.selection.selected if key in books] if selected_only else _matching_books(runtime, snapshot, state.query)
        action = _chooser(current, state, snapshot=snapshot, selected_only=selected_only)
        if action == "back":
            if selected_only:
                if direct_selected:
                    return 0
                selected_only = False
                continue
            return 0
        if action == "search":
            value = _dialog(input_dialog(title="搜索教材", text="以空格分隔全部关键词；留空查看完整目录", default=state.query, ok_text="搜索", cancel_text="返回"))
            if value is not None:
                state.query = value
                state.prefix = ()
                selected_only = False
        elif action == "selected":
            selected_only = True
        elif action == "download":
            if not state.selection.selected:
                _message("尚未选择教材", "使用空格勾选一本或多本教材。")
                continue
            identifiers = sorted(state.selection.selected)
            code = _review_download(runtime, state, identifiers, books)
            if code == 130:
                return code


def _plain_wizard(runtime: Runtime, state: WizardState, start_browse: bool) -> int:
    """NO_COLOR requests ordinary lines without cursor-control sequences."""
    from .commands import execute_download, prepare_inputs, saved_credentials

    out = console(stderr=True)
    while True:
        out.print("1 搜索并选择教材  2 粘贴链接  3 临时凭据  0 退出")
        choice = "1" if start_browse else input("选择：").strip()
        start_browse = False
        if choice == "0":
            return 0
        if choice == "3":
            import getpass

            state.credentials = parse_credentials(getpass.getpass("粘贴三字段凭据 JSON："))
            continue
        if choice == "2":
            value = input("输入教材链接或 ID，以空格分隔：").split()
            identifiers, books = prepare_inputs(runtime, [item for item in value if item.startswith("https://")], [item for item in value if not item.startswith("https://")], None)
        elif choice == "1":
            query = input(f"关键词（当前 {state.query or '无'}）：").strip()
            state.query = query
            snapshot = runtime.catalog.load()
            books = {book.resource_id: book for book in snapshot.books}
            matches = _matching_books(runtime, snapshot, query)
            while True:
                for index, book in enumerate(matches, 1):
                    out.print(f"{index} {state.selection.marker({book.resource_id})} {book.title} · {' / '.join(book.primary_category_path)}")
                out.print(f"已选 {len(state.selection.selected)} 本。输入序号切换，a 全选当前，u 清空当前，d 下载，b 返回。")
                action = input("> ").strip()
                visible = {book.resource_id for book in matches}
                if action == "a":
                    state.selection.selected.update(visible)
                elif action == "u":
                    state.selection.selected.difference_update(visible)
                elif action == "b":
                    break
                elif action == "d":
                    break
                else:
                    try:
                        for index in map(int, action.split()):
                            if 1 <= index <= len(matches):
                                state.selection.toggle({matches[index - 1].resource_id})
                    except ValueError:
                        out.print("请输入有效序号或操作字母。")
            if action != "d":
                continue
            identifiers = sorted(state.selection.selected)
        else:
            continue
        if not identifiers:
            out.print("尚未选择教材。")
            continue
        output = input(f"保存目录（回车使用 {state.output}）：").strip()
        if output:
            state.output = Path(output).expanduser()
        state.bookmarks = input("添加书签？[y/N] ").strip().casefold() == "y"
        if input(f"开始下载 {len(identifiers)} 本？[y/N] ").strip().casefold() != "y":
            continue
        credentials = state.credentials
        if credentials is None:
            credentials, warnings = saved_credentials(runtime)
            for message in warnings:
                warn(message)
        result = execute_download(runtime, identifiers, output=state.output, bookmarks=state.bookmarks, credentials=credentials, books=books)
        show_download(download_data(result, credentials))
        for warning in download_warnings(result, credentials):
            warn(warning)
        if result.exit_code == 130:
            return 130


SHELL_COMMANDS = {
    "/browse": "按分类浏览教材",
    "/search": "搜索教材，例如 /search 数学 七年级",
    "/paste": "粘贴教材页面链接或资源 ID",
    "/selected": "查看全部已选教材",
    "/download": "确认并下载已选教材",
    "/auth": "管理登录凭据",
    "/help": "查看命令与快捷键",
    "/quit": "退出",
}


class CommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for command, description in SHELL_COMMANDS.items():
            if command.startswith(text):
                yield Completion(command, start_position=-len(text), display_meta=description)


def _welcome() -> None:
    out = console(stderr=True)
    content = Text.assemble(("✳  ", "accent"), ("电子教材，在终端里找到。", "bold"),
                            "\n   国家中小学智慧教育平台\n\n",
                            (f"   {Path.cwd()}", "muted"))
    out.print(Panel(content, title=Text(f"Textbook {__version__}", style="accent"),
                    title_align="left", border_style="accent", box=box.ROUNDED,
                    padding=(1, 2), expand=False))
    out.print(Text.assemble(("  /browse", "accent"), " 浏览目录  ", ("/auth", "accent"),
                            " 凭据管理  ", ("/help", "accent"), " 全部命令"))
    out.print(Text("  直接输入关键词搜索教材；输入 / 选择命令，Tab 补全。", style="muted"))
    out.print()


def _shell_help() -> None:
    out = console(stderr=True)
    out.print(Text("可用命令", style="bold"))
    for command, description in SHELL_COMMANDS.items():
        out.print(Text.assemble((f"  {command:<12}", "accent"), description))
    out.print(Text("\n直接输入关键词也可搜索；↑↓ 查历史，Ctrl+D 退出。\n选书：空格勾选 · a 全选当前 · u 清空当前 · v 查看已选 · d 下载 · Esc 返回。",
                   style="muted"))


def _run_shell(runtime: Runtime, state: WizardState, start_browse: bool) -> int:
    session = PromptSession(
        message=command_prompt, completer=CommandCompleter(),
        style=PROMPT_STYLE, reserve_space_for_menu=6,
        placeholder=[("class:muted", "搜索教材，或输入 / 查看命令")],
    )
    while True:
        try:
            if start_browse:
                start_browse = False
                if _browse(runtime, state) == 130:
                    return 130
            status = f"  已选 {len(state.selection.selected)} 本 · 凭据：{_credential_status_line(runtime, state)} · / 命令 · Ctrl+D 退出"
            raw = session.prompt(bottom_toolbar=[("class:muted", status)]).strip()
            if not raw:
                continue
            command, _, argument = raw.partition(" ")
            if not command.startswith("/"):
                command, argument = "/search", raw
            if command == "/quit":
                return 0
            if command == "/help":
                _shell_help()
            elif command == "/browse":
                state.query, state.prefix = "", ()
                if _browse(runtime, state) == 130:
                    return 130
            elif command == "/search":
                value = argument.strip() or _dialog(input_dialog(title="搜索教材", text="输入关键词，以空格分隔；留空查看完整目录。",
                                                                  default=state.query, ok_text="搜索"))
                if value is not None:
                    state.query, state.prefix = value, ()
                    if _browse(runtime, state) == 130:
                        return 130
            elif command == "/selected":
                if _browse(runtime, state, selected_only=True) == 130:
                    return 130
            elif command == "/download":
                if not state.selection.selected:
                    warn("尚未选择教材；输入 /browse 浏览并用空格勾选。")
                    continue
                snapshot = runtime.catalog.load()
                books = {book.resource_id: book for book in snapshot.books}
                if _review_download(runtime, state, sorted(state.selection.selected), books) == 130:
                    return 130
            elif command == "/paste":
                from .commands import prepare_inputs

                value = _multiline("粘贴教材链接", description="每行输入一个教材页面链接或资源 ID。")
                if value:
                    entries = [line.strip() for line in value.splitlines() if line.strip()]
                    identifiers, books = prepare_inputs(runtime, [item for item in entries if item.startswith("https://")],
                                                        [item for item in entries if not item.startswith("https://")], None)
                    if _review_download(runtime, state, identifiers, books) == 130:
                        return 130
            elif command == "/auth":
                _credential_menu(runtime, state)
            else:
                warn(f"未知命令 {command}；输入 /help 查看可用命令。")
        except AppError as exc:
            _message("操作未完成", redact(str(exc), state.credentials))
        except EOFError:
            return 0


def run_wizard(runtime: Runtime, *, initial_query: str = "", start_browse: bool = False) -> int:
    import sys

    state = WizardState(query=initial_query)
    if "NO_COLOR" in os.environ:
        return _plain_wizard(runtime, state, start_browse)
    with create_app_session(output=create_output(stdout=sys.stderr)):
        _welcome()
        _credential_onboarding(runtime, state)
        return _run_shell(runtime, state, start_browse)
