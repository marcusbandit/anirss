"""Interactive pickers for action / downloads / movie. Skipped in non-interactive mode."""

import shutil
import subprocess

from anirss_lib.ansi import (
    C_BLD, C_CYN, C_DIM, C_GRN, C_MAG, C_OFF, C_RED, C_YEL,
    FZF_BINDS, FZF_HL_COLORS, PROMPT_ACTION,
    ansi_strip, right_anchor,
)
from anirss_lib import terminal
from anirss_lib.format import category_chip, colorize_title, format_stats
from anirss_lib.fzf import _parse_fzf_output, fzf_pick_one
from anirss_lib.types import Item


ACT_SUB, ACT_DL_PICK, ACT_DL_ALL, ACT_MOVIE, ACT_CANCEL, ACT_BACK = (
    "subscribe", "download_pick", "download_all", "movie", "cancel", "back",
)


def pick_action(n_items: int) -> str:
    rows: list[tuple[str, str, str, str]] = [
        (C_CYN, " Subscribe", "recurring RSS feed",                ACT_SUB),
        (C_GRN, " Download",     "pick which items to download",          ACT_DL_PICK),
        (C_GRN, " Download All", f"all {n_items} matching item(s) now",   ACT_DL_ALL),
        (C_MAG, " Movie",     "pick one from the list",            ACT_MOVIE),
        (C_RED, " Cancel",    "",                                  ACT_CANCEL),
    ]
    name_w = max(len(name) for _, name, _, _ in rows) + 2
    labels: list[str] = []
    by_key: dict[str, str] = {}
    for color, name, desc, act in rows:
        text = f"{color}{name:<{name_w}}{C_OFF}"
        if desc:
            text += f"{C_DIM}— {desc}{C_OFF}"
        labels.append(text)
        by_key[ansi_strip(text).strip()] = act
    header = (
        f"{C_BLD}Choose an action{C_OFF} · {C_BLD}Esc{C_OFF} → back to filter · "
        f"{C_BLD}Ctrl-C{C_OFF} quits"
    )
    choice = fzf_pick_one(labels, header, prompt_label=PROMPT_ACTION)
    if choice is None:
        return ACT_BACK
    return by_key.get(ansi_strip(choice).strip(), ACT_CANCEL)


def pick_downloads(items: list[Item]) -> list[Item]:
    """Multi-select picker. Tab/Space/Enter mark a row and step down. Picking
    the [Done] row at the top accepts the marks. Esc cancels.
    """
    if not shutil.which("fzf") or not items:
        return []
    width = max(40, terminal.get_size().columns - 3)
    sorted_items = sorted(items, key=lambda i: i.downloads, reverse=True)

    sentinel = f"DONE\t  {C_GRN}{C_BLD}[Done — confirm selection]{C_OFF}"
    lines: list[str] = [sentinel]
    by_key: dict[str, Item] = {}
    for item in sorted_items:
        body = f"{format_stats(item)}  {colorize_title(item.title)}"
        if item.category:
            body = right_anchor(body, category_chip(item), width)
        line = f"ITM\t{body}"
        lines.append(line)
        by_key[ansi_strip(line).strip()] = item

    header = (
        f"{len(items)} item(s) — "
        f"{C_YEL}{C_BLD}Tab/Space/Enter{C_OFF} marks · "
        f"go to {C_GRN}{C_BLD}Done{C_OFF} to confirm · "
        f"{C_DIM}Esc cancels{C_OFF}"
    )
    binds = FZF_BINDS + (
        ",tab:toggle+down,space:toggle+down,"
        "enter:transform:[ {1} = DONE ] && echo accept || echo toggle+down"
    )
    args = [
        "fzf",
        "--ansi", "--multi",
        "--color", FZF_HL_COLORS,
        "--delimiter", "\t",
        "--with-nth", "2..",
        "--nth", "2..",
        "--bind", binds,
        "--prompt", "select > ",
        "--header", header,
        "--marker", "▌",
        "--info", "inline",
        "--layout=reverse",
        "--height", "80%",
        "--cycle",
        "--preview-window=hidden",
        "--expect", "ctrl-c",
    ]
    proc = subprocess.run(args, input="\n".join(lines), text=True,
                          stdout=subprocess.PIPE)
    out = _parse_fzf_output(proc.stdout or "", print_query=False, expect=True)
    if proc.returncode != 0:
        return []
    chosen: list[Item] = []
    for raw in out.selections:
        item = by_key.get(ansi_strip(raw).strip())
        if item is not None:
            chosen.append(item)
    return chosen


def pick_endpoint(state) -> "Endpoint | None":
    """Switch the active endpoint. With two configured, just cycle; with
    more, open a small fzf pick. Returns the new active endpoint (state is
    mutated), or None when cancelled/unchanged/nothing to switch to."""
    if len(state.endpoints) < 2:
        return None
    if len(state.endpoints) == 2:
        return state.cycle()
    options = [
        f"{e.name} (active)" if e is state.active else e.name
        for e in state.endpoints
    ]
    choice = fzf_pick_one(options, "switch endpoint", prompt_label="endpoint > ")
    if choice is None:
        return None
    ep = state.by_name(choice.removesuffix(" (active)"))
    if ep is None or ep is state.active:
        return None
    state.active = ep
    return ep


def pick_movie(items: list[Item]) -> Item | None:
    width = max(40, terminal.get_size().columns - 3)
    sorted_items = sorted(items, key=lambda i: i.downloads, reverse=True)
    lines: list[str] = []
    by_key: dict[str, Item] = {}
    for item in sorted_items:
        line = f"{format_stats(item)}  {colorize_title(item.title)}"
        if item.category:
            line = right_anchor(line, category_chip(item), width)
        lines.append(line)
        by_key[ansi_strip(line).strip()] = item
    pick = fzf_pick_one(lines, f"pick one of {len(items)} item(s) to save as a movie")
    if pick is None:
        return None
    return by_key.get(ansi_strip(pick).strip())
