"""Responsive display policy — one place for "what to show at what size."

Every visibility decision flows through `show(part)`. Adding a new rule is
literally one `case` in the match block below — that's the whole point of
this module. No call-site math, no scattered if-statements.

Pure-function style: each call reads the live terminal size + config
snapshot, so terminal resizes and config reloads are picked up on the
next render without any wiring.
"""

from anirss_lib import terminal
from anirss_lib.config import DisplayConfig
from anirss_lib.format import MIN_TITLE_ROWS, TITLE_BOX_OVERHEAD


# ── Width thresholds (columns) ───────────────────────────────────────
# A part is visible at columns >= its threshold. Set to 0 for "always".
# Add new thresholds here when you create new rules below.
SEEDERS_HIDE_BELOW   = 70
LEECHERS_HIDE_BELOW  = 70
SIZE_HIDE_BELOW      = 0
CATEGORY_HIDE_BELOW  = 0
DOWNLOADS_HIDE_BELOW = 0
TITLE_BOX_HIDE_BELOW = 0


# Picker height passed verbatim to fzf — percentage so it auto-resizes.
PICKER_HEIGHT_SPEC = "50%"


_DISPLAY: DisplayConfig | None = None


def set_display(cfg: DisplayConfig) -> None:
    """Snapshot the resolved display config at startup."""
    global _DISPLAY
    _DISPLAY = cfg


def _cfg(key: str, default: bool = False) -> bool:
    """Read a bool from the display-config snapshot. Safe before set_display."""
    if _DISPLAY is None:
        return default
    return bool(_DISPLAY.get(key, default))  # type: ignore[arg-type]


def show(part: str) -> bool:
    """Is `part` rendered at the current terminal size?

    ─── Recognized parts ────────────────────────────────────────────
        "downloads"   — "12345 dl" stats column
        "seeders"     — "1234s" stats column
        "leechers"    — "/56l" appended after seeders
        "size"        — "1.4 GiB" stats column
        "category"    — right-anchored "Anime - English-translated" chip
        "title-box"   — bordered ╭─╮ frame around the result list
        (anything else → visible by default)

    ─── Adding a new rule ───────────────────────────────────────────
    Pick a name (string), then add a `case "<name>":` below. Each case
    body returns a bool. You have access to:
        cols        — current terminal width
        _cfg("key") — bool from [display] config (force-overrides, etc.)
        show("x")   — compose another rule
    Then have the renderer ask `responsive.show("<name>")` before drawing.

    Example — hide size below 50 columns:
        case "size":
            return cols >= 50    # was: return cols >= SIZE_HIDE_BELOW
    """
    cols = terminal.get_size().columns
    match part:
        case "downloads":
            return cols >= DOWNLOADS_HIDE_BELOW
        case "seeders":
            if _cfg("force_show_seeders"):
                return True
            return cols >= SEEDERS_HIDE_BELOW
        case "leechers":
            # Leechers are appended after seeders, so they piggy-back on
            # seeders visibility. Then gated by an explicit config opt-in.
            return show("seeders") and _cfg("show_leechers") and cols >= LEECHERS_HIDE_BELOW
        case "size":
            return cols >= SIZE_HIDE_BELOW
        case "category":
            return cols >= CATEGORY_HIDE_BELOW
        case "title-box":
            return cols >= TITLE_BOX_HIDE_BELOW
        case _:
            # Unknown parts default to visible — opt-in to hiding.
            return True


def split_view() -> tuple[int, str]:
    """Return (title_cap, picker_height_spec) for the 50/50 refine view.

    Layout per iteration:
        1 — blank separator
        1 — "{N} result(s):" header
        H — title box (cap rows + 2 borders + maybe 1 truncation footer)
        1 — blank separator
        1 — "Query: …" line
        K — fzf picker (sized by fzf at runtime via PICKER_HEIGHT_SPEC)

    `cap` is sized for the top half. `picker_height_spec` is the string
    fzf uses for --height, so the picker resizes on SIGWINCH for free.
    """
    lines = terminal.get_size().lines
    half = max(MIN_TITLE_ROWS + TITLE_BOX_OVERHEAD, (lines - 4) // 2)
    cap = max(MIN_TITLE_ROWS, half - TITLE_BOX_OVERHEAD)
    return cap, PICKER_HEIGHT_SPEC
