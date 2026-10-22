"""Keep the command prompt's user flows independent of terminal rendering."""

from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document

from textbook_cli.cli import commands, interactive
from textbook_cli.cli.interactive import Selection, WizardState
from textbook_cli.core.errors import AppError
from textbook_cli.core.models import Book, Credentials

pytestmark = pytest.mark.cli


def _session(monkeypatch, *answers):
    values = iter(answers)
    calls = []

    class Session:
        def __init__(self, **kwargs):
            pass

        def prompt(self, **kwargs):
            calls.append(kwargs)
            answer = next(values)
            if isinstance(answer, BaseException):
                raise answer
            return answer

    monkeypatch.setattr(interactive, "PromptSession", Session)
    return calls


def _runtime(**kwargs):
    return SimpleNamespace(credentials=SimpleNamespace(load=lambda: None), **kwargs)


def test_bare_query_and_search_command_open_results_with_complete_keywords(monkeypatch):
    _session(monkeypatch, "数学 七年级", "/search 语文 八年级", "/quit")
    opened = []
    state = WizardState(selection=Selection({"already-selected"}), prefix=("旧分类",))

    def browse(runtime, current):
        opened.append((current.query, current.prefix))
        return 0

    monkeypatch.setattr(interactive, "_browse", browse)
    assert interactive._run_shell(_runtime(), state, False) == 0
    assert opened == [("数学 七年级", ()), ("语文 八年级", ())]
    assert state.selection.selected == {"already-selected"}


def test_selected_command_opens_selected_mode_and_keeps_selection(monkeypatch):
    _session(monkeypatch, "/selected", "/quit")
    calls = []
    state = WizardState(selection=Selection({"hidden", "visible"}), query="数学")

    def browse(runtime, current, *, selected_only=False):
        calls.append((current, selected_only))
        return 0

    monkeypatch.setattr(interactive, "_browse", browse)
    assert interactive._run_shell(_runtime(), state, False) == 0
    assert calls == [(state, True)]
    assert state.selection.selected == {"hidden", "visible"}
    assert state.query == "数学"


def test_direct_selected_view_returns_after_one_back(monkeypatch):
    books = [Book("selected", "已选教材"), Book("other", "其他教材")]
    runtime = _runtime(catalog=SimpleNamespace(load=lambda: SimpleNamespace(books=books, warnings=[])))
    state = WizardState(selection=Selection({"selected"}), prefix=("初中",))
    views = []

    def chooser(current, active_state, *, snapshot, selected_only):
        views.append(([book.resource_id for book in current], selected_only))
        assert len(views) == 1, "直接打开已选列表后，返回不应再打开完整目录"
        return "back"

    monkeypatch.setattr(interactive, "_chooser", chooser)
    assert interactive._browse(runtime, state, selected_only=True) == 0
    assert views == [(["selected"], True)]
    assert state.selection.selected == {"selected"}
    assert state.prefix == ("初中",)


def test_selected_view_opened_from_browser_returns_to_prior_browser(monkeypatch):
    books = [Book("selected", "已选教材"), Book("other", "其他教材")]
    runtime = _runtime(catalog=SimpleNamespace(load=lambda: SimpleNamespace(books=books, warnings=[])))
    state = WizardState(selection=Selection({"selected"}), prefix=("初中", "数学"))
    actions = iter(["selected", "back", "back"])
    views = []

    def chooser(current, active_state, *, snapshot, selected_only):
        views.append(([book.resource_id for book in current], selected_only, active_state.prefix))
        return next(actions)

    monkeypatch.setattr(interactive, "_chooser", chooser)
    assert interactive._browse(runtime, state) == 0
    assert views == [
        (["selected", "other"], False, ("初中", "数学")),
        (["selected"], True, ("初中", "数学")),
        (["selected", "other"], False, ("初中", "数学")),
    ]
    assert state.selection.selected == {"selected"}


def test_download_without_selection_gives_hint_without_loading_catalog(monkeypatch, capsys):
    _session(monkeypatch, "/download", "/quit")

    def unexpected():
        raise AssertionError("尚未选择教材时不应加载目录或开始下载")

    runtime = _runtime(catalog=SimpleNamespace(load=unexpected))
    monkeypatch.setattr(interactive, "_review_download", unexpected)
    assert interactive._run_shell(runtime, WizardState(), False) == 0
    assert "尚未选择教材" in capsys.readouterr().err


def test_download_selected_books_opens_review_in_stable_order(monkeypatch):
    _session(monkeypatch, "/download", "/quit")
    books = [Book("book-b", "教材乙"), Book("book-a", "教材甲")]
    runtime = _runtime(catalog=SimpleNamespace(load=lambda: SimpleNamespace(books=books)))
    state = WizardState(selection=Selection({"book-b", "book-a"}))
    calls = []

    def review(active_runtime, current, identifiers, book_map):
        calls.append((active_runtime, current, identifiers, book_map))
        return 0

    monkeypatch.setattr(interactive, "_review_download", review)
    assert interactive._run_shell(runtime, state, False) == 0
    assert calls == [(runtime, state, ["book-a", "book-b"], {book.resource_id: book for book in books})]


def test_paste_validates_links_and_ids_before_review(monkeypatch):
    _session(monkeypatch, "/paste", "/quit")
    url = "https://basic.smartedu.cn/tchMaterial/detail?contentId=book-a"
    monkeypatch.setattr(interactive, "_multiline", lambda *args, **kwargs: f"{url}\nbook-b\n\n")
    prepared, reviewed = [], []

    def prepare(runtime, urls, identifiers, input_file):
        prepared.append((urls, identifiers, input_file))
        return ["book-a", "book-b"], {}

    def review(runtime, state, identifiers, books):
        reviewed.append((identifiers, books))
        return 0

    monkeypatch.setattr(commands, "prepare_inputs", prepare)
    monkeypatch.setattr(interactive, "_review_download", review)
    assert interactive._run_shell(_runtime(), WizardState(), False) == 0
    assert prepared == [([url], ["book-b"], None)]
    assert reviewed == [(["book-a", "book-b"], {})]


@pytest.mark.parametrize("answer", ["/quit", EOFError()])
def test_quit_and_closed_input_exit_cleanly(monkeypatch, answer):
    calls = _session(monkeypatch, answer)
    assert interactive._run_shell(_runtime(), WizardState(), False) == 0
    assert len(calls) == 1


def test_ctrl_c_propagates_for_command_exit_code_130(monkeypatch):
    _session(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        interactive._run_shell(_runtime(), WizardState(), False)


def test_service_failure_is_redacted_and_shell_can_continue(monkeypatch):
    _session(monkeypatch, "数学", "语文", "/quit")
    opened, messages = [], []
    state = WizardState(credentials=Credentials("fixture-secret-token", "fixture-secret-key"))

    def browse(runtime, current):
        opened.append(current.query)
        if len(opened) == 1:
            raise AppError("NETWORK_ERROR", "测试失败 fixture-secret-token / fixture-secret-key")
        return 0

    monkeypatch.setattr(interactive, "_browse", browse)
    monkeypatch.setattr(interactive, "_message", lambda title, text: messages.append((title, text)))
    assert interactive._run_shell(_runtime(), state, False) == 0
    assert opened == ["数学", "语文"]
    assert len(messages) == 1
    assert "fixture-secret-token" not in messages[0][1]
    assert "fixture-secret-key" not in messages[0][1]
    assert "测试失败" in messages[0][1]


@pytest.mark.parametrize("text, expected", [
    ("/s", ["/search", "/selected"]),
    ("/search 数学", []),
    ("数学", []),
])
def test_command_completion_does_not_replace_search_arguments(text, expected):
    completions = list(interactive.CommandCompleter().get_completions(Document(text), None))
    assert [item.text for item in completions] == expected
