"""Display helpers: colored stats, titles, results box."""

from anirss_lib.ansi import (
    C_CYN, C_DIM, C_GRN, C_OFF, C_RED, C_YEL,
    ansi_strip, right_anchor, truncate_ansi,
)
from anirss_lib import terminal
from anirss_lib.titles import POSTER_RE
from anirss_lib.types import Item

# Imported lazily inside format_stats to avoid an import cycle:
# responsive imports format (for MIN_TITLE_ROWS, TITLE_BOX_OVERHEAD).


# Lines show_titles prints around the box itself: top border, bottom
# border, and the optional "… and N more" truncation footer. Callers add
# this to whatever chrome *they* print so the responsive cap stays honest.
TITLE_BOX_OVERHEAD = 3
MIN_TITLE_ROWS = 4


def colorize_title(title: str) -> str:
    """Color the leading [poster] cyan; leave the rest of the title alone."""
    match = POSTER_RE.match(title)
    if not match:
        return title
    return f"{C_CYN}{match.group(0)}{C_OFF}{title[match.end():]}"


def colorize_picker_label(label: str, width: int = 0) -> str:
    """Color a group label: poster brackets cyan, '+' joiner dim. Pads to `width`
    visible chars (counted on the plain text, not the ANSI-colored output)."""
    parts = label.split("+")
    colored: list[str] = []
    for part in parts:
        if part.startswith("[") and part.endswith("]"):
            colored.append(f"{C_CYN}{part}{C_OFF}")
        else:
            colored.append(part)
    out = f"{C_DIM}+{C_OFF}".join(colored)
    pad = max(0, width - len(label))
    return out + (" " * pad)


def category_chip(item: Item) -> str:
    return f"{C_DIM}{item.category}{C_OFF}" if item.category else ""


def _grade_seeders(seeders: int) -> str:
    if seeders >= 20:
        return C_GRN
    if seeders >= 5:
        return C_YEL
    return C_RED


def _grade_downloads(downloads: int) -> str:
    if downloads >= 500:
        return C_GRN
    if downloads >= 50:
        return C_YEL
    return C_DIM


def format_stats(item: Item) -> str:
    """One-line colored stats: 530 dl · 45s/5l · 926.7 MiB.

    Each segment asks anirss_lib.responsive whether it's visible at the
    current terminal width / config — see responsive.show() to add new
    rules or change thresholds.
    """
    from anirss_lib import responsive  # local to break import cycle
    parts: list[str] = []
    if responsive.show("downloads"):
        dl_c = _grade_downloads(item.downloads)
        parts.append(f"{dl_c}{item.downloads:>5} dl{C_OFF}")
    if responsive.show("seeders"):
        s_c = _grade_seeders(item.seeders)
        seed = f"{s_c}{item.seeders:>4}{C_OFF}{C_DIM}s{C_OFF}"
        if responsive.show("leechers"):
            seed += f"/{C_RED}{item.leechers:>4}{C_OFF}{C_DIM}l{C_OFF}"
        parts.append(seed)
    if item.size and responsive.show("size"):
        parts.append(f"{C_DIM}{item.size:>10}{C_OFF}")
    return f"  {C_DIM}·{C_OFF}  ".join(parts)


def show_titles(items: list[Item], cap: int | None = None,
                *, reserved_lines: int = 0) -> None:
    """Render up to `cap` items inside a bordered box.

    If `cap` is None, derive it from the live terminal height: the caller
    passes `reserved_lines` for the chrome it knows about (picker height,
    headers, Query line, etc.), and we add `TITLE_BOX_OVERHEAD` for the
    two borders + possible truncation footer. Result is clamped to at
    least `MIN_TITLE_ROWS` so a tiny terminal still shows something.
    """
    from anirss_lib import responsive  # local to break import cycle
    term = terminal.get_size()
    if cap is None:
        budget = term.lines - reserved_lines - TITLE_BOX_OVERHEAD
        cap = max(MIN_TITLE_ROWS, budget)
    width = term.columns
    show_box = responsive.show("title-box")
    show_cat = responsive.show("category")
    if show_box:
        inner = max(40, width - 4)  # 4 = "│ " + " │"
        bar = "─" * (inner + 2)
        print(f"{C_DIM}╭{bar}╮{C_OFF}")
    else:
        inner = width
    for item in items[:cap]:
        cat = category_chip(item) if (show_cat and item.category) else ""
        cat_w = len(item.category) + 1 if cat else 0  # +1 for gap
        body_max = max(10, inner - cat_w)
        body = truncate_ansi(f"{format_stats(item)}  {colorize_title(item.title)}", body_max)
        if cat:
            line = right_anchor(body, cat, inner)
        else:
            line = body + " " * max(0, inner - len(ansi_strip(body)))
        if show_box:
            print(f"{C_DIM}│{C_OFF} {line} {C_DIM}│{C_OFF}")
        else:
            print(line)
    if show_box:
        print(f"{C_DIM}╰{bar}╯{C_OFF}")
    if len(items) > cap:
        more = len(items) - cap
        print(f"  {C_DIM}... and {more} more "
              f"({C_OFF}{C_YEL}use [show all] in the picker{C_OFF}{C_DIM}){C_OFF}")
