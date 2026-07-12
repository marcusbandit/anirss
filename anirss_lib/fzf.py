"""fzf process wrappers + the structured output parser they share."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from anirss_lib.ansi import (
    C_BLD, C_OFF,
    FILTER_PICKER_LINES, FZF_BINDS, FZF_HL_COLORS,
    PROMPT_FILTER,
    ansi_strip, right_anchor,
)
from anirss_lib import terminal
from anirss_lib.format import category_chip, colorize_title, format_stats
from anirss_lib.logging import log
from anirss_lib.types import Item


class FzfOutput(NamedTuple):
    query: str             # empty when --print-query wasn't requested
    expect_key: str        # empty when not pressed or --expect wasn't requested
    selections: list[str]  # selected lines (post any expect/query prefixes)


def _parse_fzf_output(stdout: str, *, print_query: bool, expect: bool) -> FzfOutput:
    """Decode fzf's stdout layout.

    Layout:
      line 0:           query (if --print-query) OR expect-key (if --expect) OR selection
      line 1 (if both): expect-key
      remaining lines:  selections

    Always promotes a literal `ctrl-c` expect-key into sys.exit(130).
    """
    lines = stdout.split("\n") if stdout else [""]
    query = ""
    expect_key = ""
    idx = 0
    if print_query:
        query = lines[idx] if idx < len(lines) else ""
        idx += 1
    if expect:
        expect_key = lines[idx].strip() if idx < len(lines) else ""
        idx += 1
    selections = [ln for ln in lines[idx:] if ln]
    if expect_key == "ctrl-c":
        log("INFO", "ctrl-c — terminating")
        sys.exit(130)
    return FzfOutput(query=query, expect_key=expect_key, selections=selections)


def _history_path(key: str) -> Path:
    return Path(f"~/.local/state/anirss/{key}.history").expanduser()


def fzf_pick_one(options: list[str], header: str, *,
                 prompt_label: str = "filter > ") -> str | None:
    if not shutil.which("fzf") or not options:
        return None
    height_lines = min(max(len(options) + 4, 6), FILTER_PICKER_LINES)
    args = [
        "fzf", "--ansi",
        "--color", FZF_HL_COLORS,
        "--bind", FZF_BINDS,
        "--prompt", prompt_label,
        "--header", header,
        "--layout=reverse",
        "--height", str(height_lines),
        "--cycle", "--no-info",
        "--preview-window=hidden",
        "--expect", "ctrl-c",
    ]
    proc = subprocess.run(
        args, input="\n".join(options), text=True, stdout=subprocess.PIPE,
    )
    out = _parse_fzf_output(proc.stdout or "", print_query=False, expect=True)
    if proc.returncode != 0 or not out.selections:
        return None
    return out.selections[0].strip() or None


def fzf_pick_with_query(options: list[str], header: str,
                        *, height: str | None = None,
                        prompt_label: str = PROMPT_FILTER,
                        extra_expect: str = "",
                        ) -> tuple[str, str | None, bool, str]:
    """Run fzf with --print-query.

    Returns (typed_query, selected_choice_or_None, cancelled, expect_key).
    cancelled=True means esc/ctrl-c: caller should treat as "done".
    cancelled=False with choice=None means Enter was pressed with no fzf match
    (custom-filter attempt). expect_key is the key that ended the session
    ("" for plain Enter, e.g. "ctrl-e" when `extra_expect` requested it).

    `height` is passed verbatim to fzf's --height (so callers can use "50%"
    or an absolute "24"). When omitted, auto-fit to the option count and
    clamp at FILTER_PICKER_LINES. `prompt_label` overrides the default filter
    prompt. `extra_expect` adds additional --expect keys beyond ctrl-c
    (comma-separated, e.g. "ctrl-e").
    """
    if not shutil.which("fzf") or not options:
        return "", None, True, ""
    if height is not None:
        final_height = height
    else:
        final_height = str(min(max(len(options) + 4, 6), FILTER_PICKER_LINES))
    args = [
        "fzf", "--ansi",
        "--color", FZF_HL_COLORS,
        "--bind", FZF_BINDS,
        "--prompt", prompt_label,
        "--header", header,
        "--layout=reverse",
        "--height", final_height,
        "--cycle", "--no-info",
        "--print-query",
        # Don't grab the mouse: tmux/the terminal keeps it, so the Query line
        # printed above the picker stays drag-selectable (copy/paste). The
        # tradeoff is no click-to-pick on options, arrow keys + Enter still work.
        "--no-mouse",
        "--preview-window=hidden",
        "--expect", "ctrl-c" + (f",{extra_expect}" if extra_expect else ""),
    ]
    proc = subprocess.run(
        args, input="\n".join(options), text=True, stdout=subprocess.PIPE,
    )
    out = _parse_fzf_output(proc.stdout or "", print_query=True, expect=True)
    query = out.query.strip()
    if proc.returncode == 0:
        choice = out.selections[0].strip() if out.selections else ""
        return query, (choice or None), False, out.expect_key
    if proc.returncode == 1:
        # Enter pressed with no match: treat query as a custom filter.
        return query, None, False, out.expect_key
    return "", None, True, ""


def fzf_search_prompt(prompt_label: str, *, default: str = "",
                      endpoint_name: str = "", switch_hint: bool = False,
                      ) -> tuple[str | None, str]:
    """Live nyaa search via fzf. Returns (query_or_None, key) with key in
    {"enter", "esc", "ctrl-e"}. Enter with a non-empty query returns
    (query, "enter"); empty-query Enter and Esc both return (None, "esc");
    Ctrl-E returns the currently typed text (possibly empty) and "ctrl-e" so
    the caller can keep it. Ctrl-C terminates the entire process. Items
    refresh ~0.5 s after typing pauses, driven by the hidden
    ``--_search-rss`` self-invocation.

    `endpoint_name`, when non-empty, is threaded through to the reload
    commands as `--_endpoint <name>` so the live search hits the active
    endpoint instead of the first configured one, and is named in the
    header. `switch_hint` appends the Ctrl-E endpoint-switch hint to the
    header when there's more than one endpoint to switch between.
    """
    from anirss_lib.readline_input import prompt as readline_prompt
    history_file = _history_path("search")
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    if not shutil.which("fzf"):
        q = readline_prompt(prompt_label, history="search") or None
        return q, ("enter" if q else "esc")

    script_path = shutil.which(sys.argv[0]) or os.path.abspath(sys.argv[0])
    quoted = shlex.quote(script_path)
    # `sleep 0.5` debounces: fzf cancels in-flight reloads on every keystroke,
    # so the actual fetch only runs once typing pauses for ~0.5 s.
    ep_flag = f"--_endpoint {shlex.quote(endpoint_name)} " if endpoint_name else ""
    search_cmd = f"sleep 0.5 && {quoted} --_search-rss {ep_flag}{{q}}"
    initial_cmd = f"{quoted} --_search-rss {ep_flag}{{q}}"

    header = (
        f"type to search {endpoint_name or 'nyaa'} · Stop typing for a sec to refresh · "
        f"{C_BLD}Enter{C_OFF} confirms · {C_BLD}Esc{C_OFF} back · {C_BLD}Ctrl-C{C_OFF} quits"
    )
    if switch_hint:
        header += f" · {C_BLD}Ctrl-E{C_OFF} endpoint"

    fzf_args = [
        "fzf", "--ansi",
        "--disabled",        # nyaa does the filtering; fzf only renders.
        "--no-mouse",        # no clicking on items either.
        "--print-query",
        "--expect", "ctrl-c,enter,ctrl-e",  # Enter/Ctrl-E are exit-keys, never select an item.
        "--color", FZF_HL_COLORS,
        "--prompt", prompt_label,
        "--query", default,
        "--layout=reverse",
        "--header-first",    # in reverse layout, this puts the header at the top.
        "--height", "90%",
        "--no-info",
        "--preview-window=hidden",
        "--header", header,
        "--history", str(history_file),
        "--bind", FZF_BINDS,
        "--bind", f"change:reload({search_cmd})",
        # On terminal resize, re-run the search so each row is reformatted
        # to the new column width (no debounce — explicit user action).
        "--bind", f"resize:reload({initial_cmd})",
    ]
    if default:
        fzf_args.extend(["--bind", f"start:reload({initial_cmd})"])

    proc = subprocess.run(fzf_args, input="", text=True, stdout=subprocess.PIPE)
    out = _parse_fzf_output(proc.stdout or "", print_query=True, expect=True)
    q = out.query.strip()
    if out.expect_key == "ctrl-e":
        return q, "ctrl-e"
    if out.expect_key == "enter" and q:
        try:
            with history_file.open("a") as f:
                f.write(q + "\n")
        except OSError:
            pass
        return q, "enter"
    return None, "esc"


def view_all_titles(items: list[Item]) -> None:
    """Read-only fzf viewer over `items`. Each row is multi-line: stats and
    category stay anchored on the first line, and the title wraps onto
    subsequent lines indented under itself instead of being truncated. Esc or
    Enter returns to the caller; Ctrl-C terminates the whole process.
    """
    if not shutil.which("fzf") or not items:
        return
    import textwrap
    width = max(40, terminal.get_size().columns - 3)

    # Compute alignment widths. format_stats colors its content; measure on the
    # plain (ANSI-stripped) form so wrapping matches what the eye will see.
    stats_w = max(len(ansi_strip(format_stats(it))) for it in items)
    cat_w = max((len(it.category) for it in items if it.category), default=0)
    cat_pad = (cat_w + 2) if cat_w else 0
    prefix_w = stats_w + 2
    name_w = max(20, width - prefix_w - cat_pad)

    blocks: list[str] = []
    for item in items:
        chunks = textwrap.wrap(item.title, width=name_w) or [""]
        first_chunk = colorize_title(chunks[0])
        first_body = f"{format_stats(item)}  {first_chunk}"
        first_line = (
            right_anchor(first_body, category_chip(item), width)
            if item.category else first_body
        )
        block_lines = [first_line]
        indent = " " * prefix_w
        for chunk in chunks[1:]:
            block_lines.append(f"{indent}{chunk}")
        blocks.append("\n".join(block_lines))

    args = [
        "fzf", "--ansi",
        "--read0", "--multi-line",
        "--color", FZF_HL_COLORS,
        "--bind", FZF_BINDS,
        "--prompt", "view > ",
        "--header",
        f"all {len(items)} titles · {C_BLD}Esc/Enter{C_OFF} to return · "
        f"{C_BLD}Ctrl-C{C_OFF} quits",
        "--layout=reverse",
        "--height", "90%",
        "--cycle", "--no-info",
        "--preview-window=hidden",
        "--expect", "ctrl-c",
    ]
    proc = subprocess.run(
        args, input="\0".join(blocks) + "\0", text=True, stdout=subprocess.PIPE,
    )
    _parse_fzf_output(proc.stdout or "", print_query=False, expect=True)
