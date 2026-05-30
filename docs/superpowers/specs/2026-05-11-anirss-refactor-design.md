# anirss refactor: split into modules, fix terminal-size, add non-interactive mode

Status: approved 2026-05-11.
Replaces the single 2161-line `anirss` script with a launcher + `anirss_lib/`
Python package, fixes the terminal-size regression in the `--_search-rss`
reload subprocess, repairs three drifted tests, and adds flag-driven
non-interactive operation suitable for scripts and cron.

## Goals

1. Make the codebase navigable. Today everything is one file; cross-cutting
   changes (e.g. the qBittorrent 5.x compat work) touch unrelated lines and
   produce noisy diffs.
2. Fix the terminal-size bug: the `--_search-rss` reload subprocess that
   populates the live nyaa picker has piped stdout, so
   `shutil.get_terminal_size()` returns the `(120, 24)` fallback and the rows
   it prints are sized wrong on any other terminal.
3. Repair three `_make_sid_cookie` tests that broke when the qBittorrent 5.x
   change added a required `name` argument.
4. Allow full non-interactive operation: search → action without ever opening
   fzf, plus a script-safe way to provide the qBittorrent password.

## Non-goals

- Reworking the `refine()` state machine. It works; touching it is risky.
- Replacing `die()` with exceptions everywhere. Invasive, no clear win.
- argparse migration. Pacman-style flags are intentional UX.
- Adding a pyproject.toml / pipx packaging path. Out of scope; current
  channels (brew, AUR, install.sh) stay no-build.

## Constraints

- No build step. Brew, AUR, and `install.sh` must keep working without
  `python -m build`, pip, pipx, or a virtualenv.
- The on-PATH entry point stays named `anirss` (no extension).
- The test file's `SourceFileLoader` of the single script gets replaced with
  ordinary `import anirss_lib.*`. Tests stay runnable via `uvx pytest`.

## Layout

The single script becomes a launcher plus a sibling library directory.

```
anirss                       # ~5-line launcher, on PATH
anirss_lib/
├── __init__.py              # exposes __version__
├── ansi.py                  # color constants, fzf color/binds, ansi_strip,
│                            #   right_anchor, truncate_ansi
├── terminal.py              # get_size() — reads /dev/tty (fixes B1)
├── config.py                # AnirssConfig + TypedDicts, load_config,
│                            #   migrate_config, paths
├── logging.py               # init_log, log, die
├── types.py                 # Item, Group, Pick + PICK_* sentinels
├── nyaa.py                  # search_url, fetch_items, _fetch_items_from_url
├── titles.py                # poster_of, show_name, title_tokens, regex consts
├── format.py                # format_stats, colorize_*, category_chip,
│                            #   _grade_*, show_titles
├── fzf.py                   # _parse_fzf_output + the fzf_* wrappers and
│                            #   view_all_titles
├── refine.py                # refine, compute_groups, pick_group, apply_pick,
│                            #   auto_resolution, add_{exclude,term}_to_query
├── readline_input.py        # setup_readline, prompt, get_name
├── qbt/
│   ├── __init__.py
│   ├── session.py           # QbtSession, login flow, SID persistence
│   ├── feeds.py             # feed cache + qB rule listing
│   └── actions.py           # do_subscribe/do_download/do_movie,
│                            #   apply_no_seed, _norm_path, _human_bytes,
│                            #   _dir_size_bytes, _dir_file_count, _safe_rmtree
├── cli/
│   ├── __init__.py
│   ├── urls.py              # UrlKind enum + classify_url, extract_nyaa_query
│   ├── args.py              # parse_op_flag, parse_cli_args (new for §
│   │                        #   non-interactive flags)
│   ├── pickers.py           # pick_action, pick_downloads, pick_movie
│   └── commands.py          # cmd_query, cmd_sync, cmd_remove
└── main.py                  # main + _handle_meta_flags,
                             #   _handle_op_flag, _handle_url_arg,
                             #   _run_search_state_machine, _perform_action
```

The launcher script:

```python
#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from anirss_lib.main import main
main()
```

`os.path.realpath` resolves any symlink, so the launcher always finds its
sibling `anirss_lib/` regardless of whether it's invoked through
`/usr/bin/anirss -> /usr/lib/anirss/anirss` or `~/.local/bin/anirss ->
~/Projects/anirss/anirss`.

## Terminal-size fix (B1)

`anirss_lib/terminal.py`:

```python
import os, shutil

FALLBACK = os.terminal_size((120, 24))

def get_size() -> os.terminal_size:
    """Real terminal size, even when stdout is piped (the --_search-rss
    reload subprocess inherits piped stdout from fzf)."""
    try:
        with open("/dev/tty") as tty:
            return os.get_terminal_size(tty.fileno())
    except OSError:
        return shutil.get_terminal_size(FALLBACK)
```

All five `shutil.get_terminal_size((120, 24))` call sites become
`terminal.get_size()`. The `max(40, … - 3)` clamp stays at the call sites —
that's a layout concern, not a "what's the terminal" concern.

## SID-cookie test fix (B2)

When qBittorrent 5.x compat shipped, `_make_sid_cookie` gained a required
`name` argument. The three existing tests still call it with the old
2-positional shape and now fail with `TypeError`. Fix: pass `name` explicitly
in each test; assert the resulting Cookie carries the supplied name.

## cmd_remove double-fetch (B3)

`cmd_remove` calls `_torrents_for_savepath(qbt, save_path)` twice in the
`drop_files and not drop_torrents` branch — once for `torrents` (line
~1842), then again in the dict-literal for `torrent_count_in_qb`. The two
results are necessarily equal; drop the second call and simplify the dict
entry to `len(torrents)`.

## Bad-pattern fixes

- **P1 (`__force_action_menu__` sentinel)**: stop injecting that string back
  into `args[]`. `_handle_op_flag` returns a structured result that includes
  a `force_action_menu_url: str | None`; `main` threads it into
  `_handle_url_arg`.
- **P2 (duplicated fzf parsing)**: extract `_parse_fzf_output(stdout, *,
  expect_idx, print_query) -> FzfOutput`. `FzfOutput` exposes `query`,
  `expect_key`, `selections: list[str]`. Each fzf wrapper still builds its
  own `args[]` (they really do differ), but the post-call parsing converges.
- **P3 (240-line main)**: split as described in the layout section.
- **P4 (URL classification)**: `cli/urls.py` exports a small enum and one
  function:
  ```python
  class UrlKind(enum.Enum):
      NOT_URL = auto()
      ONE_SHOT = auto()      # magnet: or *.torrent
      NYAA_RSS = auto()
      OTHER_HTTP = auto()
  def classify_url(s: str) -> UrlKind: ...
  ```
  Call sites become `if kind is UrlKind.ONE_SHOT: …`.

## Non-interactive mode

### New flags

```
Action flags (each implies non-interactive: skips fzf and Name prompt):
  --subscribe              Subscribe to the query or URL
  --download-all           Download every matching item
  --download N             Download top N items by download count
  --movie                  Top-1 to movie_path

Identification (also useful interactively):
  --name NAME              Skip the Name: prompt

Password handling (used only when the SID cache misses):
  --password-stdin         Read one line from stdin
  ANIRSS_QBT_PASSWORD=…    Env var (recommended)
```

A CLI `--password PW` flag is intentionally **not** added; `ps aux` and
shell history both leak it.

### Behavior

Non-interactive mode is active when any of `--subscribe`,
`--download-all`, `--download`, `--movie` is set.

- No fzf invocations: search query → fetch → (auto_resolution) → act.
  `refine()` is skipped entirely.
- No Name prompt: use `--name NAME` if given, else
  `show_name(items[0].title)` if at least one item, else the query.
- `auto_resolution` still applies. It's not interactive; it just refines
  the query when every result has a `<n>p` token.
- 0 results → message to stderr + exit 1.
- For `--download N`, sort by `downloads` desc (mirrors `pick_downloads`)
  and slice `[:N]`. If `N > len(items)`, use all of them.
- For `--movie`, pick top-1 by `downloads`.
- For `--subscribe` with a URL arg via `-S`, fetch the URL once to derive
  the default name, then subscribe to it directly.
- Password: `login_with_retry` tries the SID cache first (unchanged), then
  consults `ANIRSS_QBT_PASSWORD`, then stdin if `--password-stdin`. Only if
  all of those are absent **and** no action flag is set does it fall
  through to the existing `getpass.getpass` retry loop. With an action flag
  and no password source, it dies non-interactively.

### Examples

```
ANIRSS_QBT_PASSWORD=… anirss --subscribe Frieren
ANIRSS_QBT_PASSWORD=… anirss --download 5 "Frieren 1080p" --name Frieren
echo "$pw" | anirss --password-stdin --movie "Some Movie"
ANIRSS_QBT_PASSWORD=… anirss --subscribe -S "https://nyaa.si/?page=rss&q=…"
```

## Packaging changes

| Channel | Today | After |
|---|---|---|
| `install.sh` | `install -m 755 anirss "$BIN_DIR/anirss"` | Copy launcher + `anirss_lib/` into `${XDG_DATA_HOME:-~/.local/share}/anirss/`; symlink `~/.local/bin/anirss` → there. The existing symlink-mode (dev checkout) path keeps using `readlink -f` against the repo checkout. |
| Brew formula | `bin.install "anirss"` | `libexec.install "anirss", "anirss_lib"` + `bin.install_symlink libexec/"anirss"`. |
| AUR `anirss` and `anirss-git` PKGBUILDs | `install -Dm755 anirss "$pkgdir/usr/bin/anirss"` | `install -d "$pkgdir/usr/lib/anirss"`; copy launcher (755) + `anirss_lib/` (dirs 755, files 644) under it; symlink `/usr/bin/anirss` to it. |

## Test plan

- All 50 currently-passing tests stay green after each commit.
- The 3 SID-cookie tests get fixed in the qBittorrent move commit.
- New tests for non-interactive:
  - `parse_cli_args` recognises every new flag and rejects mutually
    exclusive combinations (e.g. `--subscribe --download-all`).
  - `--download N` slicing on a 10-item synthetic list returns top N by
    `downloads`.
  - `classify_url` table-driven cases.
- New test for terminal size: stub `/dev/tty` open via monkeypatch and
  confirm `get_size()` reaches it before `shutil.get_terminal_size`.
- Manual smoke run against the user's local qBittorrent with
  `ANIRSS_QBT_PASSWORD` set: `--subscribe`, `--download 1`, `--movie`.

## Commit ordering

1. Skeleton: launcher + empty `anirss_lib/__init__.py` (with `__version__`)
   + smoke test that imports it.
2. Move pure-data modules: `ansi.py`, `config.py`, `types.py`,
   `logging.py`, `titles.py`, `nyaa.py`, `terminal.py` with the /dev/tty
   fix. Update `test_anirss.py` to `import anirss_lib.…`.
3. Move display: `format.py`.
4. Move fzf core + helpers: `fzf.py` with the `_parse_fzf_output`
   consolidation (P2).
5. Move refine + readline: `refine.py`, `readline_input.py`.
6. Move qBittorrent: `qbt/session.py`, `qbt/feeds.py`, `qbt/actions.py`.
   Fix B2 and B3 here.
7. Move CLI: `cli/urls.py` (P4), `cli/args.py`, `cli/pickers.py`,
   `cli/commands.py`.
8. Split `main.py` (P3 + P1).
9. Add non-interactive flags + tests.
10. Update `install.sh`, brew formula, both PKGBUILDs.
11. Bump `__version__` to `0.3.0`; update README.
