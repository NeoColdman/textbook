"""Exercise compact terminal prompts through real keyboard input."""

import io
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from textbook_cli.cli import interactive, prompts
from textbook_cli.core.models import Book

pytestmark = pytest.mark.cli


@pytest.mark.parametrize("keys, expected", [
    ("\r", "browse"),
    ("\x1b[B\r", "search"),
    ("\x1b[B\x1b[B\x1b[A\r", "search"),
    ("2", "search"),
    ("\x1b", None),
])
def test_choice_prompt_accepts_navigation_numbers_and_escape(keys, expected):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = prompts.button_dialog(
            title="电子教材",
            text="选择操作",
            buttons=[("浏览目录", "browse"), ("搜索教材", "search"), ("返回", "back")],
        )
        assert app.full_screen is False
        pipe.send_text(keys)
        assert app.run() == expected


def test_radiolist_respects_default_and_supports_direct_enter():
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = prompts.radiolist_dialog(
            title="选择凭据来源",
            values=[("chrome", "Chrome"), ("edge", "Edge")],
            default="edge",
        )
        pipe.send_text("\r")
        assert app.run() == "edge"


def test_long_menu_scrolls_to_choice_beyond_visible_rows_in_narrow_terminal():
    stream = io.StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=16, columns=48), term="xterm", enable_cpr=False)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        app = prompts.radiolist_dialog(
            title="选择浏览器配置",
            values=[(index, f"浏览器配置 {index:02} · 用于验证长标签和窄终端中的列表滚动")
                    for index in range(20)],
        )
        renders = []

        def after_render(application):
            renders.append(True)
            if len(renders) == 1:
                pipe.send_text("\x1b[B" * 19)
            elif len(renders) == 2:
                pipe.send_text("\r")

        app.after_render += after_render
        assert app.run() == 19
    assert "19" in stream.getvalue()
    assert "Window too small" not in stream.getvalue()


def test_wrapped_book_rows_scroll_and_select_final_book_without_losing_hidden_selection(monkeypatch):
    stream = io.StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=16, columns=48), term="xterm", enable_cpr=False)
    books = [Book(f"book-{index:02}", f"教材 {index:02} · 义务教育教科书数学七年级上册根据新版课程标准修订")
             for index in range(24)]
    state = interactive.WizardState(selection=interactive.Selection({"hidden-selected"}))
    application_class = interactive.Application
    frames = []
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        def application(**kwargs):
            app = application_class(**kwargs)

            def after_render(current):
                screen = current.renderer.last_rendered_screen
                frames.append("\n".join(
                    "".join(cell.char for _, cell in sorted(row.items()))
                    for _, row in sorted(screen.data_buffer.items())
                ) if screen is not None else "")
                if len(frames) == 1:
                    pipe.send_text("\x1b[B" * 23)
                elif len(frames) == 2:
                    pipe.send_text(" d")

            app.after_render += after_render
            return app

        monkeypatch.setattr(interactive, "Application", application)
        assert interactive._chooser(books, state) == "download"
    assert state.selection.selected == {"hidden-selected", "book-23"}
    assert "教材 23" in frames[1]
    assert "Window too small" not in stream.getvalue()
    assert "\x1b[?1049h" not in stream.getvalue()


def test_real_home_prompt_executes_help_and_quit_inline(monkeypatch, capsys):
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = io.StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=24, columns=80),
                          term="xterm-256color", enable_cpr=False)
    runtime = SimpleNamespace(credentials=SimpleNamespace(load=lambda: None))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        pipe.send_text("/help\r/quit\r")
        assert interactive._run_shell(runtime, interactive.WizardState(), False) == 0
    assert "可用命令" in capsys.readouterr().err
    assert "\x1b[?1049h" not in stream.getvalue()


@pytest.mark.parametrize("keys, expected", [
    ("数学 七年级\r", "数学 七年级"),
    ("\x1b", None),
])
def test_input_prompt_submits_or_cancels(keys, expected):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = prompts.input_dialog(title="搜索教材", text="输入关键词")
        assert app.full_screen is False
        pipe.send_text(keys)
        assert app.run() == expected


def test_input_prompt_edits_existing_value():
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = prompts.input_dialog(title="保存目录", default="old-directory")
        pipe.send_text("\x01\x0b新的目录\r")
        assert app.run() == "新的目录"


@pytest.mark.parametrize("factory", [
    lambda: prompts.button_dialog(title="确认", buttons=[("继续", True)]),
    lambda: prompts.input_dialog(title="搜索"),
    lambda: prompts.message_dialog(title="结果", text="已完成"),
])
def test_prompt_ctrl_c_raises_interrupt(factory):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = factory()
        pipe.send_bytes(b"\x03")
        with pytest.raises(KeyboardInterrupt):
            app.run()


def test_secret_multiline_preserves_lines_without_rendering_values():
    stream = io.StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=28, columns=80), term="xterm", enable_cpr=False)
    secret = "fixture-access-token\nfixture-mac-key"
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        pipe.send_text(secret)
        pipe.send_bytes(b"\x04")
        assert interactive._multiline("粘贴凭据 JSON", secret=True) == secret
    rendered = stream.getvalue()
    assert "fixture-access-token" not in rendered
    assert "fixture-mac-key" not in rendered
    assert "\x1b[?1049h" not in rendered
