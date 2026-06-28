"""Interactive refinement loop and its building blocks."""

import re
import signal
import sys
from collections import Counter, defaultdict
from statistics import median

from anirss_lib.ansi import (
    C_BLD, C_CYN, C_DIM, C_GRN, C_OFF, C_RED, C_YEL, ansi_strip,
)
from anirss_lib import bestfit, responsive, terminal
from anirss_lib.config import BestfitConfig, SearchConfig
from anirss_lib.format import colorize_picker_label, show_titles
from anirss_lib.fzf import fzf_pick_with_query, view_all_titles
from anirss_lib.logging import log
from anirss_lib.nyaa import fetch_items
from anirss_lib.readline_input import prompt
from anirss_lib.titles import RES_RE, poster_of, title_tokens
from anirss_lib.types import (
    Group, Item, PICK_BACK, PICK_BEST_FIT, PICK_DONE, PICK_EXCLUDE,
    PICK_SHOW_ALL, Pick,
)


DONE = "[→ Continue To Actions]"
EXCLUDE = "[✗ Exclude Term…]"
BEST_FIT = "[★ Try Best Fit]"


# --- SIGWINCH redraw of the title list -------------------------------
# fzf gets its own SIGWINCH and redraws the picker half on its own.
# We re-render the top half (title list + Query line) so the box borders
# track the new terminal width. Clear is scoped to the top half so we
# don't trample fzf's overlay below.

_CURRENT_FRAME: dict | None = None
_PREV_WINCH_HANDLER = None
_REDRAWING = False


def _redraw_top_half() -> None:
    """Reprint the title list area at the current terminal size."""
    global _REDRAWING
    if _CURRENT_FRAME is None or _REDRAWING:
        return
    _REDRAWING = True
    try:
        selected = _CURRENT_FRAME["selected"]
        query = _CURRENT_FRAME["query"]
        no_groups = _CURRENT_FRAME["no_groups"]
        cap, _ = responsive.split_view()
        lines = terminal.get_size().lines
        # With picker --height 50%, fzf owns the bottom half. The top half
        # is everything above. Clear those rows only, then redraw.
        top_h = max(1, lines - lines // 2)
        sys.stdout.write("\x1b[H")
        for _ in range(top_h):
            sys.stdout.write("\x1b[2K\x1b[1B")
        sys.stdout.write("\x1b[H")
        sys.stdout.flush()
        print()
        print(f"{C_BLD}{len(selected)} result(s):{C_OFF}")
        show_titles(selected, cap=cap)
        if no_groups:
            print(f"{C_DIM}(no token refinements left — exclude a term or finalize){C_OFF}")
        print()
        print(f"{C_BLD}Query:{C_OFF} {query}")
        sys.stdout.flush()
    except Exception:
        # Signal handlers must never raise.
        pass
    finally:
        _REDRAWING = False


def _on_sigwinch(signum, frame) -> None:
    _redraw_top_half()


def auto_resolution(query: str, items: list[Item]) -> tuple[str, list[Item]]:
    """If query lacks a <n>p token and every item has one, append the highest."""
    if RES_RE.search(query):
        return query, items
    per_max = [
        max((int(match.group(1)) for match in RES_RE.finditer(item.title)),
            default=None)
        for item in items
    ]
    missing = sum(1 for res in per_max if res is None)
    if missing:
        print(f"{C_DIM}[skipping auto-resolution: {missing}/{len(items)} "
              f"title(s) lack a <n>p token]{C_OFF}")
        log("INFO", f"skip auto-resolution: {missing} titles lack one")
        return query, items
    highest = max(res for res in per_max if res is not None)
    token = f"{highest}p"
    print(f"{C_YEL}[auto-appending highest available resolution: {token}]{C_OFF}")
    log("INFO", f"auto-append {token} (max across {len(items)} titles)")
    new_items = [item for item, res in zip(items, per_max) if res == highest]
    return f"{query} {token}", new_items


def _members_of(selected: list[Item], token: str) -> frozenset[int]:
    return frozenset(
        i for i, item in enumerate(selected)
        if (token.startswith("[") and poster_of(item.title) == token)
        or (not token.startswith("[") and token in title_tokens(item.title))
    )


def compute_groups(selected: list[Item]) -> list[Group]:
    """Find tokens that partition `selected` into refinable subsets, grouped by membership."""
    total = len(selected)
    counts: Counter[str] = Counter()
    for item in selected:
        seen: set[str] = set()
        poster = poster_of(item.title)
        if poster:
            seen.add(poster)
        for token in title_tokens(item.title):
            seen.add(token)
        for token in seen:
            counts[token] += 1

    refinable = [token for token, count in counts.items() if 1 < count < total]

    by_members: dict[frozenset[int], list[str]] = defaultdict(list)
    for token in refinable:
        by_members[_members_of(selected, token)].append(token)

    groups: list[Group] = []
    for members, tokens in by_members.items():
        tokens_sorted = sorted(tokens, key=lambda x: (not x.startswith("["),
                                                     -len(x), x))
        label = "+".join(tokens_sorted)
        has_poster = any(token.startswith("[") for token in tokens)
        groups.append(Group(label, tokens_sorted, len(members), has_poster))

    groups.sort(key=lambda g: (not g.has_poster, -g.member_count, g.label))
    return groups


def pick_group(groups: list[Group], selected: list[Item],
               *, height: str | None = None) -> Pick:
    """Show the picker. Return a Pick (kind=tokens|done|exclude|show_all|custom).

    Typing text + Enter with no fzf match becomes Pick("custom", [text]) so the
    caller can decide whether to fold the text into the nyaa query. The
    `height` override (e.g. "50%") is passed straight through to fzf's
    --height so the picker auto-resizes on SIGWINCH.
    """
    n_results = len(selected)
    show_all_label = f"[≡ Show All {n_results} Titles]"
    label_width = 28
    options = [
        f"{C_CYN}{BEST_FIT}{C_OFF}",
        f"{C_YEL}{show_all_label}{C_OFF}",
        f"{C_GRN}{DONE}{C_OFF}",
        f"{C_RED}{EXCLUDE}{C_OFF}",
    ]
    options += [
        f"{colorize_picker_label(g.label, label_width)} {C_DIM}({g.member_count}){C_OFF}"
        for g in groups
    ]
    header = (
        f"{n_results} results — pick a token, type to filter "
        f"(adds to query if no match) · {C_BLD}Esc{C_OFF} → back to search · "
        f"{C_BLD}Ctrl-C{C_OFF} quits"
    )
    query, choice, cancelled = fzf_pick_with_query(options, header, height=height)
    if cancelled:
        return PICK_BACK
    if choice is None:
        if query:
            log("INFO", f"custom-filter typed: {query!r}")
            return Pick("custom", [query])
        return PICK_DONE
    choice_plain = ansi_strip(choice)
    if choice_plain == DONE:
        return PICK_DONE
    if choice_plain == EXCLUDE:
        return PICK_EXCLUDE
    if choice_plain == BEST_FIT:
        return PICK_BEST_FIT
    if choice_plain == show_all_label:
        return PICK_SHOW_ALL
    chosen_label = choice_plain.rsplit(" (", 1)[0].rstrip()
    chosen = next((g for g in groups if g.label == chosen_label), None)
    if chosen is None:
        return PICK_DONE
    log("INFO", f"picked {chosen_label!r} -> tokens {chosen.tokens}")
    return Pick("tokens", chosen.tokens)


# Split a query into fields, keeping a `"quoted phrase"` (with an optional
# leading `-`) as one field so rebuilding round-trips the original quoting.
_FIELD_RE = re.compile(r'-?"[^"]*"|\S+')


def _field_text(field: str) -> str:
    """The matchable core of a query field: drop a leading `-` and any quotes."""
    core = field[1:] if field.startswith("-") else field
    if len(core) >= 2 and core[0] == '"' and core[-1] == '"':
        core = core[1:-1]
    return core.lower()


def _title_position(text: str, selected: list[Item]) -> float:
    """Median index at which `text` appears across the selected titles.

    inf when no title contains it. Using the median (rather than one sample)
    keeps the ordering stable when release groups format titles differently.
    """
    spots = [
        item.title.lower().index(text)
        for item in selected
        if text and text in item.title.lower()
    ]
    return median(spots) if spots else float("inf")


def _insert_positional(query: str, token: str, selected: list[Item]) -> str:
    """Slot `token` into `query` ahead of the first existing field that occurs
    *later* in the titles, so the query mirrors real release-name order (e.g.
    1080p before multisub) instead of being blindly appended. Posters are never
    crossed; exclusions (`-tag`) always stay at the tail.
    """
    fields = _FIELD_RE.findall(query)
    token_pos = _title_position(token.lower(), selected)
    insert_at = len(fields)
    for i, field in enumerate(fields):
        if field.startswith("["):          # never slot in front of a poster
            continue
        if field.startswith("-"):          # exclusions stay at the end
            insert_at = i
            break
        if _title_position(_field_text(field), selected) > token_pos:
            insert_at = i
            break
    fields.insert(insert_at, token)
    return " ".join(fields)


def build_refined_query(query: str, tokens: list[str], selected: list[Item]
                        ) -> str:
    """Extend `query` with `tokens`, each placed where it belongs.

    A poster (`[...]`) leads the title, so it goes to the front unless the query
    already starts with one. Every other token is positioned by where it occurs
    in the titles (see `_insert_positional`). Returns the rebuilt query string;
    the caller refetches nyaa with it rather than filtering locally, so episodes
    beyond the first RSS page can surface.
    """
    new_query = query
    for token in tokens:
        if token.startswith("["):
            if not new_query.lstrip().startswith("["):
                new_query = f"{token} {new_query}"
            continue
        new_query = _insert_positional(new_query, token, selected)
    return new_query


def add_exclude_to_query(query: str, term: str) -> str:
    """Append nyaa-style -tag to query. Returns query unchanged if term is empty after stripping."""
    term = term.lstrip("-").strip()
    if not term:
        return query
    flag = f'-"{term}"' if " " in term else f"-{term}"
    return f"{query} {flag}"


def add_term_to_query(query: str, term: str) -> str:
    """Append a positive search term to query, quoting if it contains spaces."""
    term = term.strip()
    if not term:
        return query
    flag = f'"{term}"' if " " in term else term
    return f"{query} {flag}"


def refine(initial_query: str, items: list[Item], search: SearchConfig,
           bestfit_cfg: BestfitConfig) -> tuple[str, list[Item], str]:
    """Return (final_query, list of Items, status) after interactive refinement.
    `status` is "done" if the user finalized, or "back" if Esc was pressed to
    return to the search step. Caller must ensure `items` is non-empty.

    Installs a SIGWINCH handler while the loop runs so resizing the terminal
    repaints the title list in real time without waiting for the next pick.
    """
    global _CURRENT_FRAME, _PREV_WINCH_HANDLER
    query = initial_query.strip()
    log("INFO", f"refine start: {query!r} ({len(items)} items)")
    query, selected = auto_resolution(query, items)

    _PREV_WINCH_HANDLER = signal.signal(signal.SIGWINCH, _on_sigwinch)
    try:
        return _refine_loop(query, selected, search, bestfit_cfg)
    finally:
        _CURRENT_FRAME = None
        if _PREV_WINCH_HANDLER is not None:
            signal.signal(signal.SIGWINCH, _PREV_WINCH_HANDLER)
        _PREV_WINCH_HANDLER = None


def _refine_loop(query: str, selected: list[Item], search: SearchConfig,
                 bestfit_cfg: BestfitConfig) -> tuple[str, list[Item], str]:
    """The original refine loop body; extracted so refine() can wrap it
    in signal-handler setup/teardown via try/finally.
    """
    global _CURRENT_FRAME
    while True:
        terminal.clear_screen()
        cap, picker_height_spec = responsive.split_view()
        count = len(selected)
        print()
        print(f"{C_BLD}{count} result(s):{C_OFF}")
        show_titles(selected, cap=cap)

        groups = compute_groups(selected)
        if not groups:
            print(f"{C_DIM}(no token refinements left — exclude a term or finalize){C_OFF}")

        # Query line sits immediately above the picker so the user can see what
        # they're filtering against without scanning back to the top.
        print()
        print(f"{C_BLD}Query:{C_OFF} {query}")
        # Capture the current frame so the SIGWINCH handler can repaint it.
        _CURRENT_FRAME = {
            "selected": selected,
            "query": query,
            "no_groups": not groups,
        }
        pick = pick_group(groups, selected, height=picker_height_spec)
        if pick.kind == "done":
            break
        if pick.kind == "back":
            log("INFO", "refine: esc → back to search")
            return query, selected, "back"
        if pick.kind == "show_all":
            view_all_titles(selected)
            continue
        if pick.kind == "best_fit":
            best = bestfit.best_item(selected, bestfit_cfg)
            if best is None:
                continue
            tokens = [t for t in bestfit.profile_tokens(best)
                      if t.lower() not in query.lower()]
            if not tokens:
                print(f"{C_DIM}already at the best fit for these results{C_OFF}")
                log("INFO", "best-fit: query already pins the top profile")
                continue
            new_query = build_refined_query(query, tokens, selected)
            print(f"{C_CYN}best fit:{C_OFF} {' '.join(tokens)}")
            print(f"{C_DIM}refetching nyaa with {new_query!r}...{C_OFF}")
            new_items = fetch_items(new_query, search)
            if not new_items:
                print(f"{C_YEL}best fit returns 0 results — skipped{C_OFF}")
                log("WARN", f"best-fit {tokens!r} → 0 results — reverted")
                continue
            delta = len(new_items) - len(selected)
            print(f"{C_YEL}nyaa returned {len(new_items)} (was {len(selected)}, "
                  f"{delta:+d}){C_OFF}")
            query, selected = new_query, new_items
            log("INFO", f"after best-fit {tokens!r}: {len(selected)} results, query={query!r}")
            continue
        if pick.kind == "custom":
            term = pick.tokens[0]
            term_lc = term.lower()
            hits = sum(1 for item in selected if term_lc in item.title.lower())
            if hits == 0:
                print(f"{C_YEL}no titles contain {term!r} — ignored{C_OFF}")
                log("WARN", f"custom filter {term!r}: 0 substring matches in current results")
                continue
            new_query = add_term_to_query(query, term)
            print(f"{C_DIM}refetching nyaa with {new_query!r}...{C_OFF}")
            new_items = fetch_items(new_query, search)
            if not new_items:
                print(f"{C_YEL}filter would yield 0 results — skipped{C_OFF}")
                log("WARN", f"after custom {term!r}: 0 results — reverted")
                continue
            removed = len(selected) - len(new_items)
            print(f"{C_YEL}filtered — nyaa returned {len(new_items)} (was {len(selected)}, "
                  f"{removed:+d}){C_OFF}")
            query, selected = new_query, new_items
            log("INFO", f"after custom {term!r}: {len(selected)} results, query={query!r}")
            continue
        if pick.kind == "exclude":
            term = prompt("Exclude term: ", history="exclude")
            new_query = add_exclude_to_query(query, term)
            if new_query == query:
                print(f"{C_DIM}empty — skipped{C_OFF}")
                continue
            print(f"{C_DIM}refetching nyaa with {new_query!r}...{C_OFF}")
            new_items = fetch_items(new_query, search)
            if not new_items:
                print(f"{C_YEL}exclude would yield 0 results — skipped{C_OFF}")
                log("WARN", f"after exclude {term!r}: 0 results — reverted")
                continue
            removed = len(selected) - len(new_items)
            print(f"{C_YEL}excluded — nyaa returned {len(new_items)} (was {len(selected)}, "
                  f"{removed:+d}){C_OFF}")
            query, selected = new_query, new_items
            log("INFO", f"after exclude {term!r}: {len(selected)} results, query={query!r}")
            continue

        # Picking a token refetches nyaa with the extended query (like the
        # custom/exclude paths) instead of filtering the already-fetched page.
        # The first RSS page only holds the most-recent matches, so a narrower
        # query surfaces episodes the broad search never returned.
        new_query = build_refined_query(query, pick.tokens, selected)
        print(f"{C_DIM}refetching nyaa with {new_query!r}...{C_OFF}")
        new_items = fetch_items(new_query, search)
        if not new_items:
            print(f"{C_YEL}that filter returns 0 results — skipped{C_OFF}")
            log("WARN", f"tokens {pick.tokens!r} → 0 results — skipped")
            continue
        delta = len(new_items) - len(selected)
        print(f"{C_YEL}nyaa returned {len(new_items)} (was {len(selected)}, "
              f"{delta:+d}){C_OFF}")
        query, selected = new_query, new_items
        log("INFO", f"after pick {pick.tokens!r}: {len(selected)} results, query={query!r}")

    print()
    print(f"{C_GRN}{C_BLD}Final query:{C_OFF} {query}")
    return query, selected, "done"
