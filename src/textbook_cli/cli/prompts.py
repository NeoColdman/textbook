"""Compact inline terminal prompts; no alternate screen or dialog chrome."""

from __future__ import annotations

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, ScrollOffsets, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from .theme import ACCENT


PROMPT_STYLE = Style.from_dict({
    "accent": ACCENT,
    "title": "bold",
    "muted": "#888888",
    "selected": f"{ACCENT} bold",
    "marker": ACCENT,
    "rule": "#666666",
    "text-area": "",
    "text-area.prompt": f"{ACCENT} bold",
    "completion-menu.completion": "bg:default #888888",
    "completion-menu.completion.current": f"bg:default {ACCENT} bold",
    "completion-menu.meta.completion": "bg:default #888888",
    "completion-menu.meta.completion.current": f"bg:default {ACCENT}",
    "bottom-toolbar": "noreverse",
    "bottom-toolbar.text": "#888888",
    "auto-suggestion": "#888888",
})


def command_prompt():
    width = max(1, get_app().output.get_size().columns - 1)
    return [("class:rule", "─" * width), ("", "\n"), ("class:accent", "❯ ")]


def _title(title: str) -> Window:
    return Window(FormattedTextControl([("class:accent", "❯ "), ("class:title", title)]),
                  height=1, dont_extend_height=True)


def _hint(text: str) -> Window:
    return Window(FormattedTextControl([("class:muted", text)]),
                  wrap_lines=True, dont_extend_height=True)


def _cancel_keys() -> KeyBindings:
    keys = KeyBindings()

    @keys.add("escape")
    def back(event):
        event.app.exit(result=None)

    @keys.add("c-c")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    return keys


def _menu(*, title: str, text: str, values: list, default=None, ok_text="继续", style=None):
    cursor = next((index for index, (value, _) in enumerate(values) if value == default), 0)
    keys = _cancel_keys()

    def fragments():
        result = []
        for index, (_, label) in enumerate(values):
            selected = index == cursor
            result.append(("class:selected" if selected else "",
                           f"  {'❯' if selected else ' '} {index + 1}. {label}\n"))
        return result

    @keys.add("up")
    @keys.add("s-tab")
    def up(event):
        nonlocal cursor
        cursor = max(0, cursor - 1)

    @keys.add("down")
    @keys.add("tab")
    def down(event):
        nonlocal cursor
        cursor = min(len(values) - 1, cursor + 1)

    @keys.add("enter")
    def accept(event):
        if values:
            event.app.exit(result=values[cursor][0])

    for index in range(min(9, len(values))):
        def select(event, index=index):
            event.app.exit(result=values[index][0])
        keys.add(str(index + 1))(select)

    control = FormattedTextControl(fragments, focusable=True, show_cursor=False,
                                  get_cursor_position=lambda: Point(x=0, y=cursor))
    children = [_title(title), Window(height=1)]
    if text:
        children.extend([Window(FormattedTextControl(text), wrap_lines=True, dont_extend_height=True),
                         Window(height=1)])
    children.extend([
        Window(control, wrap_lines=True, height=Dimension(min=1, max=10),
               dont_extend_height=True, scroll_offsets=ScrollOffsets(top=1, bottom=1)),
        Window(height=1),
        _hint(f"  ↑↓ 选择 · Enter {ok_text} · 数字快捷选择 · Esc 返回 · Ctrl+C 退出"),
    ])
    return Application(layout=Layout(HSplit(children), focused_element=control), key_bindings=keys,
                       full_screen=False, erase_when_done=True, mouse_support=False,
                       style=style or PROMPT_STYLE)


def button_dialog(title="", text="", buttons=None, style=None):
    """Keep the familiar factory API while showing actions as vertical choices."""
    return _menu(title=title, text=text, values=[(value, label) for label, value in buttons or []],
                 style=style)


def radiolist_dialog(title="", text="", ok_text="继续", cancel_text="返回", values=None,
                     default=None, style=None):
    return _menu(title=title, text=text, values=list(values or []), default=default,
                 ok_text=ok_text, style=style)


def input_dialog(title="", text="", ok_text="确认", cancel_text="返回", completer=None,
                 validator=None, password=False, style=None, default="", *, multiline=False):
    field = TextArea(text=default, multiline=multiline, password=password, prompt="❯ ",
                     wrap_lines=True, completer=completer, validator=validator,
                     height=Dimension(min=1, max=8), dont_extend_height=True)
    field.buffer.cursor_position = len(default)
    keys = _cancel_keys()

    def accept(event):
        if field.buffer.validate(set_cursor=True):
            event.app.exit(result=field.text)

    if multiline:
        keys.add("c-d")(accept)
        keys.add("c-s")(accept)
    else:
        keys.add("enter")(accept)

    submit = "Ctrl+D / Ctrl+S" if multiline else "Enter"
    children = [_title(title), Window(height=1)]
    if text:
        children.extend([Window(FormattedTextControl(text), wrap_lines=True, dont_extend_height=True),
                         Window(height=1)])
    children.extend([Window(height=1, char="─", style="class:rule"), field,
                     Window(height=1, char="─", style="class:rule"),
                     _hint(f"  {submit} {ok_text} · Esc {cancel_text} · Ctrl+C 退出")])
    return Application(layout=Layout(HSplit(children), focused_element=field), key_bindings=keys,
                       full_screen=False, erase_when_done=True, mouse_support=False,
                       style=style or PROMPT_STYLE)


def message_dialog(title="", text="", ok_text="返回", style=None):
    body = TextArea(text=text, read_only=True, wrap_lines=True, scrollbar=False,
                    height=Dimension(min=1, max=16), dont_extend_height=True)
    keys = _cancel_keys()

    @keys.add("enter")
    def accept(event):
        event.app.exit(result=None)

    children = [_title(title), Window(height=1), body, Window(height=1),
                _hint(f"  ↑↓ 滚动 · Enter / Esc {ok_text} · Ctrl+C 退出")]
    return Application(layout=Layout(HSplit(children), focused_element=body), key_bindings=keys,
                       full_screen=False, erase_when_done=True, mouse_support=False,
                       style=style or PROMPT_STYLE)
