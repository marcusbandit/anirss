"""ANSI escape codes and ansi-aware text helpers."""

import re

C_RED, C_GRN, C_YEL, C_BLU, C_MAG, C_CYN, C_DIM, C_BLD, C_OFF = (
    "\033[31m", "\033[32m", "\033[33m", "\033[34m", "\033[35m",
    "\033[36m", "\033[2m", "\033[1m", "\033[0m",
)

# Force a strong highlight for typed-text matches in fzf (default theme can be
# too subtle). `hl` = matched chars in non-current rows; `hl+` = matched chars
# in the current row.
FZF_HL_COLORS = "hl:bright-yellow:bold,hl+:bright-yellow:bold:reverse"

# Editing keybindings present in every fzf invocation. Alt-Backspace
# (Option+Delete on macOS) deletes the previous word; without an explicit bind
# fzf treats the ESC prefix as the start of an unfinished escape sequence and
# the picker appears frozen.
FZF_BINDS = (
    "alt-bspace:backward-kill-word,"
    "alt-bs:backward-kill-word,"
    "ctrl-w:backward-kill-word"
)

PROMPT_FILTER = f"{C_YEL}Search >{C_OFF} {C_BLU}Filter >{C_OFF} "
PROMPT_ACTION = f"{C_YEL}Search >{C_OFF} {C_BLU}Filter >{C_OFF} {C_GRN}Action >{C_OFF} "

# Max height (terminal lines) for the compact pickers (filter, action, etc.).
# Kept small so the printed Results box above can use the rest of the screen.
FILTER_PICKER_LINES = 14

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def ansi_strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


def right_anchor(left: str, right: str, width: int) -> str:
    """Pad with spaces so `right` ends at column `width`. ANSI in either side
    is allowed; padding is computed against visible width."""
    pad = width - len(ansi_strip(left)) - len(ansi_strip(right))
    if pad < 1:
        pad = 1
    return f"{left}{' ' * pad}{right}"


def truncate_ansi(s: str, max_visible: int) -> str:
    """Cut `s` to `max_visible` visible chars, copying ANSI escapes verbatim.
    If a cut happens, append a dim ellipsis."""
    if len(ansi_strip(s)) <= max_visible:
        return s
    out: list[str] = []
    visible = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\x1b":
            end = s.find("m", i)
            if end == -1:
                break
            out.append(s[i:end + 1])
            i = end + 1
            continue
        if visible >= max_visible - 1:
            break
        out.append(ch)
        visible += 1
        i += 1
    out.append(f"{C_DIM}…{C_OFF}")
    return "".join(out)
