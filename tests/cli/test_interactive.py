"""Selection semantics are tested independently of a real terminal."""

import inspect

import pytest
from types import SimpleNamespace
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from textbook_cli.cli import commands, interactive
from textbook_cli.cli.interactive import Row, Selection, WizardState, _chooser, _multiline, category_rows
from textbook_cli.core.errors import AppError
from textbook_cli.core.models import BatchResult, Book, Credentials, DownloadResult
from textbook_cli.storage.browser_import import BrowserCredential

pytestmark = pytest.mark.cli


def _button_dialog_stub(captured: list):
    """替换 button_dialog，但先按真实签名校验参数，防止传入不存在的关键字。"""
    from prompt_toolkit.shortcuts import button_dialog as real

    valid = inspect.signature(real).parameters

    def factory(**kwargs):
        for key in kwargs:
            assert key in valid, f"button_dialog 不接受参数 {key!r}"
        captured.append(kwargs.get("text"))
        return object()

    return factory


def test_filtered_all_and_clear_preserve_hidden_selection():
    selection = Selection({"hidden", "visible-a"})
    selection.toggle({"visible-a", "visible-b"})
    assert selection.selected == {"hidden", "visible-a", "visible-b"}
    assert selection.marker({"visible-a", "visible-b"}) == "[x]"
    selection.toggle({"visible-a", "visible-b"})
    assert selection.selected == {"hidden"}
    assert selection.marker({"hidden", "not-selected"}) == "[-]"


def test_dynamic_categories_merge_multi_path_resource_ids():
    books = [
        Book("one", "第一册", category_paths=(("初中", "数学"), ("高中", "数学"))),
        Book("two", "第二册", category_paths=(("初中", "数学", "七年级"),)),
    ]
    root = category_rows(books)
    assert [row.category for row in root] == [("初中",), ("高中",)]
    assert root[0].identifiers == {"one", "two"}
    nested = category_rows(books, ("初中", "数学"))
    assert nested[0].category == ("初中", "数学", "七年级")
    assert nested[1].identifiers == {"one"}


def test_no_path_books_remain_selectable_and_empty_categories_do_not_appear():
    book = Book("one", "无分类教材")
    rows = category_rows([book])
    assert len(rows) == 1
    assert rows[0].category is None
    assert rows[0].identifiers == {"one"}
    assert category_rows([]) == []


def test_selected_flat_list_deduplicates_multi_path_books():
    book = Book("one", "很长的教材标题", category_paths=(("甲",), ("乙",)))
    rows = category_rows([book], flat=True)
    assert len(rows) == 1
    assert "很长的教材标题" in rows[0].label


def test_header_shows_total_and_visible_selection_separately():
    state = WizardState(selection=Selection({"hidden", "a1"}))
    state.query = "数学七年级"
    state.prefix = ("电子教材", "初中", "数学")
    rows = [
        Row("人教版 /（2 本）", {"a1", "a2"}, ("电子教材", "初中", "数学", "人教版")),
        Row("冀教版 /（2 本）", {"b1", "b2"}, ("电子教材", "初中", "数学", "冀教版")),
    ]
    text = interactive.header_text(state, rows, snapshot=None)
    assert "已选 2 本（当前结果 1 本）" in text
    assert "电子教材 / 初中 / 数学" in text


def test_header_total_only_when_all_selected_are_visible():
    state = WizardState(selection=Selection({"a1", "b1"}))
    rows = [
        Row("人教版 /（1 本）", {"a1"}, ("初中", "数学", "人教版")),
        Row("冀教版 /（1 本）", {"b1"}, ("初中", "数学", "冀教版")),
    ]
    text = interactive.header_text(state, rows, snapshot=None)
    assert "已选 2 本" in text
    assert "当前结果" not in text


def test_header_selected_view_offers_clear_all():
    state = WizardState(selection=Selection({"a1", "b1", "hidden"}))
    rows = category_rows(
        [Book("a1", "甲"), Book("b1", "乙")], flat=True)
    text = interactive.header_text(state, rows, snapshot=None, selected_only=True)
    assert "全部已选教材" in text
    assert "u 清空全部" in text
    assert "已选 3 本" in text
    assert "当前结果" not in text


def test_real_keyboard_category_selection_keeps_hidden_selection():
    books = [Book("one", "第一册", category_paths=(("初中", "数学"),)), Book("two", "第二册", category_paths=(("初中", "数学"),))]
    state = WizardState(selection=Selection({"hidden"}))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(" \r")
        action = _chooser(books, state)
    assert action == "level"
    assert state.prefix == ("初中",)
    assert state.selection.selected == {"hidden", "one", "two"}


def test_real_keyboard_search_returns_without_losing_selection():
    state = WizardState(selection=Selection({"one"}))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("/")
        action = _chooser([Book("one", "教材")], state)
    assert action == "search"
    assert state.selection.selected == {"one"}


def test_real_keyboard_ctrl_c_raises_interrupt():
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_bytes(b"\x03")
        with pytest.raises(KeyboardInterrupt):
            _chooser([], WizardState())


def test_multiline_paste_keeps_all_lines_until_explicit_submit():
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("book-a\nbook-b")
        pipe.send_bytes(b"\x04")
        result = _multiline("粘贴链接")
    assert result == "book-a\nbook-b"


@pytest.fixture
def warning_result():
    return BatchResult(1, [DownloadResult(
        "book-a", "pdf", "completed", path="book.pdf", bookmarks="failed",
        warnings=["书签处理失败，已保留原始 PDF", "平台未提供可用章节", "文件摘要格式不标准", "临时检查 fixture-token / fixture-key"],
    )], warnings=["本次批次警告", "书签处理失败，已保留原始 PDF"])


def test_review_shows_file_and_batch_warnings_without_secrets(monkeypatch, capsys, warning_result):
    actions = iter(["start", "back"])
    captured = []
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(actions))
    monkeypatch.setattr(interactive, "button_dialog", _button_dialog_stub(captured))
    monkeypatch.setattr(commands, "execute_download", lambda *args, **kwargs: warning_result)
    state = WizardState(credentials=Credentials("fixture-token", "fixture-key"))
    assert interactive._review_download(object(), state, ["book-a"], {}) == 0
    output = capsys.readouterr()
    assert "本次批次警告" in output.err
    assert output.err.count("书签处理失败") == 1
    assert "平台未提供可用章节" in output.err
    assert "文件摘要格式不标准" in output.err
    assert "fixture-token" not in output.out + output.err
    assert "fixture-key" not in output.out + output.err


def test_review_confirm_dialog_shows_missing_credential_hint(monkeypatch):
    captured = []
    actions = iter(["back"])
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(actions))

    def fake_button_dialog(**kwargs):
        captured.append(kwargs.get("text"))
        return object()

    monkeypatch.setattr(interactive, "button_dialog", fake_button_dialog)
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None))
    assert interactive._review_download(runtime, WizardState(), ["book-a"], {}) == 0
    assert any(t and "未导入" in t and "仅尝试公开资源" in t for t in captured)


def test_review_result_dialog_shows_error_reasons(monkeypatch):
    captured = []
    actions = iter(["start", "back"])
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(actions))

    def fake_button_dialog(**kwargs):
        captured.append(kwargs.get("text"))
        return object()

    monkeypatch.setattr(interactive, "button_dialog", fake_button_dialog)
    monkeypatch.setattr(commands, "execute_download", lambda *args, **kwargs: BatchResult(2, [
        DownloadResult("book-a", None, "failed", error={"code": "AUTH_REQUIRED", "message": "平台拒绝匿名访问（HTTP 401），请提供自己的登录凭据。"}),
        DownloadResult("book-b", None, "failed", error={"code": "NETWORK_ERROR", "message": "下载连接中断。"}),
    ]))
    monkeypatch.setattr(interactive, "show_download", lambda data: None)
    monkeypatch.setattr(interactive, "download_data", lambda result, credentials: {})
    monkeypatch.setattr(interactive, "download_warnings", lambda result, credentials: [])
    state = WizardState(credentials=Credentials("fixture-token", "fixture-key"))
    books = {"book-a": Book("book-a", "教材甲"), "book-b": Book("book-b", "教材乙")}
    # 两项分别失败于凭据错误与网络错误：不全是凭据错误，按文档矩阵整体退出码为 1。
    assert interactive._review_download(object(), state, ["book-a", "book-b"], books) == 1
    assert "AUTH_REQUIRED" in captured[1]
    assert "平台拒绝匿名访问（HTTP 401）" in captured[1]
    assert "NETWORK_ERROR：下载连接中断。" in captured[1]
    assert "fixture-token" not in captured[0] + captured[1]


def test_onboarding_silent_when_credentials_saved(monkeypatch):
    state = WizardState()
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: Credentials("t", "m", 0)))

    def no_dialog(app):
        raise AssertionError("已有凭据时不应弹出引导")

    monkeypatch.setattr(interactive, "_dialog", no_dialog)
    interactive._credential_onboarding(runtime, state)
    assert state.credential_prompted is False  # 静默路径没有发生引导
    assert state.credentials is None


def test_onboarding_can_be_deferred_and_not_repeated(monkeypatch):
    state = WizardState()
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None))
    actions = iter(["later"])
    captured = []
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(actions))
    monkeypatch.setattr(interactive, "button_dialog", _button_dialog_stub(captured))
    interactive._credential_onboarding(runtime, state)
    assert state.credential_prompted is True
    assert state.credentials is None

    def no_dialog(app):
        raise AssertionError("稍后再说后本会话不应再次引导")

    monkeypatch.setattr(interactive, "_dialog", no_dialog)
    interactive._credential_onboarding(runtime, state)


def test_onboarding_invalid_json_shows_browser_login_hint(monkeypatch):
    state = WizardState()
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None))

    def no_save(credentials):
        raise AssertionError("解析失败时不应保存")

    runtime.credentials.save = no_save
    dialogs = iter(["import"])
    captured = []
    messages = []
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(dialogs))
    monkeypatch.setattr(interactive, "button_dialog", _button_dialog_stub(captured))
    monkeypatch.setattr(interactive, "_multiline", lambda *args, **kwargs: "不是JSON")
    monkeypatch.setattr(interactive, "_message", lambda title, text: messages.append((title, text)))
    interactive._credential_onboarding(runtime, state)
    assert state.credentials is None
    assert any("凭据格式无效" in title for title, _ in messages)
    assert any("浏览器" in text for _, text in messages)


def test_onboarding_import_saves_to_keyring(monkeypatch):
    state = WizardState()
    saved = {}
    runtime = SimpleNamespace(credentials=SimpleNamespace(
        load=lambda: None, save=lambda credentials: saved.setdefault("credentials", credentials)))
    dialogs = iter(["import"])
    captured = []
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(dialogs))
    monkeypatch.setattr(interactive, "button_dialog", _button_dialog_stub(captured))
    monkeypatch.setattr(interactive, "_multiline", lambda *args, **kwargs: '{"access_token": "t", "mac_key": "m", "diff": 5}')
    monkeypatch.setattr(interactive, "_message", lambda title, text: None)
    interactive._credential_onboarding(runtime, state)
    assert saved["credentials"].access_token == "t"
    assert state.credentials.access_token == "t"


def test_onboarding_save_failure_falls_back_to_temporary(monkeypatch):
    state = WizardState()

    def failing_save(credentials):
        raise AppError("CREDENTIAL_STORE_ERROR", "系统凭据库不可用")

    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None, save=failing_save))
    dialogs = iter(["import", True])
    captured = []
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(dialogs))
    monkeypatch.setattr(interactive, "button_dialog", _button_dialog_stub(captured))
    monkeypatch.setattr(interactive, "_multiline", lambda *args, **kwargs: '{"access_token": "t", "mac_key": "m", "diff": 5}')
    monkeypatch.setattr(interactive, "_message", lambda title, text: None)
    interactive._credential_onboarding(runtime, state)
    assert state.credentials.access_token == "t"


def test_menu_credential_status_line():
    state = WizardState()
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None))
    assert interactive._credential_status_line(runtime, state) == "未导入"
    state.credentials = Credentials("t", "m", 0)
    assert interactive._credential_status_line(runtime, state) == "本次使用"
    broken = SimpleNamespace(credentials=SimpleNamespace(load=lambda: (_ for _ in ()).throw(AppError("CREDENTIAL_STORE_ERROR", "x"))))
    assert interactive._credential_status_line(broken, WizardState()) == "后端不可用"


def test_onboarding_dialog_works_with_real_prompt_toolkit():
    """真实 widget 构造引导对话框；传错参数会立即 TypeError（回归 ok_text 事故）。"""
    state = WizardState()
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b")  # Esc 取消引导，等同稍后再说
        interactive._credential_onboarding(runtime, state)
    assert state.credential_prompted is True
    assert state.credentials is None


def test_onboarding_browser_import_saves_newest(monkeypatch):
    state = WizardState()
    saved = {}
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None, save=lambda c: saved.setdefault("c", c)))
    dialogs = iter(["browser"])
    captured = []
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(dialogs))
    monkeypatch.setattr(interactive, "button_dialog", _button_dialog_stub(captured))
    monkeypatch.setattr(interactive, "import_from_browser",
                        lambda: [BrowserCredential(Credentials("t", "m", 5), "Chrome", "Default", 9)])
    monkeypatch.setattr(interactive, "_message", lambda title, text: None)
    interactive._credential_onboarding(runtime, state)
    assert saved["c"].access_token == "t"
    assert state.credentials.access_token == "t"


def test_onboarding_browser_import_empty_shows_hint(monkeypatch):
    state = WizardState()
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None, save=lambda c: None))
    dialogs = iter(["browser"])
    captured = []
    messages = []
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(dialogs))
    monkeypatch.setattr(interactive, "button_dialog", _button_dialog_stub(captured))
    monkeypatch.setattr(interactive, "import_from_browser", lambda: [])
    monkeypatch.setattr(interactive, "_message", lambda title, text: messages.append((title, text)))
    interactive._credential_onboarding(runtime, state)
    assert any("未能自动导入" in title for title, _ in messages)
    assert state.credentials is None


def test_onboarding_browser_import_multiple_sources_asks_user(monkeypatch):
    """多个浏览器配置都有凭据时不猜“最新”，弹出选择对话框由用户决定。"""
    state = WizardState()
    saved = {}
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None, save=lambda c: saved.setdefault("c", c)))
    dialogs = iter(["browser"])
    captured = []
    monkeypatch.setattr(interactive, "_dialog", lambda app: next(dialogs))
    monkeypatch.setattr(interactive, "button_dialog", _button_dialog_stub(captured))
    monkeypatch.setattr(interactive, "import_from_browser", lambda: [
        BrowserCredential(Credentials("stale", "k", 1), "Edge", "Old", 99),
        BrowserCredential(Credentials("fresh", "k", 2), "Chrome", "Default", 3),
    ])
    messages = []
    monkeypatch.setattr(interactive, "_message", lambda title, text: messages.append((title, text)))
    monkeypatch.setattr(
        interactive, "_choose_browser_source",
        lambda groups: next(group[0] for group in groups if group[0].credentials.access_token == "fresh"),
    )
    interactive._credential_onboarding(runtime, state)
    assert saved["c"].access_token == "fresh"
    assert state.credentials.access_token == "fresh"


def test_no_color_wizard_shows_file_and_batch_warnings(monkeypatch, capsys, warning_result):
    answers = iter(["2", "book-a", "", "y", "y", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(commands, "execute_download", lambda *args, **kwargs: warning_result)
    runtime = SimpleNamespace(catalog=SimpleNamespace(cached=lambda: None))
    state = WizardState(credentials=Credentials("fixture-token", "fixture-key"))
    assert interactive._plain_wizard(runtime, state, False) == 0
    output = capsys.readouterr()
    assert "本次批次警告" in output.err
    assert "书签处理失败" in output.err
    assert "平台未提供可用章节" in output.err
    assert "文件摘要格式不标准" in output.err
    assert "fixture-token" not in output.out + output.err
    assert "fixture-key" not in output.out + output.err
