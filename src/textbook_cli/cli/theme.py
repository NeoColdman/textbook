"""Shared terminal palette; body text follows the user's terminal theme."""

from rich.theme import Theme


ACCENT = "#d97757"
MUTED = "#8b8680"
SUCCESS = "#78a87c"
WARNING = "#d2a768"
ERROR = "#d66b68"

TERMINAL_THEME = Theme({
    "accent": ACCENT,
    "muted": MUTED,
    "success": SUCCESS,
    "warning": WARNING,
    "error": ERROR,
})
