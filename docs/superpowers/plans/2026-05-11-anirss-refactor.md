# anirss Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single 2161-line `anirss` script into a launcher + `anirss_lib/` package, fix the terminal-size regression, repair three drifted tests, and add full non-interactive flag-driven operation.

**Architecture:** Launcher script `anirss` injects its own dir into `sys.path` then imports `anirss_lib.main:main`. Modules separated by responsibility: ansi/terminal/config/types/logging are leaves; titles/format/nyaa/refine/qbt/cli are mid-tier; `main.py` orchestrates. Non-interactive mode is triggered by any of `--subscribe`, `--download-all`, `--download N`, `--movie`; all fzf/prompt code is bypassed and password is read from `ANIRSS_QBT_PASSWORD` or `--password-stdin`.

**Tech Stack:** Python 3.11+ (stdlib only: `tomllib`, `urllib`, `xml.etree`, `readline`, `subprocess`), `fzf` (runtime), `pytest` via `uvx`. No new runtime deps.

---

## Pre-flight

- [ ] **Step 0a: Confirm baseline**

Run: `uvx pytest test_anirss.py -q`
Expected: `3 failed, 50 passed` (the three `_make_sid_cookie` tests fail with `TypeError: _make_sid_cookie() missing 1 required positional argument: 'value'`).

- [ ] **Step 0b: Create the package skeleton**

```bash
mkdir -p anirss_lib/qbt anirss_lib/cli
touch anirss_lib/__init__.py anirss_lib/qbt/__init__.py anirss_lib/cli/__init__.py
```

- [ ] **Step 0c: Sanity-check `/dev/tty` accessibility**

Run in an interactive terminal (not a piped shell):
```bash
python3 -c "import os
with open('/dev/tty') as t: print(os.get_terminal_size(t.fileno()))"
```
Expected: an `os.terminal_size(columns=…, lines=…)` matching your terminal. If this raises `OSError: No such device or address`, the `/dev/tty` fallback in Task 4 will simply not be exercised — `shutil.get_terminal_size()` then takes over. That's the documented fallback path.

---

## Task 1: Launcher + version

**Files:**
- Create: `anirss_lib/__init__.py`
- Modify: `anirss` (replace contents with launcher)
- Backup: `anirss.bak` (working copy of the original, deleted at the very end)

- [ ] **Step 1: Stash the original for incremental copy-paste**

```bash
cp anirss anirss.bak
```

- [ ] **Step 2: Write `anirss_lib/__init__.py`**

```python
"""anirss — search nyaa.si, then subscribe (RSS rule) or download via qBittorrent."""

__version__ = "0.3.0"
```

- [ ] **Step 3: Replace `anirss` with the launcher**

```python
#!/usr/bin/env python3
"""anirss launcher — locates anirss_lib next to itself and runs main()."""
import os
import sys

# realpath resolves the symlink chain (e.g. /usr/bin/anirss ->
# /usr/lib/anirss/anirss, or ~/.local/bin/anirss -> ~/repos/anirss/anirss)
# so the launcher always finds its sibling anirss_lib/.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from anirss_lib.main import main

if __name__ == "__main__":
    main()
```

Keep the file executable: `chmod +x anirss`.

- [ ] **Step 4: Add a smoke test**

Create `tests/test_smoke.py` (new dir `tests/`):
```python
def test_version_importable():
    import anirss_lib
    assert anirss_lib.__version__ == "0.3.0"
```

Also add `tests/__init__.py` (empty) so pytest discovers it cleanly.

- [ ] **Step 5: Stub `anirss_lib/main.py` so the launcher doesn't crash**

```python
"""Temporary stub until Task 11 fills it in."""

def main() -> None:
    raise SystemExit("anirss_lib.main.main is not implemented yet")
```

- [ ] **Step 6: Run the smoke test**

Run: `uvx pytest tests/test_smoke.py -v`
Expected: PASS.

The legacy `test_anirss.py` is still loading the original `anirss` script via `SourceFileLoader`, but we just overwrote it with a stub. **Skip the legacy file for now** — it'll be ported in each task as code moves over. Verify pytest still completes (the 3 known failures + many new failures will appear since `anirss` no longer contains the functions). That's expected; pass the next task as long as `test_smoke.py` passes.

Workaround for incremental migration: replace the top of `test_anirss.py` so it loads from `anirss.bak` until everything is migrated. Change:
```python
_ANIRSS_PATH = Path(__file__).parent / "anirss"
```
to:
```python
_ANIRSS_PATH = Path(__file__).parent / "anirss.bak"
```

Run: `uvx pytest -q`
Expected: `3 failed, 50 passed` (same as Step 0a) plus the smoke test passes.

---

## Task 2: ansi + terminal + B1 fix

**Files:**
- Create: `anirss_lib/ansi.py`
- Create: `anirss_lib/terminal.py`
- Test: `tests/test_terminal.py`, `tests/test_ansi.py`

- [ ] **Step 1: Move ANSI constants to `anirss_lib/ansi.py`**

```python
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

PROMPT_SEARCH = f"{C_YEL}Search >{C_OFF} "
PROMPT_FILTER = f"{C_YEL}Search >{C_OFF} {C_BLU}Filter >{C_OFF} "
PROMPT_ACTION = f"{C_YEL}Search >{C_OFF} {C_BLU}Filter >{C_OFF} {C_GRN}Action >{C_OFF} "

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
```

- [ ] **Step 2: Write the failing terminal test**

Create `tests/test_terminal.py`:
```python
import io
import os
from unittest.mock import patch, MagicMock

from anirss_lib import terminal


def test_get_size_reads_from_dev_tty(monkeypatch):
    """When /dev/tty is accessible, get_size uses it (not stdout)."""
    fake_tty = MagicMock()
    fake_tty.__enter__.return_value = fake_tty
    fake_tty.fileno.return_value = 42

    def fake_open(path, *a, **kw):
        if path == "/dev/tty":
            return fake_tty
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(os, "get_terminal_size",
                        lambda fd: os.terminal_size((80, 30)) if fd == 42 else None)

    size = terminal.get_size()
    assert size == os.terminal_size((80, 30))


def test_get_size_falls_back_when_no_tty(monkeypatch):
    def fake_open(*a, **kw):
        raise OSError("no tty")
    monkeypatch.setattr("builtins.open", fake_open)
    size = terminal.get_size()
    # shutil's fallback returns FALLBACK when stdout is piped in tests too.
    assert size.columns >= 40
    assert size.lines >= 1
```

- [ ] **Step 3: Run to confirm it fails**

Run: `uvx pytest tests/test_terminal.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'anirss_lib.terminal'`).

- [ ] **Step 4: Implement `anirss_lib/terminal.py`**

```python
"""Get the real terminal size, even when stdout is piped.

The hidden `--_search-rss` reload subprocess that backs the live nyaa picker
inherits piped stdout from fzf, so `shutil.get_terminal_size()` falls back
to its default. Reading `/dev/tty` (the controlling terminal) sidesteps the
problem.
"""

import os
import shutil

FALLBACK = os.terminal_size((120, 24))


def get_size() -> os.terminal_size:
    try:
        with open("/dev/tty") as tty:
            return os.get_terminal_size(tty.fileno())
    except OSError:
        return shutil.get_terminal_size(FALLBACK)
```

- [ ] **Step 5: Run terminal tests**

Run: `uvx pytest tests/test_terminal.py -v`
Expected: PASS.

- [ ] **Step 6: Add ansi tests**

Create `tests/test_ansi.py`:
```python
from anirss_lib.ansi import ansi_strip, right_anchor, truncate_ansi, C_RED, C_OFF


def test_ansi_strip_removes_color():
    assert ansi_strip(f"{C_RED}hi{C_OFF}") == "hi"


def test_right_anchor_pads_visible_width():
    out = right_anchor("a", "b", 10)
    assert ansi_strip(out) == "a" + " " * 8 + "b"


def test_truncate_ansi_keeps_visible_chars():
    out = truncate_ansi("hello world", 5)
    # 4 visible chars + dim ellipsis (1 visible char in the ANSI form)
    assert ansi_strip(out) == "hell…"


def test_truncate_ansi_noop_when_fits():
    assert truncate_ansi("hi", 10) == "hi"
```

Run: `uvx pytest tests/test_ansi.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add anirss_lib/ansi.py anirss_lib/terminal.py tests/test_terminal.py tests/test_ansi.py
git commit -m "Add anirss_lib.ansi and anirss_lib.terminal

terminal.get_size() reads /dev/tty so the --_search-rss reload
subprocess gets the real terminal size instead of shutil's piped-stdout
fallback (120, 24)."
```

---

## Task 3: config + logging + types

**Files:**
- Create: `anirss_lib/config.py`, `anirss_lib/logging.py`, `anirss_lib/types.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Move types to `anirss_lib/types.py`**

```python
"""Shared dataclass-like types used across modules."""

from typing import NamedTuple


class Item(NamedTuple):
    title: str
    link: str
    seeders: int = 0
    leechers: int = 0
    downloads: int = 0
    size: str = ""
    category: str = ""


class Group(NamedTuple):
    label: str
    tokens: list[str]
    member_count: int
    has_poster: bool


class Pick(NamedTuple):
    """Result of pick_group: a kind and (for tokens/custom) the relevant terms."""
    kind: str  # "tokens" | "done" | "exclude" | "show_all" | "custom" | "back"
    tokens: list[str]


PICK_DONE = Pick("done", [])
PICK_EXCLUDE = Pick("exclude", [])
PICK_SHOW_ALL = Pick("show_all", [])
PICK_BACK = Pick("back", [])
```

- [ ] **Step 2: Move logging to `anirss_lib/logging.py`**

```python
"""File-based logging + die() for fatal exits."""

import datetime
import sys
from typing import NoReturn, TextIO

from anirss_lib.ansi import C_DIM, C_OFF, C_RED


_LOG_FILE: TextIO | None = None


def init_log(log_path: str) -> None:
    import os
    global _LOG_FILE
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        _LOG_FILE = open(log_path, "a", buffering=1)
    except OSError:
        _LOG_FILE = None


def log(level: str, msg: str) -> None:
    if _LOG_FILE is None:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _LOG_FILE.write(f"{ts} [{level:5}] {msg}\n")
    except OSError:
        pass


def die(msg: str) -> NoReturn:
    log("ERROR", msg)
    print(f"{C_RED}error:{C_OFF} {msg}", file=sys.stderr)
    if _LOG_FILE is not None:
        print(f"{C_DIM}log: {_LOG_FILE.name}{C_OFF}", file=sys.stderr)
    sys.exit(1)
```

- [ ] **Step 3: Move config to `anirss_lib/config.py`**

```python
"""Config schema, defaults, loading, and migration."""

import copy
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import TypedDict, cast

from anirss_lib.ansi import C_CYN, C_DIM, C_GRN, C_OFF, C_YEL
from anirss_lib.logging import die


CONFIG_PATH = Path("~/.config/anirss/config.toml").expanduser()
STATE_DIR = Path("~/.local/state/anirss").expanduser()
SID_PATH = STATE_DIR / "qbt.sid"
FEEDS_CACHE_PATH = STATE_DIR / "feeds.txt"
FEED_CACHE_TTL_SECONDS = 24 * 60 * 60


class QbtConfig(TypedDict):
    url: str
    username: str
    login_retries: int


class DownloadsConfig(TypedDict):
    save_base: str
    movie_path: str


class SearchConfig(TypedDict):
    nyaa_url: str
    category: str
    filter: str


class LoggingConfig(TypedDict):
    log_path: str


class DisplayConfig(TypedDict):
    show_leechers: bool


class AnirssConfig(TypedDict):
    qbittorrent: QbtConfig
    downloads: DownloadsConfig
    search: SearchConfig
    logging: LoggingConfig
    display: DisplayConfig


DEFAULT_CONFIG_TOML = """\
# anirss configuration

[qbittorrent]
url = "http://localhost:8080"
username = "admin"
# password attempts before giving up; the password itself is always prompted
login_retries = 3

[downloads]
# Subscriptions and bulk downloads create a per-name subdirectory under this.
save_base = "~/Downloads/Anime"
# Single-file movie downloads land directly here (no per-name subdir).
movie_path = "~/Downloads/Movies"

[search]
nyaa_url = "https://nyaa.si/"
# nyaa category id: "1_0" = Anime (all), "1_2" = English-translated, etc.
category = "1_0"
# nyaa filter: "0" = no filter, "1" = no remakes, "2" = trusted only
filter = "2"

[logging]
log_path = "~/.local/state/anirss/anirss.log"

[display]
# Show leechers count alongside seeders (e.g. "45s/5l"). Off by default.
show_leechers = false
"""

DEFAULT_CONFIG: AnirssConfig = cast(AnirssConfig, tomllib.loads(DEFAULT_CONFIG_TOML))


def _deep_merge(dst, src):
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


SECTION_HEADER_RE = re.compile(r"^\[([^\[\]]+)\]\s*$", re.MULTILINE)


def _split_toml_sections(text: str) -> list[tuple[str, str]]:
    matches = list(SECTION_HEADER_RE.finditer(text))
    if not matches:
        return [("", text)]
    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        sections.append(("", text[:matches[0].start()]))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(1), text[m.start():end]))
    return sections


def migrate_config() -> None:
    if not CONFIG_PATH.exists():
        print(f"{C_DIM}no config at {CONFIG_PATH} — run anirss once to bootstrap.{C_OFF}")
        return
    user_text = CONFIG_PATH.read_text()
    try:
        user_cfg = tomllib.loads(user_text)
    except tomllib.TOMLDecodeError as e:
        die(f"can't parse {CONFIG_PATH}: {e}")

    defaults = _split_toml_sections(DEFAULT_CONFIG_TOML)
    missing = [(name, body) for name, body in defaults if name and name not in user_cfg]
    if not missing:
        print(f"{C_GRN}config up to date{C_OFF} — no new sections in {CONFIG_PATH}")
        return

    appended = "\n".join(body.rstrip() for _, body in missing)
    new_text = user_text.rstrip() + "\n\n" + appended + "\n"
    CONFIG_PATH.write_text(new_text)
    print(f"{C_GRN}migrated {CONFIG_PATH}:{C_OFF} appended {len(missing)} new section(s)")
    for name, _ in missing:
        print(f"  {C_CYN}+{C_OFF} [{name}]")


def load_config() -> AnirssConfig:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("rb") as f:
                user_cfg = tomllib.load(f)
            _deep_merge(cfg, user_cfg)
        except (OSError, tomllib.TOMLDecodeError) as e:
            print(f"{C_YEL}warning: bad config at {CONFIG_PATH}: {e}{C_OFF}", file=sys.stderr)
    else:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)
            print(f"{C_DIM}wrote default config: {CONFIG_PATH} (edit it!){C_OFF}", file=sys.stderr)
        except OSError as e:
            print(f"{C_YEL}warning: couldn't create {CONFIG_PATH}: {e}{C_OFF}", file=sys.stderr)
    cfg["downloads"]["save_base"] = os.path.expanduser(cfg["downloads"]["save_base"])
    cfg["downloads"]["movie_path"] = os.path.expanduser(cfg["downloads"]["movie_path"])
    cfg["logging"]["log_path"] = os.path.expanduser(cfg["logging"]["log_path"])
    return cfg
```

- [ ] **Step 4: Test the imports**

Create `tests/test_config.py`:
```python
from anirss_lib import config, logging, types


def test_default_config_has_required_sections():
    cfg = config.DEFAULT_CONFIG
    assert "qbittorrent" in cfg
    assert "downloads" in cfg
    assert "search" in cfg


def test_item_namedtuple_default_values():
    it = types.Item("title", "link")
    assert it.seeders == 0
    assert it.leechers == 0


def test_pick_constants():
    assert types.PICK_DONE.kind == "done"
    assert types.PICK_BACK.kind == "back"
```

Run: `uvx pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/config.py anirss_lib/logging.py anirss_lib/types.py tests/test_config.py
git commit -m "Add anirss_lib.{config,logging,types}"
```

---

## Task 4: titles + nyaa

**Files:**
- Create: `anirss_lib/titles.py`, `anirss_lib/nyaa.py`
- Modify: `test_anirss.py` (port poster_of/show_name/title_tokens tests to import from anirss_lib)

- [ ] **Step 1: Move title parsing to `anirss_lib/titles.py`**

```python
"""Best-effort title parsing for nyaa torrent names."""

import re


POSTER_RE   = re.compile(r"^\[([^\]]+)\]")
TOKEN_RE    = re.compile(r"[\s\[\]\(\)]+")
NUMERIC_RE  = re.compile(r"^\d+$")
HEX_RE      = re.compile(r"^[0-9A-Fa-f]{6,}$")
EXT_RE      = re.compile(r"\.\w{2,4}$")
RES_RE      = re.compile(r"\b(\d{3,4})p\b", re.IGNORECASE)
# " - 04" or " - 04v2" followed by space, end, or bracket; not " - 1080p".
EPISODE_RE  = re.compile(r"\s+-\s+\d{1,4}(?:v\d+)?(?=\s|$|\[)")
META_RE     = re.compile(r"\s*\[")


def poster_of(title: str) -> str | None:
    match = POSTER_RE.match(title)
    return f"[{match.group(1)}]" if match else None


def show_name(title: str) -> str:
    name = EXT_RE.sub("", title)
    name = POSTER_RE.sub("", name, count=1).strip()
    cuts: list[int] = []
    episode = EPISODE_RE.search(name)
    if episode:
        cuts.append(episode.start())
    meta = META_RE.search(name)
    if meta and meta.start() > 0:
        cuts.append(meta.start())
    if cuts:
        name = name[:min(cuts)]
    return name.strip(" -")


def title_tokens(title: str) -> list[str]:
    base = EXT_RE.sub("", title)
    base = POSTER_RE.sub("", base, count=1)
    tokens: list[str] = []
    for token in TOKEN_RE.split(base):
        if not token or token == "-":
            continue
        if NUMERIC_RE.match(token):
            continue
        if HEX_RE.match(token) and not re.search(r"[g-zG-Z]", token):
            continue
        if len(token) < 2:
            continue
        tokens.append(token)
    return tokens
```

- [ ] **Step 2: Move nyaa fetcher to `anirss_lib/nyaa.py`**

```python
"""nyaa.si RSS fetching."""

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from anirss_lib.config import SearchConfig
from anirss_lib.logging import die, log
from anirss_lib.types import Item


NYAA_NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}


def search_url(query: str, search: SearchConfig) -> str:
    qs = urllib.parse.urlencode({
        "page": "rss",
        "q": query,
        "c": search["category"],
        "f": search["filter"],
    })
    return f"{search['nyaa_url']}?{qs}"


def _int_text(elem: ET.Element | None) -> int:
    if elem is None or not elem.text:
        return 0
    try:
        return int(elem.text.strip())
    except ValueError:
        return 0


def _str_text(elem: ET.Element | None) -> str:
    if elem is None or not elem.text:
        return ""
    return elem.text.strip()


def _fetch_items_from_url(url: str) -> list[Item]:
    log("INFO", f"GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "anirss/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except urllib.error.URLError as e:
        die(f"can't reach nyaa: {e}")
    root = ET.fromstring(data)
    out: list[Item] = []
    for entry in root.iter("item"):
        title = entry.find("title")
        link = entry.find("link")
        if title is not None and title.text and link is not None and link.text:
            out.append(Item(
                title=title.text.strip(),
                link=link.text.strip(),
                seeders=_int_text(entry.find("nyaa:seeders", NYAA_NS)),
                leechers=_int_text(entry.find("nyaa:leechers", NYAA_NS)),
                downloads=_int_text(entry.find("nyaa:downloads", NYAA_NS)),
                size=_str_text(entry.find("nyaa:size", NYAA_NS)),
                category=_str_text(entry.find("nyaa:category", NYAA_NS)),
            ))
    log("INFO", f"  -> {len(out)} items")
    return out


def fetch_items(query: str, search: SearchConfig) -> list[Item]:
    """Return list of Items from the nyaa RSS for `query`."""
    return _fetch_items_from_url(search_url(query, search))
```

- [ ] **Step 3: Port the title-related tests in test_anirss.py**

Replace the test_anirss.py header (lines 1-21) with:
```python
"""Tests for the pure functions in anirss_lib."""

from __future__ import annotations

from anirss_lib import titles, refine, types
# Legacy alias keeps existing test bodies working while we migrate.
import anirss_lib.titles as _t
import anirss_lib.refine as _r

# Stand-ins so the existing tests keep referencing `anirss.foo`.
class _Anirss:
    poster_of = _t.poster_of
    show_name = _t.show_name
    title_tokens = _t.title_tokens
    Item = types.Item
    auto_resolution = _r.auto_resolution
    compute_groups = _r.compute_groups
    apply_pick = _r.apply_pick
    add_exclude_to_query = _r.add_exclude_to_query

anirss = _Anirss()
```

This is intentionally an ugly shim — it keeps the test diff small. Each later task removes more legacy references and the shim shrinks accordingly.

After Task 11 ships, the shim is replaced with direct imports per test.

- [ ] **Step 4: Run the suite**

Run: `uvx pytest -q`
Expected: title-related tests (`test_poster_of_*`, `test_show_name_*`, `test_title_tokens_*`) PASS via the shim. Other tests still fail (`refine`, `auto_resolution`, etc. not in `_r` until later tasks). That's OK as long as the title tests pass.

Run: `uvx pytest test_anirss.py -k "poster_of or show_name or title_tokens" -v`
Expected: all 14 of those tests PASS.

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/titles.py anirss_lib/nyaa.py test_anirss.py
git commit -m "Add anirss_lib.titles and anirss_lib.nyaa"
```

---

## Task 5: format

**Files:**
- Create: `anirss_lib/format.py`

- [ ] **Step 1: Write `anirss_lib/format.py`**

```python
"""Display helpers: colored stats, titles, results box."""

from anirss_lib.ansi import (
    C_BLD, C_CYN, C_DIM, C_GRN, C_OFF, C_RED, C_YEL,
    ansi_strip, right_anchor, truncate_ansi,
)
from anirss_lib import terminal
from anirss_lib.titles import POSTER_RE
from anirss_lib.types import Item


# Set from cfg in main(); guards leechers in format_stats.
_SHOW_LEECHERS: bool = True


def set_show_leechers(flag: bool) -> None:
    global _SHOW_LEECHERS
    _SHOW_LEECHERS = bool(flag)


def colorize_title(title: str) -> str:
    """Color the leading [poster] cyan; leave the rest alone."""
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
    """One-line colored stats: 530 dl · 45s/5l · 926.7 MiB."""
    dl_c = _grade_downloads(item.downloads)
    s_c = _grade_seeders(item.seeders)
    seed = f"{s_c}{item.seeders:>3}{C_OFF}{C_DIM}s{C_OFF}"
    if _SHOW_LEECHERS:
        seed += f"/{C_RED}{item.leechers}{C_OFF}{C_DIM}l{C_OFF}"
    parts = [f"{dl_c}{item.downloads:>5} dl{C_OFF}", seed]
    if item.size:
        parts.append(f"{C_DIM}{item.size:>10}{C_OFF}")
    return f"  {C_DIM}·{C_OFF}  ".join(parts)


def show_titles(items: list[Item], cap: int | None = None) -> None:
    from anirss_lib.ansi import FILTER_PICKER_LINES
    term = terminal.get_size()
    if cap is None:
        reserved = FILTER_PICKER_LINES + 8
        cap = max(8, term.lines - reserved)
    width = term.columns
    inner = max(40, width - 4)
    bar = "─" * (inner + 2)
    print(f"{C_DIM}╭{bar}╮{C_OFF}")
    for item in items[:cap]:
        cat = category_chip(item)
        cat_w = len(item.category) + 1 if item.category else 0
        body_max = max(10, inner - cat_w)
        body = truncate_ansi(f"{format_stats(item)}  {colorize_title(item.title)}", body_max)
        if cat:
            line = right_anchor(body, cat, inner)
        else:
            line = body + " " * max(0, inner - len(ansi_strip(body)))
        print(f"{C_DIM}│{C_OFF} {line} {C_DIM}│{C_OFF}")
    print(f"{C_DIM}╰{bar}╯{C_OFF}")
    if len(items) > cap:
        more = len(items) - cap
        print(f"  {C_DIM}... and {more} more "
              f"({C_OFF}{C_YEL}use [show all] in the picker{C_OFF}{C_DIM}){C_OFF}")
```

- [ ] **Step 2: Smoke-import**

Run: `python3 -c "from anirss_lib import format; print(format.format_stats.__name__)"`
Expected: `format_stats`.

- [ ] **Step 3: Commit**

```bash
git add anirss_lib/format.py
git commit -m "Add anirss_lib.format"
```

---

## Task 6: fzf

**Files:**
- Create: `anirss_lib/fzf.py`

- [ ] **Step 1: Write `anirss_lib/fzf.py`**

Combine the `_check_fzf_ctrlc` + line parsing into one helper, and copy the four fzf wrappers (`fzf_pick_one`, `fzf_pick_with_query`, `fzf_search_prompt`, `view_all_titles`) verbatim from the original `anirss.bak`, switching them to use the helper.

```python
"""fzf process wrappers + the structured output parser they share."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from anirss_lib.ansi import (
    C_BLD, C_DIM, C_OFF, FILTER_PICKER_LINES, FZF_BINDS, FZF_HL_COLORS,
    PROMPT_FILTER, ansi_strip, right_anchor, truncate_ansi,
)
from anirss_lib import terminal
from anirss_lib.format import category_chip, colorize_title, format_stats
from anirss_lib.logging import log
from anirss_lib.types import Item


class FzfOutput(NamedTuple):
    query: str            # empty when --print-query wasn't requested
    expect_key: str       # empty when not pressed or --expect wasn't requested
    selections: list[str] # selected lines, possibly empty


def _parse_fzf_output(stdout: str, *, print_query: bool, expect: bool) -> FzfOutput:
    """Decode fzf's stdout layout. Layout:
       line 0:           query (if --print-query) or expect-key (if --expect) or first selection
       line 0 or 1:      expect-key (if both flags) or first selection
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
    return FzfOutput(query=query.strip() if not print_query else query,
                     expect_key=expect_key, selections=selections)


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
    proc = subprocess.run(args, input="\n".join(options), text=True,
                          stdout=subprocess.PIPE)
    out = _parse_fzf_output(proc.stdout or "", print_query=False, expect=True)
    if proc.returncode != 0 or not out.selections:
        return None
    return out.selections[0].strip() or None


def fzf_pick_with_query(options: list[str], header: str
                        ) -> tuple[str, str | None, bool]:
    """Run fzf with --print-query. Returns (typed_query, choice_or_None, cancelled)."""
    if not shutil.which("fzf") or not options:
        return "", None, True
    height_lines = min(max(len(options) + 4, 6), FILTER_PICKER_LINES)
    args = [
        "fzf", "--ansi",
        "--color", FZF_HL_COLORS,
        "--bind", FZF_BINDS,
        "--prompt", PROMPT_FILTER,
        "--header", header,
        "--layout=reverse",
        "--height", str(height_lines),
        "--cycle", "--no-info",
        "--print-query",
        "--preview-window=hidden",
        "--expect", "ctrl-c",
    ]
    proc = subprocess.run(args, input="\n".join(options), text=True,
                          stdout=subprocess.PIPE)
    out = _parse_fzf_output(proc.stdout or "", print_query=True, expect=True)
    query = out.query.strip()
    if proc.returncode == 0 and out.selections:
        return query, (out.selections[0].strip() or None), False
    if proc.returncode == 1:
        return query, None, False
    return "", None, True


def fzf_search_prompt(prompt_label: str, *, default: str = "") -> str | None:
    """Live nyaa search via fzf. Returns the typed query on Enter, None on Esc."""
    from anirss_lib.readline_input import prompt as readline_prompt
    history_file = _history_path("search")
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    if not shutil.which("fzf"):
        return readline_prompt(prompt_label, history="search") or None

    script_path = shutil.which(sys.argv[0]) or os.path.abspath(sys.argv[0])
    quoted = shlex.quote(script_path)
    search_cmd = f"sleep 1 && {quoted} --_search-rss {{q}}"
    initial_cmd = f"{quoted} --_search-rss {{q}}"

    fzf_args = [
        "fzf", "--ansi",
        "--disabled", "--no-mouse",
        "--print-query",
        "--expect", "ctrl-c,enter",
        "--color", FZF_HL_COLORS,
        "--prompt", prompt_label,
        "--query", default,
        "--layout=reverse",
        "--header-first",
        "--height", "90%",
        "--no-info",
        "--preview-window=hidden",
        "--header",
        f"type to search nyaa · pauses ~1 s before refresh · "
        f"{C_BLD}Enter{C_OFF} confirms · {C_BLD}Esc{C_OFF} back · {C_BLD}Ctrl-C{C_OFF} quits",
        "--history", str(history_file),
        "--bind", FZF_BINDS,
        "--bind", f"change:reload({search_cmd})",
    ]
    if default:
        fzf_args.extend(["--bind", f"start:reload({initial_cmd})"])

    proc = subprocess.run(fzf_args, input="", text=True, stdout=subprocess.PIPE)
    out = _parse_fzf_output(proc.stdout or "",
                            print_query=True, expect=True)
    if out.expect_key == "ctrl-c":
        # _parse_fzf_output already sys.exit(130)'d; defensive.
        sys.exit(130)
    if out.expect_key == "enter":
        q = out.query.strip()
        if not q:
            return None
        try:
            with history_file.open("a") as f:
                f.write(q + "\n")
        except OSError:
            pass
        return q
    return None


def view_all_titles(items: list[Item]) -> None:
    if not shutil.which("fzf") or not items:
        return
    import textwrap
    width = max(40, terminal.get_size().columns - 3)

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
        "fzf", "--ansi", "--read0", "--multi-line",
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
    proc = subprocess.run(args, input="\0".join(blocks) + "\0",
                          text=True, stdout=subprocess.PIPE)
    _parse_fzf_output(proc.stdout or "", print_query=False, expect=True)
```

- [ ] **Step 2: Smoke-import**

Run: `python3 -c "from anirss_lib import fzf; print(fzf.FzfOutput)"`
Expected: prints the NamedTuple class.

- [ ] **Step 3: Commit**

```bash
git add anirss_lib/fzf.py
git commit -m "Add anirss_lib.fzf with _parse_fzf_output consolidation"
```

---

## Task 7: readline_input + refine

**Files:**
- Create: `anirss_lib/readline_input.py`, `anirss_lib/refine.py`
- Modify: `test_anirss.py` (extend shim to expose refine members)

- [ ] **Step 1: Write `anirss_lib/readline_input.py`**

```python
"""input() prompts with per-history-key line editing."""

from pathlib import Path

from anirss_lib.logging import die, log


HISTORY_LIMIT = 1000


def _history_path(key: str) -> Path:
    return Path(f"~/.local/state/anirss/{key}.history").expanduser()


def setup_readline() -> None:
    try:
        import readline
    except ImportError:
        return
    libedit = "libedit" in (readline.__doc__ or "")
    if libedit:
        binds = [r'bind "\e\x7f" ed-delete-prev-word',
                 r'bind "\e\b"   ed-delete-prev-word']
    else:
        binds = [r'"\e\x7f": backward-kill-word',
                 r'"\e\b":   backward-kill-word']
    for b in binds:
        try:
            readline.parse_and_bind(b)
        except Exception:
            pass
    readline.set_history_length(HISTORY_LIMIT)


def prompt(label: str, *, history: str | None = None) -> str:
    try:
        import readline as rl
    except ImportError:
        rl = None  # type: ignore[assignment]

    history_file = _history_path(history) if history else None
    saved: list[str] = []
    if rl is not None and history_file is not None:
        n = rl.get_current_history_length()
        saved = [rl.get_history_item(i + 1) or "" for i in range(n)]
        rl.clear_history()
        try:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            if history_file.exists():
                rl.read_history_file(str(history_file))
        except OSError as e:
            log("WARN", f"history read {history_file}: {e}")

    try:
        result = input(label).strip()
    except (EOFError, KeyboardInterrupt):
        die("cancelled")

    if rl is not None and history_file is not None:
        try:
            rl.write_history_file(str(history_file))
        except OSError as e:
            log("WARN", f"history write {history_file}: {e}")
        rl.clear_history()
        for entry in saved:
            if entry:
                rl.add_history(entry)

    return result


def get_name(default_name: str | None) -> str:
    label = f"Name [{default_name}]: " if default_name else "Name: "
    name = prompt(label, history="name")
    if not name and default_name:
        name = default_name
    if not name:
        die("name cannot be empty")
    if "/" in name or "\\" in name:
        die("name must not contain '/' or '\\'")
    return name
```

- [ ] **Step 2: Write `anirss_lib/refine.py`**

Copy `compute_groups`, `pick_group`, `apply_pick`, `auto_resolution`, `add_exclude_to_query`, `add_term_to_query`, `_members_of`, `refine`, plus the `DONE`/`EXCLUDE` labels, from the original. Update imports:

```python
"""Interactive refinement loop and its building blocks."""

from collections import Counter, defaultdict

from anirss_lib.ansi import (
    C_BLD, C_DIM, C_GRN, C_OFF, C_RED, C_YEL, ansi_strip,
)
from anirss_lib.config import SearchConfig
from anirss_lib.format import colorize_picker_label, show_titles
from anirss_lib.fzf import fzf_pick_with_query, view_all_titles
from anirss_lib.logging import log
from anirss_lib.nyaa import fetch_items
from anirss_lib.readline_input import prompt
from anirss_lib.titles import RES_RE, poster_of, title_tokens
from anirss_lib.types import (
    Group, Item, PICK_BACK, PICK_DONE, PICK_EXCLUDE, PICK_SHOW_ALL, Pick,
)


DONE = "[→ Continue To Actions]"
EXCLUDE = "[✗ Exclude Term…]"


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


def pick_group(groups: list[Group], selected: list[Item]) -> Pick:
    # body identical to original lines 850-897 — copy verbatim
    n_results = len(selected)
    show_all_label = f"[≡ Show All {n_results} Titles]"
    label_width = 28
    options = [
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
    query, choice, cancelled = fzf_pick_with_query(options, header)
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
    if choice_plain == show_all_label:
        return PICK_SHOW_ALL
    chosen_label = choice_plain.rsplit(" (", 1)[0].rstrip()
    chosen = next((g for g in groups if g.label == chosen_label), None)
    if chosen is None:
        return PICK_DONE
    log("INFO", f"picked {chosen_label!r} -> tokens {chosen.tokens}")
    return Pick("tokens", chosen.tokens)


def apply_pick(selected: list[Item], query: str, tokens: list[str]
               ) -> tuple[list[Item], str] | None:
    new_selected = list(selected)
    for token in tokens:
        if token.startswith("["):
            new_selected = [item for item in new_selected if poster_of(item.title) == token]
        else:
            new_selected = [item for item in new_selected if token in title_tokens(item.title)]
    if not new_selected:
        return None
    new_query = query
    for token in tokens:
        if token.startswith("[") and not new_query.lstrip().startswith("["):
            new_query = f"{token} {new_query}"
        elif not token.startswith("["):
            new_query = f"{new_query} {token}"
    return new_selected, new_query


def add_exclude_to_query(query: str, term: str) -> str:
    term = term.lstrip("-").strip()
    if not term:
        return query
    flag = f'-"{term}"' if " " in term else f"-{term}"
    return f"{query} {flag}"


def add_term_to_query(query: str, term: str) -> str:
    term = term.strip()
    if not term:
        return query
    flag = f'"{term}"' if " " in term else term
    return f"{query} {flag}"


def refine(initial_query: str, items: list[Item], search: SearchConfig
           ) -> tuple[str, list[Item], str]:
    query = initial_query.strip()
    log("INFO", f"refine start: {query!r} ({len(items)} items)")
    query, selected = auto_resolution(query, items)

    while True:
        count = len(selected)
        print()
        print(f"{C_BLD}{count} result(s):{C_OFF}")
        show_titles(selected)

        groups = compute_groups(selected)
        if not groups:
            print(f"{C_DIM}(no token refinements left — exclude a term or finalize){C_OFF}")

        print()
        print(f"{C_BLD}Query:{C_OFF} {query}")
        pick = pick_group(groups, selected)
        if pick.kind == "done":
            break
        if pick.kind == "back":
            log("INFO", "refine: esc → back to search")
            return query, selected, "back"
        if pick.kind == "show_all":
            view_all_titles(selected)
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

        result = apply_pick(selected, query, pick.tokens)
        if result is None:
            print(f"{C_YEL}filter would yield 0 results — skipped{C_OFF}")
            log("WARN", f"tokens {pick.tokens!r} would yield 0 — skipped")
            continue
        selected, query = result
        log("INFO", f"after pick: {len(selected)} results, query={query!r}")

    print()
    print(f"{C_GRN}{C_BLD}Final query:{C_OFF} {query}")
    return query, selected, "done"
```

- [ ] **Step 3: Run the refine/exclude tests**

The shim in `test_anirss.py` already wires `auto_resolution`, `compute_groups`, `apply_pick`, `add_exclude_to_query` to `anirss_lib.refine`. Run:

```
uvx pytest test_anirss.py -k "auto_resolution or compute_groups or apply_pick or add_exclude_to_query" -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add anirss_lib/readline_input.py anirss_lib/refine.py
git commit -m "Add anirss_lib.readline_input and anirss_lib.refine"
```

---

## Task 8: qbt/session + qbt/feeds + B2 fix

**Files:**
- Create: `anirss_lib/qbt/session.py`, `anirss_lib/qbt/feeds.py`
- Modify: `test_anirss.py` (fix B2 — three SID-cookie tests)

- [ ] **Step 1: Write `anirss_lib/qbt/session.py`**

Move `QbtSession`, `_make_qbt_opener`, `_is_sid_cookie_name`, `_save_sid`, `_load_sid`, `_drop_sid`, `_effective_cookie_host`, `_make_sid_cookie`, `_try_qbt_sid`, `qbt_login`, `login_with_retry`, `apply_no_seed` verbatim from `anirss.bak`. Update imports:

```python
"""qBittorrent WebUI session + SID cookie persistence."""

import getpass
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from anirss_lib.ansi import C_BLD, C_GRN, C_OFF, C_RED, C_YEL
from anirss_lib.config import (
    QbtConfig, SID_PATH, STATE_DIR,
)
from anirss_lib.logging import die, log


# ... (entire content from anirss.bak lines 1027–1315, with `STATE_DIR` and
# `SID_PATH` already imported above)
```

The body is identical. **No behavior change** in this task except for one new helper used by Task 10 (non-interactive password):

```python
def login_with_password(qbt_cfg: QbtConfig, password: str) -> "QbtSession":
    """Single-shot login — no prompts, no retries. Used by non-interactive flag flow."""
    sess = _try_qbt_sid(qbt_cfg["url"])
    if sess is not None:
        return sess
    qbt, err = qbt_login(qbt_cfg["url"], qbt_cfg["username"], password)
    if qbt is None:
        die(err or "qBittorrent login failed")
    return qbt
```

- [ ] **Step 2: Write `anirss_lib/qbt/feeds.py`**

```python
"""qBittorrent RSS feed cache mirror."""

import datetime

from anirss_lib.config import FEED_CACHE_TTL_SECONDS, FEEDS_CACHE_PATH, STATE_DIR
from anirss_lib.logging import log
from anirss_lib.qbt.session import QbtSession


def write_feed_cache(names: list[str]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FEEDS_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text("".join(name + "\n" for name in sorted(names, key=str.lower)))
        tmp.replace(FEEDS_CACHE_PATH)
        log("INFO", f"wrote feed cache ({len(names)} names) to {FEEDS_CACHE_PATH}")
    except OSError as e:
        log("WARN", f"couldn't write feed cache to {FEEDS_CACHE_PATH}: {e}")


def read_feed_cache() -> list[str]:
    try:
        return [line for line in FEEDS_CACHE_PATH.read_text().splitlines() if line]
    except OSError:
        return []


def feed_cache_age_seconds() -> float | None:
    try:
        return max(0.0, datetime.datetime.now().timestamp() - FEEDS_CACHE_PATH.stat().st_mtime)
    except OSError:
        return None


def is_feed_cache_stale(threshold_seconds: float = FEED_CACHE_TTL_SECONDS) -> bool:
    age = feed_cache_age_seconds()
    return age is None or age > threshold_seconds


def list_qbt_rule_names(qbt: QbtSession) -> list[str]:
    rules = qbt.get_json("/api/v2/rss/rules")
    if not isinstance(rules, dict):
        return []
    return list(rules.keys())


def refresh_feed_cache(qbt: QbtSession) -> list[str]:
    names = list_qbt_rule_names(qbt)
    write_feed_cache(names)
    return names


def maybe_refresh_feed_cache(qbt: QbtSession) -> None:
    if not is_feed_cache_stale():
        return
    try:
        refresh_feed_cache(qbt)
    except Exception as e:  # noqa: BLE001
        log("WARN", f"feed-cache refresh failed: {e}")
```

- [ ] **Step 3: Fix B2 — repair the broken SID-cookie tests**

In `test_anirss.py`, replace the three SID-cookie tests:

```python
def test_sid_cookie_attaches_to_outgoing_request():
    import http.cookiejar
    import urllib.request
    from anirss_lib.qbt.session import _make_sid_cookie

    jar = http.cookiejar.CookieJar()
    jar.set_cookie(_make_sid_cookie("localhost", "SID", "deadbeef123", https=False))

    req = urllib.request.Request("http://localhost:8080/api/v2/app/version")
    jar.add_cookie_header(req)
    assert req.get_header("Cookie") == "SID=deadbeef123"


def test_sid_cookie_v5_name_attaches():
    """qBittorrent v5+ uses QBT_SID_<port>. The cookie must still attach."""
    import http.cookiejar
    import urllib.request
    from anirss_lib.qbt.session import _make_sid_cookie

    jar = http.cookiejar.CookieJar()
    jar.set_cookie(_make_sid_cookie("localhost", "QBT_SID_8080", "abc123", https=False))

    req = urllib.request.Request("http://localhost:8080/api/v2/app/version")
    jar.add_cookie_header(req)
    assert req.get_header("Cookie") == "QBT_SID_8080=abc123"


def test_sid_cookie_does_not_leak_to_other_hosts():
    import http.cookiejar
    import urllib.request
    from anirss_lib.qbt.session import _make_sid_cookie

    jar = http.cookiejar.CookieJar()
    jar.set_cookie(_make_sid_cookie("localhost", "SID", "deadbeef123", https=False))

    other = urllib.request.Request("http://example.com/api/v2/app/version")
    jar.add_cookie_header(other)
    assert other.get_header("Cookie") is None


def test_sid_cookie_https_only_when_https_set():
    import http.cookiejar
    import urllib.request
    from anirss_lib.qbt.session import _make_sid_cookie

    jar = http.cookiejar.CookieJar()
    jar.set_cookie(_make_sid_cookie("localhost", "SID", "abc", https=True))

    http_req = urllib.request.Request("http://localhost:8080/x")
    jar.add_cookie_header(http_req)
    assert http_req.get_header("Cookie") is None

    https_req = urllib.request.Request("https://localhost:8080/x")
    jar.add_cookie_header(https_req)
    assert https_req.get_header("Cookie") == "SID=abc"
```

The `test_effective_cookie_host_munges_only_dotless_hostnames` test stays as-is, just change the import: `from anirss_lib.qbt.session import _effective_cookie_host`.

- [ ] **Step 4: Run the SID-cookie tests**

Run: `uvx pytest test_anirss.py -k "sid_cookie or effective_cookie_host" -v`
Expected: all 5 PASS (the 3 previously-broken ones + new v5-name one + the effective-cookie-host one).

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/qbt/__init__.py anirss_lib/qbt/session.py anirss_lib/qbt/feeds.py test_anirss.py
git commit -m "Move qBittorrent session + feed cache to anirss_lib.qbt; fix SID-cookie tests"
```

---

## Task 9: qbt/actions + B3 fix

**Files:**
- Create: `anirss_lib/qbt/actions.py`

- [ ] **Step 1: Write `anirss_lib/qbt/actions.py`**

Move the do_*/apply_*/cmd_remove/_safe_rmtree/_norm_path/_human_bytes/_dir_* helpers from `anirss.bak`. **Apply the B3 fix during the move**: in `cmd_remove`, line ~1851 of the original:

Original:
```python
"torrent_count_in_qb": len(_torrents_for_savepath(qbt, save_path)) if drop_files and not drop_torrents else len(torrents),
```

Replaced with:
```python
"torrent_count_in_qb": len(torrents),
```

Justification: `torrents` was already populated earlier on whenever `(drop_torrents or drop_files)` is true, and one of those is always true in this branch. The conditional fetch always produces the same value as `len(torrents)`.

The full file:

```python
"""qBittorrent-side actions: add torrents, manage subscriptions, remove rules."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from anirss_lib.ansi import (
    C_BLD, C_CYN, C_DIM, C_GRN, C_OFF, C_RED, C_YEL, FZF_BINDS,
)
from anirss_lib.config import CONFIG_PATH, QbtConfig, STATE_DIR
from anirss_lib.logging import die, log
from anirss_lib.qbt.feeds import refresh_feed_cache, write_feed_cache
from anirss_lib.qbt.session import QbtSession, login_with_retry


# --- path utilities ---

def _norm_path(p: str) -> str:
    return os.path.normpath(os.path.expanduser(p)).rstrip("/")


def _human_bytes(n: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} {u}"
        f /= 1024.0
    return f"{n} B"


def _dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _dir_file_count(path: str) -> int:
    if not os.path.isdir(path):
        return 0
    total = 0
    for _root, _dirs, files in os.walk(path, followlinks=False):
        total += len(files)
    return total


def _safe_rmtree(path: str) -> None:
    p = _norm_path(path)
    home = _norm_path(str(Path.home()))
    forbidden = {"", "/", home, _norm_path(str(STATE_DIR)),
                 _norm_path(str(CONFIG_PATH.parent))}
    if p in forbidden or len(p) <= 1:
        die(f"refusing to rmtree suspicious path: {path!r}")
    try:
        shutil.rmtree(p)
    except OSError as e:
        die(f"rmtree {p}: {e}")


# --- torrent add ---

def _torrents_add_error(body: str) -> str | None:
    body = body.strip()
    if body == "Ok.":
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return f"qBittorrent torrents/add returned {body!r}"
    if not isinstance(data, dict):
        return f"qBittorrent torrents/add returned {body!r}"
    failure = int(data.get("failure_count", 0) or 0)
    if failure:
        return f"qBittorrent torrents/add reported {failure} failure(s): {body!r}"
    return None


def do_subscribe(qbt: QbtSession, feed_url: str, name: str, save_base: str) -> None:
    save_path = os.path.join(save_base, name)
    print(f"{C_CYN}==>{C_OFF} adding feed {C_BLD}{name}{C_OFF}")
    qbt.post("/api/v2/rss/addFeed", url=feed_url, path=name)
    rule = {
        "enabled": True, "mustContain": "", "mustNotContain": "",
        "useRegex": False, "episodeFilter": "",
        "smartFilter": False, "previouslyMatchedEpisodes": [],
        "affectedFeeds": [feed_url],
        "ignoreDays": 0, "lastMatch": "",
        "addPaused": None, "assignedCategory": "",
        "savePath": save_path,
    }
    print(f"{C_CYN}==>{C_OFF} adding rule  {C_BLD}{name}{C_OFF} -> {save_path}")
    qbt.post("/api/v2/rss/setRule", ruleName=name, ruleDef=json.dumps(rule))


def do_download(qbt: QbtSession, links: list[str], name: str, save_base: str) -> None:
    save_path = os.path.join(save_base, name)
    print(f"{C_CYN}==>{C_OFF} adding {len(links)} torrent(s) -> {save_path}")
    body = qbt.post("/api/v2/torrents/add",
                    urls="\n".join(links), savepath=save_path)
    err = _torrents_add_error(body)
    if err:
        die(err)


def do_movie(qbt: QbtSession, title: str, link: str, movie_path: str) -> None:
    print(f"{C_CYN}==>{C_OFF} downloading movie -> {movie_path}")
    print(f"    {C_DIM}{title}{C_OFF}")
    body = qbt.post("/api/v2/torrents/add", urls=link, savepath=movie_path)
    err = _torrents_add_error(body)
    if err:
        die(err)


# --- remove flow internals exported for cli/commands.py ---

def _find_feed_path(qbt: QbtSession, rule_url: str) -> str | None:
    items = qbt.get_json("/api/v2/rss/items", withData="false")

    def walk(node, prefix: str) -> str | None:
        if isinstance(node, dict):
            if "url" in node and isinstance(node.get("url"), str) and node["url"] == rule_url:
                return prefix.rstrip("\\")
            for key, child in node.items():
                hit = walk(child, f"{prefix}{key}\\")
                if hit is not None:
                    return hit
        return None

    return walk(items, "")


def _torrents_for_savepath(qbt: QbtSession, save_path: str) -> list[dict]:
    norm = _norm_path(save_path)
    info = qbt.get_json("/api/v2/torrents/info")
    if not isinstance(info, list):
        return []
    return [t for t in info if isinstance(t, dict)
            and _norm_path(t.get("save_path", "")) == norm]
```

- [ ] **Step 2: Smoke-import**

Run: `python3 -c "from anirss_lib.qbt import actions; print(actions.do_subscribe.__name__)"`
Expected: `do_subscribe`.

- [ ] **Step 3: Commit**

```bash
git add anirss_lib/qbt/actions.py
git commit -m "Move qBittorrent actions to anirss_lib.qbt.actions; fix cmd_remove double-fetch"
```

---

## Task 10: cli/urls + cli/args + cli/pickers + cli/commands

**Files:**
- Create: `anirss_lib/cli/urls.py`, `anirss_lib/cli/args.py`, `anirss_lib/cli/pickers.py`, `anirss_lib/cli/commands.py`
- Test: `tests/test_cli_urls.py`, `tests/test_cli_args.py`

- [ ] **Step 1: Failing test for `classify_url`**

Create `tests/test_cli_urls.py`:
```python
import pytest
from anirss_lib.cli.urls import classify_url, UrlKind


@pytest.mark.parametrize("url,kind", [
    ("magnet:?xt=urn:btih:abc", UrlKind.ONE_SHOT),
    ("https://example.com/x.torrent", UrlKind.ONE_SHOT),
    ("http://example.com/x.torrent?token=1", UrlKind.ONE_SHOT),
    ("https://nyaa.si/?page=rss&q=Frieren", UrlKind.NYAA_RSS),
    ("https://sukebei.nyaa.si/", UrlKind.NYAA_RSS),
    ("https://example.com/", UrlKind.OTHER_HTTP),
    ("not a url", UrlKind.NOT_URL),
    ("", UrlKind.NOT_URL),
])
def test_classify_url(url, kind):
    assert classify_url(url) == kind
```

Run: `uvx pytest tests/test_cli_urls.py -v` → FAIL (no module).

- [ ] **Step 2: Implement `anirss_lib/cli/urls.py`**

```python
"""URL classification + nyaa query extraction."""

import enum
import re
import urllib.parse


class UrlKind(enum.Enum):
    NOT_URL = enum.auto()
    ONE_SHOT = enum.auto()
    NYAA_RSS = enum.auto()
    OTHER_HTTP = enum.auto()


_TORRENT_RE = re.compile(r"\.torrent($|[?#])", re.IGNORECASE)


def classify_url(s: str) -> UrlKind:
    if not s:
        return UrlKind.NOT_URL
    if s.startswith("magnet:"):
        return UrlKind.ONE_SHOT
    if not s.startswith(("http://", "https://")):
        return UrlKind.NOT_URL
    if _TORRENT_RE.search(s):
        return UrlKind.ONE_SHOT
    try:
        host = (urllib.parse.urlparse(s).hostname or "").lower()
    except ValueError:
        return UrlKind.OTHER_HTTP
    if host == "nyaa.si" or host.endswith(".nyaa.si"):
        return UrlKind.NYAA_RSS
    return UrlKind.OTHER_HTTP


def extract_nyaa_query(url: str) -> str | None:
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except ValueError:
        return None
    values = qs.get("q") or []
    if not values:
        return None
    q = values[0].strip()
    return q or None
```

Run: `uvx pytest tests/test_cli_urls.py -v` → PASS.

- [ ] **Step 3: Write `anirss_lib/cli/args.py`**

```python
"""Argument parsing: pacman-style op flags + non-interactive action flags."""

from dataclasses import dataclass, field

from anirss_lib.logging import die


@dataclass
class ParsedArgs:
    """Structured result of parse_cli_args. Only `positional` and the flag
    booleans are filled; meta-flags (--help, --version, --config, …) are
    handled separately in main._handle_meta_flags before this runs."""
    positional: list[str] = field(default_factory=list)
    noconfirm: bool = False
    name: str | None = None
    password_stdin: bool = False

    # action flags (mutually exclusive)
    subscribe: bool = False
    download_all: bool = False
    download_n: int | None = None
    movie: bool = False

    @property
    def action_flag_count(self) -> int:
        return sum([
            self.subscribe, self.download_all,
            self.download_n is not None, self.movie,
        ])

    @property
    def non_interactive(self) -> bool:
        return self.action_flag_count > 0


def parse_op_flag(arg: str) -> tuple[str, set[str]] | None:
    """Recognize pacman-style bundled op flags: -Q, -Qj, -S, -Sy, -R, -Rs, -Rn, -Rns."""
    if not arg.startswith("-") or arg.startswith("--") or len(arg) < 2:
        return None
    body = arg[1:]
    if not body or not body[0].isalpha():
        return None
    op = body[0]
    if op not in ("Q", "S", "R"):
        return None
    mods = set(body[1:])
    allowed = {"Q": {"j"}, "S": {"y"}, "R": {"n", "s"}}
    bad = mods - allowed[op]
    if bad:
        die(f"unknown modifier(s) for -{op}: {''.join(sorted(bad))!r} "
            f"(valid: {''.join(sorted(allowed[op])) or '<none>'})")
    return op, mods


def parse_cli_args(argv: list[str]) -> ParsedArgs:
    """Pull out long-form flags (--subscribe, --download-all, --download N,
    --movie, --name X, --password-stdin, --noconfirm). Leaves op flags
    (-Q/-S/-R*), URLs, and free-form queries in `positional`.

    Errors (via die) on bad N for --download or two competing action flags.
    """
    out = ParsedArgs()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--noconfirm":
            out.noconfirm = True
        elif a == "--subscribe":
            out.subscribe = True
        elif a == "--download-all":
            out.download_all = True
        elif a == "--movie":
            out.movie = True
        elif a == "--password-stdin":
            out.password_stdin = True
        elif a == "--name":
            i += 1
            if i >= len(argv):
                die("--name requires a value")
            out.name = argv[i]
        elif a.startswith("--name="):
            out.name = a.split("=", 1)[1]
        elif a == "--download":
            i += 1
            if i >= len(argv):
                die("--download requires N")
            try:
                out.download_n = int(argv[i])
            except ValueError:
                die(f"--download expects an integer, got {argv[i]!r}")
            if out.download_n < 1:
                die(f"--download N must be >= 1, got {out.download_n}")
        elif a.startswith("--download="):
            try:
                out.download_n = int(a.split("=", 1)[1])
            except ValueError:
                die(f"--download expects an integer, got {a.split('=', 1)[1]!r}")
        else:
            out.positional.append(a)
        i += 1

    if out.action_flag_count > 1:
        die("at most one of --subscribe, --download-all, --download, --movie")
    return out
```

- [ ] **Step 4: Tests for `parse_cli_args`**

Create `tests/test_cli_args.py`:
```python
import pytest
from anirss_lib.cli import args as a


def test_parse_subscribe_with_query():
    p = a.parse_cli_args(["--subscribe", "Frieren"])
    assert p.subscribe
    assert p.positional == ["Frieren"]
    assert p.non_interactive


def test_parse_download_n():
    p = a.parse_cli_args(["--download", "5", "Frieren"])
    assert p.download_n == 5
    assert p.positional == ["Frieren"]


def test_parse_download_equals_form():
    p = a.parse_cli_args(["--download=3", "Frieren"])
    assert p.download_n == 3


def test_parse_name():
    p = a.parse_cli_args(["--subscribe", "--name", "Frieren-Anime", "Frieren"])
    assert p.name == "Frieren-Anime"
    assert p.positional == ["Frieren"]


def test_parse_name_equals_form():
    p = a.parse_cli_args(["--name=Frieren", "--subscribe"])
    assert p.name == "Frieren"


def test_parse_password_stdin():
    p = a.parse_cli_args(["--password-stdin", "--subscribe", "Frieren"])
    assert p.password_stdin


def test_parse_rejects_two_action_flags(monkeypatch):
    captured = {}
    def fake_die(msg):
        captured["msg"] = msg
        raise SystemExit(1)
    monkeypatch.setattr(a, "die", fake_die)
    with pytest.raises(SystemExit):
        a.parse_cli_args(["--subscribe", "--movie", "Frieren"])
    assert "at most one" in captured["msg"]


def test_parse_rejects_non_integer_n(monkeypatch):
    monkeypatch.setattr(a, "die", lambda m: (_ for _ in ()).throw(SystemExit(1)))
    with pytest.raises(SystemExit):
        a.parse_cli_args(["--download", "x", "Frieren"])


def test_parse_no_action_flags_means_interactive():
    p = a.parse_cli_args(["Frieren"])
    assert not p.non_interactive
    assert p.positional == ["Frieren"]
```

Run: `uvx pytest tests/test_cli_args.py -v` → PASS.

- [ ] **Step 5: Write `anirss_lib/cli/pickers.py`**

Move `pick_action`, `pick_downloads`, `pick_movie` from `anirss.bak`. Update imports:

```python
"""Interactive pickers for action / downloads / movie. Skipped in non-interactive mode."""

import shutil
import subprocess

from anirss_lib.ansi import (
    C_BLD, C_CYN, C_DIM, C_GRN, C_MAG, C_OFF, C_RED, C_YEL,
    FZF_BINDS, FZF_HL_COLORS, PROMPT_ACTION, ansi_strip, right_anchor,
)
from anirss_lib import terminal
from anirss_lib.format import category_chip, colorize_title, format_stats
from anirss_lib.fzf import _parse_fzf_output, fzf_pick_one
from anirss_lib.types import Item


ACT_SUB, ACT_DL_PICK, ACT_DL_ALL, ACT_MOVIE, ACT_CANCEL, ACT_BACK = (
    "subscribe", "download_pick", "download_all", "movie", "cancel", "back",
)


def pick_action(n_items: int) -> str:
    # (copy verbatim from anirss.bak lines 1380-1404)
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
    """(copy verbatim from anirss.bak lines 1407-1476, replacing
    `shutil.get_terminal_size((120, 24))` with `terminal.get_size()`)"""
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
        "fzf", "--ansi", "--multi",
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
```

- [ ] **Step 6: Write `anirss_lib/cli/commands.py`**

Move `cmd_sync`, `cmd_query`, `cmd_remove`, `_resolve_remove_targets` from `anirss.bak`. **Note**: `cmd_remove` was already cleaned up in Task 9 — copy that fixed version. Same for `_torrents_add_error` (already in qbt.actions).

```python
"""High-level commands: cmd_query (-Q), cmd_sync (-Sy), cmd_remove (-R*)."""

import os
import shutil
import subprocess
import json

from anirss_lib.ansi import (
    C_BLD, C_CYN, C_DIM, C_GRN, C_OFF, C_RED, C_YEL, FZF_BINDS,
)
from anirss_lib.config import FEEDS_CACHE_PATH, QbtConfig
from anirss_lib.logging import die, log
from anirss_lib.qbt.actions import (
    _dir_file_count, _dir_size_bytes, _find_feed_path, _human_bytes,
    _norm_path, _safe_rmtree, _torrents_for_savepath,
)
from anirss_lib.qbt.feeds import refresh_feed_cache, write_feed_cache
from anirss_lib.qbt.session import QbtSession, login_with_retry


def cmd_sync(qbt_cfg: QbtConfig) -> None:
    print(f"{C_CYN}==>{C_OFF} syncing feed cache from qBittorrent")
    qbt = login_with_retry(qbt_cfg)
    names = refresh_feed_cache(qbt)
    print(f"{C_GRN}OK:{C_OFF} cached {len(names)} feed name(s) at {FEEDS_CACHE_PATH}")


def cmd_query(qbt_cfg: QbtConfig, json_format: bool) -> None:
    # body identical to original lines 1722-1792
    qbt = login_with_retry(qbt_cfg)
    rules = qbt.get_json("/api/v2/rss/rules") or {}
    if not isinstance(rules, dict):
        rules = {}
    info = qbt.get_json("/api/v2/torrents/info")
    write_feed_cache(list(rules.keys()))

    if not rules:
        if json_format:
            print("[]")
        else:
            print(f"{C_DIM}(no feeds subscribed){C_OFF}")
        return

    counts: dict[str, int] = {}
    if isinstance(info, list):
        for t in info:
            if isinstance(t, dict):
                sp = _norm_path(t.get("save_path", ""))
                counts[sp] = counts.get(sp, 0) + 1

    if json_format:
        out: list[dict] = []
        for name, rule in sorted(rules.items(), key=lambda kv: kv[0].lower()):
            feeds = rule.get("affectedFeeds") or []
            save_path = rule.get("savePath") or ""
            torrent_count = counts.get(_norm_path(save_path), 0)
            file_count = _dir_file_count(save_path)
            out.append({
                "name": name,
                "feed_url": feeds[0] if feeds else "",
                "save_path": save_path,
                "rule_enabled": bool(rule.get("enabled", True)),
                "torrent_count": torrent_count,
                "file_count": file_count,
            })
        print(json.dumps(out, indent=2))
        return

    for name in sorted(rules, key=str.lower):
        rule = rules[name]
        save_path = rule.get("savePath") or ""
        n_torrents = counts.get(_norm_path(save_path), 0)
        n_files = _dir_file_count(save_path)
        t_unit = "torrent" if n_torrents == 1 else "torrents"
        f_unit = "file" if n_files == 1 else "files"
        t_color = f"{C_BLD}{C_YEL}" if n_torrents > 0 else C_DIM
        f_color = f"{C_BLD}{C_YEL}" if n_files > 0 else C_DIM
        disabled = "" if rule.get("enabled", True) else f"{C_RED}[disabled]{C_OFF} "
        print(
            f"{disabled}{C_BLD}{C_CYN}{name}{C_OFF} "
            f"{C_DIM}({C_OFF}{t_color}{n_torrents}{C_OFF}{C_DIM} {t_unit}){C_OFF}"
        )
        print(
            f"  {C_DIM}{C_CYN}└─{C_OFF}{C_GRN}{save_path}{C_OFF} "
            f"{C_DIM}({C_OFF}{f_color}{n_files}{C_OFF}{C_DIM} {f_unit}){C_OFF}"
        )


def _resolve_remove_targets(args: list[str], qbt: QbtSession | None) -> list[str]:
    if args:
        return args
    if qbt is None or not shutil.which("fzf"):
        die("no targets given (and fzf not available for interactive picker)")
    rules = qbt.get_json("/api/v2/rss/rules") or {}
    names = sorted(rules.keys(), key=str.lower) if isinstance(rules, dict) else []
    if not names:
        die("no feeds to remove")
    proc = subprocess.run(
        ["fzf", "--multi", "--prompt", "remove > ",
         "--header", "Tab/Space marks. Enter confirms. Esc cancels.",
         "--bind", FZF_BINDS],
        input="\n".join(names), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        die("cancelled")
    chosen = [line for line in proc.stdout.splitlines() if line.strip()]
    if not chosen:
        die("no targets selected")
    return chosen


def cmd_remove(qbt_cfg: QbtConfig, args: list[str], drop_torrents: bool,
               drop_files: bool, noconfirm: bool, save_base: str) -> None:
    qbt = login_with_retry(qbt_cfg)
    targets = _resolve_remove_targets(args, qbt)

    rules = qbt.get_json("/api/v2/rss/rules") or {}
    if not isinstance(rules, dict):
        rules = {}

    plans: list[dict] = []
    missing: list[str] = []
    for name in targets:
        rule = rules.get(name)
        if not isinstance(rule, dict):
            missing.append(name)
            continue
        feeds = rule.get("affectedFeeds") or []
        feed_url = feeds[0] if feeds else ""
        save_path = _norm_path(rule.get("savePath") or os.path.join(save_base, name))
        feed_path = _find_feed_path(qbt, feed_url) if feed_url else None
        torrents = _torrents_for_savepath(qbt, save_path) if (drop_torrents or drop_files) else []
        size_bytes = _dir_size_bytes(save_path) if (drop_files and os.path.isdir(save_path)) else 0
        plans.append({
            "name": name,
            "feed_url": feed_url,
            "feed_path": feed_path,
            "save_path": save_path,
            "torrents": torrents,
            "size_bytes": size_bytes,
            "torrent_count_in_qb": len(torrents),  # B3 fix: was a redundant fetch
        })
    if missing:
        die(f"unknown feed(s): {', '.join(missing)} (run `anirss -Q` to list)")

    print()
    for p in plans:
        print(f"{C_BLD}About to remove:{C_OFF}")
        print(f"  {C_CYN}Feed:{C_OFF}    {p['name']}")
        print(f"  {C_CYN}Rule:{C_OFF}    {p['name']}")
        if drop_torrents:
            kept = "files deleted" if drop_files else "files kept"
            print(f"  {C_CYN}Torrents:{C_OFF} {len(p['torrents'])} in qB "
                  f"(will be removed from qB, {kept})")
        elif drop_files and p["torrent_count_in_qb"]:
            print(f"  {C_YEL}Note:{C_OFF}    {p['torrent_count_in_qb']} torrent(s) "
                  f"will be left in qB and will error (-n without -s)")
        if drop_files:
            shown = p["save_path"] if os.path.isdir(p["save_path"]) \
                else f"{p['save_path']} {C_DIM}(does not exist){C_OFF}"
            print(f"  {C_CYN}Files:{C_OFF}   {shown}  "
                  f"{C_DIM}({_human_bytes(p['size_bytes'])}){C_OFF}")
        print()

    if not noconfirm:
        try:
            reply = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            die("cancelled")
        if reply not in ("y", "yes"):
            print(f"{C_DIM}cancelled.{C_OFF}")
            return

    for p in plans:
        if drop_torrents and p["torrents"]:
            hashes = "|".join(t["hash"] for t in p["torrents"] if isinstance(t.get("hash"), str))
            qbt.post("/api/v2/torrents/delete", hashes=hashes,
                     deleteFiles=("true" if drop_files else "false"))
            print(f"{C_GRN}OK:{C_OFF} removed {len(p['torrents'])} torrent(s) for {p['name']}")
        if p["feed_path"]:
            qbt.post("/api/v2/rss/removeItem", path=p["feed_path"])
            print(f"{C_GRN}OK:{C_OFF} removed feed {p['name']}")
        elif p["feed_url"]:
            log("WARN", f"no qB feed-path matched url={p['feed_url']!r} for rule {p['name']!r}")
        qbt.post("/api/v2/rss/removeRule", ruleName=p["name"])
        print(f"{C_GRN}OK:{C_OFF} removed rule {p['name']}")
        if drop_files and not drop_torrents and os.path.isdir(p["save_path"]):
            _safe_rmtree(p["save_path"])
            print(f"{C_GRN}OK:{C_OFF} deleted files at {p['save_path']}")

    refresh_feed_cache(qbt)
```

- [ ] **Step 7: Update test_anirss.py shim to expose new is_nyaa_url/extract_nyaa_query and parse_op_flag**

Add to the shim class in `test_anirss.py`:

```python
from anirss_lib.cli import args as _ca
from anirss_lib.cli.urls import classify_url, UrlKind, extract_nyaa_query as _extract

# … inside _Anirss …
    parse_op_flag = _ca.parse_op_flag
    extract_nyaa_query = _extract
    is_nyaa_url = staticmethod(lambda s: classify_url(s) == UrlKind.NYAA_RSS)

# Path utilities used by tests
from anirss_lib.qbt import actions as _qa
# … inside _Anirss …
    _norm_path = staticmethod(_qa._norm_path)
    _human_bytes = staticmethod(_qa._human_bytes)

# Feed cache state used by tests via monkeypatch
from anirss_lib import config as _cfg
from anirss_lib.qbt import feeds as _qfeeds
# Add module-level proxies so the existing tests' monkeypatch targets keep working:
FEEDS_CACHE_PATH = _cfg.FEEDS_CACHE_PATH
STATE_DIR = _cfg.STATE_DIR
write_feed_cache = _qfeeds.write_feed_cache
read_feed_cache = _qfeeds.read_feed_cache
feed_cache_age_seconds = _qfeeds.feed_cache_age_seconds
is_feed_cache_stale = _qfeeds.is_feed_cache_stale
```

The existing `test_feed_cache_round_trip`, `test_feed_cache_age_when_missing`,
`test_feed_cache_stale_threshold` already do
`monkeypatch.setattr(anirss, "FEEDS_CACHE_PATH", …)`. With the shim, those
patches now hit the shim's module-level attributes — but the real
implementations in `anirss_lib.qbt.feeds` still read from
`anirss_lib.config.FEEDS_CACHE_PATH`. To keep the existing tests working
unchanged, patch them to monkeypatch `anirss_lib.config` instead:

```python
def test_feed_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "FEEDS_CACHE_PATH", tmp_path / "feeds.txt")
    monkeypatch.setattr(_cfg, "STATE_DIR", tmp_path)
    # qbt.feeds reads these by attribute access at call time, so patching
    # the module they're imported from is the right move.
    _qfeeds.write_feed_cache(["b feed", "A feed", "c feed"])
    assert _qfeeds.read_feed_cache() == ["A feed", "b feed", "c feed"]
```

(Apply the same `_cfg.…` monkeypatch swap to the other two feed-cache tests.)

- [ ] **Step 8: Run all tests**

Run: `uvx pytest -q`
Expected: **all green** (50 from `test_anirss.py` minus those replaced by the new SID-cookie tests, plus all new tests). The only thing still loading `anirss.bak` is `test_anirss.py`'s shim header — which now also loads `anirss_lib` modules directly.

If any test fails, fix and re-run before committing.

- [ ] **Step 9: Commit**

```bash
git add anirss_lib/cli tests/test_cli_args.py tests/test_cli_urls.py test_anirss.py
git commit -m "Add anirss_lib.cli.{urls,args,pickers,commands}"
```

---

## Task 11: main + state machine + non-interactive flags

**Files:**
- Modify: `anirss_lib/main.py` (replace the stub)
- Test: `tests/test_main_noninteractive.py`

- [ ] **Step 1: Failing tests for the non-interactive helpers**

Create `tests/test_main_noninteractive.py`:
```python
import pytest
from anirss_lib import main
from anirss_lib.types import Item


def _items(n):
    """n synthetic items with downloads = 100, 99, 98, …"""
    return [Item(title=f"Show ep{i} 1080p", link=f"link-{i}",
                 downloads=100 - i, seeders=10)
            for i in range(n)]


def test_pick_top_n_sorts_by_downloads():
    items = _items(5)
    chosen = main._pick_top_n(items, 3)
    assert len(chosen) == 3
    assert chosen[0].downloads == 100
    assert chosen[1].downloads == 99
    assert chosen[2].downloads == 98


def test_pick_top_n_clamps_to_available():
    items = _items(3)
    chosen = main._pick_top_n(items, 10)
    assert len(chosen) == 3


def test_resolve_password_env(monkeypatch):
    monkeypatch.setenv("ANIRSS_QBT_PASSWORD", "secret")
    assert main._resolve_password(password_stdin=False) == "secret"


def test_resolve_password_stdin(monkeypatch):
    import io
    monkeypatch.delenv("ANIRSS_QBT_PASSWORD", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("from-stdin\n"))
    assert main._resolve_password(password_stdin=True) == "from-stdin"


def test_resolve_password_neither_returns_none(monkeypatch):
    monkeypatch.delenv("ANIRSS_QBT_PASSWORD", raising=False)
    assert main._resolve_password(password_stdin=False) is None
```

Run: `uvx pytest tests/test_main_noninteractive.py -v` → FAIL (functions not defined).

- [ ] **Step 2: Implement `anirss_lib/main.py`**

```python
"""Top-level dispatch: argv parsing, state machine, action dispatch."""

import json
import os
import shutil
import sys

from anirss_lib import __version__, format as fmt, terminal
from anirss_lib.ansi import (
    C_BLD, C_DIM, C_GRN, C_OFF, C_YEL, PROMPT_SEARCH,
)
from anirss_lib.cli.args import ParsedArgs, parse_cli_args, parse_op_flag
from anirss_lib.cli.commands import cmd_query, cmd_remove, cmd_sync
from anirss_lib.cli.pickers import (
    ACT_BACK, ACT_CANCEL, ACT_DL_ALL, ACT_DL_PICK, ACT_MOVIE, ACT_SUB,
    pick_action, pick_downloads, pick_movie,
)
from anirss_lib.cli.urls import UrlKind, classify_url, extract_nyaa_query
from anirss_lib.config import (
    AnirssConfig, CONFIG_PATH, load_config, migrate_config,
)
from anirss_lib.format import (
    category_chip, colorize_title, format_stats, show_titles,
)
from anirss_lib.fzf import fzf_search_prompt
from anirss_lib.logging import die, init_log, log
from anirss_lib.nyaa import _fetch_items_from_url, fetch_items, search_url
from anirss_lib.qbt.actions import apply_no_seed, do_download, do_movie, do_subscribe
from anirss_lib.qbt.feeds import maybe_refresh_feed_cache
from anirss_lib.qbt.session import login_with_password, login_with_retry
from anirss_lib.readline_input import get_name, setup_readline
from anirss_lib.refine import refine
from anirss_lib.titles import show_name
from anirss_lib.types import Item


# ---- small helpers ----

def _pick_top_n(items: list[Item], n: int) -> list[Item]:
    """Top-N by downloads, clamped to len(items). Stable for ties."""
    return sorted(items, key=lambda i: i.downloads, reverse=True)[:n]


def _resolve_password(*, password_stdin: bool) -> str | None:
    pw = os.environ.get("ANIRSS_QBT_PASSWORD")
    if pw:
        return pw
    if password_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\n") or None
    return None


def _login_for(parsed: ParsedArgs, cfg: AnirssConfig):
    """Pick the right login flow based on whether we're non-interactive."""
    if not parsed.non_interactive:
        return login_with_retry(cfg["qbittorrent"])
    pw = _resolve_password(password_stdin=parsed.password_stdin)
    if pw is None:
        # Try the SID cache. If that also fails, login_with_password will die.
        pw = ""  # forces qbt_login to fail clearly when SID cache is empty
    return login_with_password(cfg["qbittorrent"], pw)


# ---- meta flags (--help/--version/etc) ----

def _print_search_rss_rows(query: str, cfg: AnirssConfig) -> None:
    """Hidden self-invocation handler called by fzf's reload binding."""
    items = fetch_items(query, cfg["search"])
    width = max(40, terminal.get_size().columns - 3)
    from anirss_lib.ansi import right_anchor
    for item in items:
        line = f"{format_stats(item)}  {colorize_title(item.title)}"
        if item.category:
            line = right_anchor(line, category_chip(item), width)
        print(line)


def _handle_meta_flags(argv: list[str], cfg: AnirssConfig) -> bool:
    """Returns True if a meta flag was found and handled (caller returns)."""
    if not argv:
        return False
    a = argv[0]
    if a in ("-h", "--help"):
        print(__doc__ or "anirss")
        return True
    if a in ("-V", "--version"):
        print(f"anirss {__version__}")
        return True
    if a in ("--no-seed", "--apply-no-seed"):
        apply_no_seed(cfg["qbittorrent"])
        return True
    if a == "--config":
        print(f"config path: {CONFIG_PATH}")
        print(json.dumps(cfg, indent=2))
        return True
    if a == "--migrate-config":
        migrate_config()
        return True
    if a == "--_search-rss":
        query = " ".join(argv[1:]).strip()
        if query:
            _print_search_rss_rows(query, cfg)
        return True
    return False


# ---- op-flag dispatch (-Q/-S/-R) ----

def _handle_op_flag(argv: list[str], cfg: AnirssConfig, parsed: ParsedArgs
                    ) -> tuple[bool, str | None]:
    """Returns (handled, force_action_menu_url).

    - handled=True means the op flag terminated execution (e.g. -Q, -Sy, -R*).
    - force_action_menu_url is set when `-S <url>` was given and the URL flow
      must show the action menu (interactive case).
    """
    if not argv:
        return False, None
    op_parsed = parse_op_flag(argv[0])
    if op_parsed is None:
        return False, None
    op, mods = op_parsed
    rest = argv[1:]
    if op == "Q":
        cmd_query(cfg["qbittorrent"], json_format=("j" in mods))
        return True, None
    if op == "S" and "y" in mods:
        cmd_sync(cfg["qbittorrent"])
        return True, None
    if op == "S":
        if not rest:
            die("usage: anirss -S <rss-url>")
        url = rest[0]
        if classify_url(url) == UrlKind.NOT_URL:
            die(f"-S expects a URL, got {url!r}")
        return False, url
    if op == "R":
        cmd_remove(cfg["qbittorrent"], args=rest,
                   drop_torrents=("s" in mods),
                   drop_files=("n" in mods),
                   noconfirm=parsed.noconfirm,
                   save_base=cfg["downloads"]["save_base"])
        return True, None
    return False, None


# ---- search state machine (interactive) ----

def _run_search_state_machine(initial_query: str, cfg: AnirssConfig
                              ) -> tuple[str, list[Item], str]:
    """Loops search → fetch → refine → action picker. Returns
    (final_query, final_items, action). Caller proceeds based on `action`.
    """
    last_search_query = initial_query
    items: list[Item] = []
    query: str = ""
    selected: list[Item] = []
    action: str = ACT_CANCEL
    state = "fetch" if initial_query else "search"

    while True:
        if state == "search":
            result = fzf_search_prompt(PROMPT_SEARCH, default=last_search_query)
            if result is None:
                return "", [], ACT_CANCEL
            if not result:
                print(f"{C_DIM}(empty — type something or Esc to quit){C_OFF}")
                continue
            last_search_query = result
            initial_query = result
            state = "fetch"
            continue
        if state == "fetch":
            print(f"{C_BLD}Query:{C_OFF} {initial_query}")
            print(f"{C_DIM}fetching nyaa.si...{C_OFF}")
            items = fetch_items(initial_query, cfg["search"])
            if not items:
                print(f"{C_YEL}no results for {initial_query!r} — edit and try again "
                      f"({C_DIM}↑ recalls last query{C_OFF}{C_YEL}){C_OFF}")
                state = "search"
                continue
            query, selected = initial_query, items
            state = "refine"
            continue
        if state == "refine":
            query, selected, status = refine(query, selected, cfg["search"])
            if status == "back":
                state = "search"
                continue
            state = "action"
            continue
        # state == "action"
        action = pick_action(len(selected))
        log("INFO", f"action={action}")
        if action == ACT_BACK:
            state = "refine"
            continue
        break

    return query, selected, action


# ---- non-interactive action runner ----

def _run_noninteractive(parsed: ParsedArgs, cfg: AnirssConfig,
                        initial_query: str, force_url: str | None) -> None:
    """Execute the chosen action without ever opening fzf or prompting."""
    if force_url is not None:
        items = _fetch_items_from_url(force_url)
        if not items:
            die(f"no items returned by {force_url}")
        query = ""
        feed_url_for_sub = force_url
    elif initial_query:
        items = fetch_items(initial_query, cfg["search"])
        if not items:
            die(f"no results for {initial_query!r}")
        query = initial_query
        feed_url_for_sub = search_url(initial_query, cfg["search"])
    else:
        die("non-interactive action requires a query or -S <url>")

    default_name = parsed.name or (show_name(items[0].title) or initial_query) or "anirss"
    qbt = _login_for(parsed, cfg)

    if parsed.subscribe:
        do_subscribe(qbt, feed_url_for_sub, default_name, cfg["downloads"]["save_base"])
    elif parsed.download_all:
        links = [it.link for it in items]
        do_download(qbt, links, default_name, cfg["downloads"]["save_base"])
    elif parsed.download_n is not None:
        chosen = _pick_top_n(items, parsed.download_n)
        links = [it.link for it in chosen]
        do_download(qbt, links, default_name, cfg["downloads"]["save_base"])
    elif parsed.movie:
        top = _pick_top_n(items, 1)[0]
        do_movie(qbt, top.title, top.link, cfg["downloads"]["movie_path"])

    maybe_refresh_feed_cache(qbt)
    log("INFO", "done (non-interactive)")
    print(f"{C_GRN}done.{C_OFF}")


# ---- interactive action runner ----

def _run_interactive(initial_query: str, force_url: str | None,
                     parsed: ParsedArgs, cfg: AnirssConfig) -> None:
    """The original search→refine→action picker flow, plus -S URL flow."""
    feed_url: str | None = None
    download_links: list[str] | None = None
    movie_choice: Item | None = None
    default_name: str | None = None

    if force_url is not None:
        print(f"{C_BLD}Subscribe URL:{C_OFF} {force_url}")
        print(f"{C_DIM}fetching feed...{C_OFF}")
        items = _fetch_items_from_url(force_url)
        if not items:
            die(f"no items returned by {force_url}")
        print(f"{C_BLD}{len(items)} item(s):{C_OFF}")
        show_titles(items)
        action = pick_action(len(items))
        log("INFO", f"action={action}")
        if action in (ACT_CANCEL, ACT_BACK):
            print(f"{C_DIM}cancelled.{C_OFF}")
            return
        default_name = show_name(items[0].title) or force_url
        if action == ACT_SUB:
            feed_url = force_url
        elif action == ACT_DL_ALL:
            download_links = [it.link for it in items]
        elif action == ACT_DL_PICK:
            chosen = pick_downloads(items)
            if not chosen:
                print(f"{C_DIM}cancelled.{C_OFF}")
                return
            download_links = [it.link for it in chosen]
            default_name = show_name(chosen[0].title) or default_name
        else:
            movie_choice = pick_movie(items)
            if movie_choice is None:
                print(f"{C_DIM}cancelled.{C_OFF}")
                return
    else:
        # URL classification of the bare positional arg
        arg = initial_query
        kind = classify_url(arg) if arg else UrlKind.NOT_URL
        if kind == UrlKind.ONE_SHOT:
            download_links = [arg]
            print(f"{C_BLD}Direct download:{C_OFF} {arg}")
        elif kind == UrlKind.NYAA_RSS:
            extracted = extract_nyaa_query(arg)
            if not extracted:
                die("nyaa URL has no `q=` parameter to search with — use `anirss -S <url>` to subscribe")
            print(f"{C_DIM}extracted search:{C_OFF} {extracted}")
            arg = extracted
            kind = UrlKind.NOT_URL  # treat as a search now
        elif kind == UrlKind.OTHER_HTTP:
            die(f"bare URL only supported for nyaa.si — use `anirss -S {arg}` to subscribe")

        if download_links is None and kind == UrlKind.NOT_URL:
            query, selected, action = _run_search_state_machine(arg, cfg)
            if action == ACT_CANCEL:
                print(f"{C_DIM}cancelled.{C_OFF}")
                return
            default_name = arg or "anirss"
            if selected:
                default_name = show_name(selected[0].title) or default_name
            if action == ACT_SUB:
                feed_url = search_url(query, cfg["search"])
                print(f"{C_GRN}RSS URL:{C_OFF} {feed_url}")
            elif action == ACT_DL_ALL:
                download_links = [it.link for it in selected]
            elif action == ACT_DL_PICK:
                chosen = pick_downloads(selected)
                if not chosen:
                    print(f"{C_DIM}cancelled.{C_OFF}")
                    return
                download_links = [it.link for it in chosen]
                default_name = show_name(chosen[0].title) or default_name
            else:
                movie_choice = pick_movie(selected)
                if movie_choice is None:
                    print(f"{C_DIM}cancelled.{C_OFF}")
                    return

    name = "" if movie_choice is not None else get_name(parsed.name or default_name)
    log("INFO", f"name={name!r} feed={feed_url!r} "
                f"downloads={len(download_links) if download_links else 0} "
                f"movie={movie_choice.title if movie_choice else None!r}")

    qbt = _login_for(parsed, cfg)
    if feed_url:
        do_subscribe(qbt, feed_url, name, cfg["downloads"]["save_base"])
    if download_links:
        do_download(qbt, download_links, name, cfg["downloads"]["save_base"])
    if movie_choice:
        do_movie(qbt, movie_choice.title, movie_choice.link, cfg["downloads"]["movie_path"])

    maybe_refresh_feed_cache(qbt)
    log("INFO", "done")
    print(f"{C_GRN}done.{C_OFF}")


# ---- main entry ----

def main() -> None:
    cfg = load_config()
    fmt.set_show_leechers(bool(cfg["display"]["show_leechers"]))

    argv = sys.argv[1:]

    # --_search-rss is special: stay quiet, no readline, no log init
    if argv and argv[0] == "--_search-rss":
        _print_search_rss_rows(" ".join(argv[1:]).strip(), cfg)
        return

    if _handle_meta_flags(argv, cfg):
        return

    init_log(cfg["logging"]["log_path"])
    setup_readline()
    log("INFO", f"=== anirss invoked: argv={sys.argv[1:]} ===")

    parsed = parse_cli_args(argv)
    handled, force_url = _handle_op_flag(parsed.positional, cfg, parsed)
    if handled:
        return

    initial_query = " ".join(parsed.positional).strip()

    if parsed.non_interactive:
        _run_noninteractive(parsed, cfg, initial_query, force_url)
        return

    _run_interactive(initial_query, force_url, parsed, cfg)
```

- [ ] **Step 3: Run non-interactive helper tests**

Run: `uvx pytest tests/test_main_noninteractive.py -v` → PASS.

- [ ] **Step 4: Run the full test suite**

Run: `uvx pytest -q`
Expected: all green. If any test fails, fix before committing.

- [ ] **Step 5: Quick smoke test of the launcher**

Run: `./anirss --help`
Expected: prints the help text from `anirss_lib`'s `__doc__` (which is the top of `__init__.py`).

Run: `./anirss --version`
Expected: `anirss 0.3.0`.

Run: `./anirss --config`
Expected: prints config path + JSON config.

Run: `./anirss -Q`
Expected: either lists feeds (if qBittorrent is logged in via SID cache) or
prompts for password.

- [ ] **Step 6: Commit**

```bash
git add anirss_lib/main.py tests/test_main_noninteractive.py
git commit -m "Implement anirss_lib.main with non-interactive flags

New flags:
  --subscribe / --download-all / --download N / --movie
  --name NAME
  --password-stdin
  ANIRSS_QBT_PASSWORD env var"
```

---

## Task 12: docstring / help text

**Files:**
- Modify: `anirss_lib/__init__.py` (move the help docstring from `anirss.bak`)

- [ ] **Step 1: Update `anirss_lib/__init__.py`**

```python
"""anirss — search nyaa.si, then either subscribe (RSS rule) or download now in qBittorrent.

Usage:
    anirss                        Prompt for a search.
    anirss [search query]         Search, refine, choose subscribe/download/movie.
    anirss <nyaa-rss-url>         Extract `q=` from the URL and run as a fresh search.
    anirss <magnet|*.torrent>     Download a single torrent now.

    anirss -S <url>               Subscribe: skip search/refine, go to action menu.
    anirss -Sy                    Sync the feed cache from qBittorrent.

    anirss -Q                     List subscribed feeds (cached).
    anirss -Qj                    List subscribed feeds as JSON.

    anirss -R <name>...           Remove feed + rule.
    anirss -Rs <name>...          + remove torrents (keep files).
    anirss -Rn <name>...          + delete files (torrents stay; will error in qB).
    anirss -Rns <name>...         + everything (clean uninstall).
    anirss --noconfirm            Skip the y/N prompt for -R*.

Non-interactive (skip fzf, skip Name prompt):
    --subscribe                   Subscribe to query/URL without showing the action menu.
    --download-all                Download every matching item.
    --download N                  Download top N by download count.
    --movie                       Top-1 to movie_path.
    --name NAME                   Provide the subscription/download name.
    --password-stdin              Read qBittorrent password from stdin.

    Or set ANIRSS_QBT_PASSWORD in the environment.

Other:
    anirss --no-seed              Set qBittorrent to pause torrents at ratio 0.
    anirss --config               Print the resolved config and exit.
    anirss --migrate-config       Append any new default sections to your config.toml.
    anirss --version              Print version and exit.

Config: ~/.config/anirss/config.toml (auto-created on first run).
State:  ~/.local/state/anirss/   (feeds.txt cache, qbt.sid session cookie, anirss.log)
"""

__version__ = "0.3.0"
```

- [ ] **Step 2: Make main()'s --help use the package docstring**

In `anirss_lib/main.py`, change:
```python
if a in ("-h", "--help"):
    print(__doc__ or "anirss")
    return True
```
to:
```python
if a in ("-h", "--help"):
    import anirss_lib
    print(anirss_lib.__doc__ or "anirss")
    return True
```

- [ ] **Step 3: Verify**

Run: `./anirss --help | head -5`
Expected: shows the help text.

- [ ] **Step 4: Commit**

```bash
git add anirss_lib/__init__.py anirss_lib/main.py
git commit -m "Move help docstring to anirss_lib/__init__.py; document non-interactive flags"
```

---

## Task 13: Drop anirss.bak; final cleanup

**Files:**
- Delete: `anirss.bak`
- Modify: `test_anirss.py` (collapse the shim to direct imports per test)

- [ ] **Step 1: Replace the shim header**

Replace the top of `test_anirss.py` with:

```python
"""Tests for the pure functions in anirss_lib."""

from __future__ import annotations

from anirss_lib import titles, refine, types, config, format
from anirss_lib.cli import args as cli_args
from anirss_lib.cli.urls import UrlKind, classify_url, extract_nyaa_query
from anirss_lib.qbt import actions, feeds, session

# Aliases kept until each test body migrates.
import anirss_lib  # noqa: F401
poster_of = titles.poster_of
show_name = titles.show_name
title_tokens = titles.title_tokens
auto_resolution = refine.auto_resolution
compute_groups = refine.compute_groups
apply_pick = refine.apply_pick
add_exclude_to_query = refine.add_exclude_to_query
parse_op_flag = cli_args.parse_op_flag
_norm_path = actions._norm_path
_human_bytes = actions._human_bytes
write_feed_cache = feeds.write_feed_cache
read_feed_cache = feeds.read_feed_cache
feed_cache_age_seconds = feeds.feed_cache_age_seconds
is_feed_cache_stale = feeds.is_feed_cache_stale
_make_sid_cookie = session._make_sid_cookie
_effective_cookie_host = session._effective_cookie_host
Item = types.Item


def is_nyaa_url(s):
    return classify_url(s) == UrlKind.NYAA_RSS
```

Then go through each test function and replace `anirss.poster_of(…)` → `poster_of(…)`, `anirss._human_bytes(…)` → `_human_bytes(…)`, etc. The feed-cache tests' monkeypatch targets become `config.FEEDS_CACHE_PATH` / `config.STATE_DIR` (which is what they really patch through to the underlying feed-cache module).

- [ ] **Step 2: Run the suite**

Run: `uvx pytest -q`
Expected: all green.

- [ ] **Step 3: Delete the backup**

```bash
rm anirss.bak
```

- [ ] **Step 4: Commit**

```bash
git add test_anirss.py
git rm anirss.bak
git commit -m "Drop anirss.bak; collapse test shim to direct imports"
```

---

## Task 14: install.sh

**Files:**
- Modify: `install.sh`

- [ ] **Step 1: Update the install-or-refresh block**

Replace the "install or refresh the binary" block (lines 68-78 of `install.sh`) with a layout that installs the launcher + `anirss_lib/` into a shared dir, then symlinks the launcher onto PATH.

Find:
```bash
# --- install or refresh the binary ---
mkdir -p "$BIN_DIR"
if [ -L "$TARGET" ] && [ "$(readlink -f "$TARGET" 2>/dev/null)" = "$REPO_DIR/anirss" ]; then
    ok "symlink at $TARGET — picks up changes automatically"
elif [ -e "$TARGET" ]; then
    install -m 755 "$REPO_DIR/anirss" "$TARGET"
    ok "refreshed $TARGET"
else
    install -m 755 "$REPO_DIR/anirss" "$TARGET"
    ok "installed $TARGET"
fi
```

Replace with:
```bash
# --- install or refresh the binary + library ---
# Layout:
#   $LIB_DIR/anirss       launcher (executable)
#   $LIB_DIR/anirss_lib/  Python package
#   $BIN_DIR/anirss       symlink -> $LIB_DIR/anirss
LIB_DIR="${ANIRSS_LIB_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/anirss}"
mkdir -p "$BIN_DIR" "$LIB_DIR"

# If $TARGET is already a symlink into this repo, keep using the repo
# checkout — dev convenience, picks up edits without re-running install.sh.
if [ -L "$TARGET" ] && [ "$(readlink -f "$TARGET" 2>/dev/null)" = "$REPO_DIR/anirss" ]; then
    ok "symlink at $TARGET — picks up repo changes automatically"
else
    install -m 755 "$REPO_DIR/anirss" "$LIB_DIR/anirss"
    # Replace anirss_lib/ atomically: copy to .new then rename. Avoids a
    # partially-updated dir if the copy is interrupted.
    rm -rf "$LIB_DIR/anirss_lib.new"
    cp -R "$REPO_DIR/anirss_lib" "$LIB_DIR/anirss_lib.new"
    rm -rf "$LIB_DIR/anirss_lib"
    mv "$LIB_DIR/anirss_lib.new" "$LIB_DIR/anirss_lib"
    ok "installed launcher + library at $LIB_DIR"

    # (Re)symlink the launcher onto PATH.
    if [ -L "$TARGET" ] || [ ! -e "$TARGET" ]; then
        ln -sfn "$LIB_DIR/anirss" "$TARGET"
        ok "symlinked $TARGET -> $LIB_DIR/anirss"
    elif [ -f "$TARGET" ]; then
        # Replace the old single-file install with a symlink.
        rm -f "$TARGET"
        ln -sfn "$LIB_DIR/anirss" "$TARGET"
        ok "replaced old single-file $TARGET with a symlink to $LIB_DIR/anirss"
    fi
fi
```

- [ ] **Step 2: Verify the new flow**

Run: `bash install.sh`
Expected: prints `installed launcher + library at …` and `symlinked …`.
After: `~/.local/bin/anirss` is a symlink to `~/.local/share/anirss/anirss`,
and `~/.local/share/anirss/anirss_lib/` exists.

Run: `~/.local/bin/anirss --version`
Expected: `anirss 0.3.0`.

- [ ] **Step 3: Commit**

```bash
git add install.sh
git commit -m "install.sh: install launcher + anirss_lib side-by-side under $XDG_DATA_HOME"
```

---

## Task 15: Brew formula

**Files:**
- Modify: `packaging/brew/anirss.rb`

- [ ] **Step 1: Update the formula**

Find the `def install` block. Replace `bin.install "anirss"` with:

```ruby
def install
  libexec.install "anirss", "anirss_lib"
  bin.install_symlink libexec/"anirss"
  zsh_completion.install "completions/_anirss"
  bash_completion.install "completions/anirss.bash" => "anirss"
end
```

Brew's `libexec` is the standard place for "everything the binary needs but
doesn't belong on PATH". The symlink in `bin` is what users invoke.

- [ ] **Step 2: Update test block in formula (if any)**

If there's a `test do … end` block, ensure `assert_match "anirss",
shell_output("#{bin}/anirss --version")` still works — it should, since the
symlink resolves through `realpath` in our launcher.

- [ ] **Step 3: Commit**

```bash
git add packaging/brew/anirss.rb
git commit -m "brew: install anirss launcher + anirss_lib to libexec, symlink launcher to bin"
```

---

## Task 16: AUR PKGBUILDs

**Files:**
- Modify: `packaging/aur/anirss/PKGBUILD`, `packaging/aur/anirss-git/PKGBUILD`

- [ ] **Step 1: Update `packaging/aur/anirss/PKGBUILD`**

Find the `package()` function. Replace:
```bash
install -Dm755 anirss "$pkgdir/usr/bin/anirss"
```
with:
```bash
# Layout: /usr/lib/anirss/{anirss,anirss_lib}, /usr/bin/anirss -> launcher.
install -d "$pkgdir/usr/lib/anirss"
install -m 755 anirss "$pkgdir/usr/lib/anirss/anirss"
cp -R anirss_lib "$pkgdir/usr/lib/anirss/anirss_lib"
find "$pkgdir/usr/lib/anirss/anirss_lib" -type d -exec chmod 755 {} +
find "$pkgdir/usr/lib/anirss/anirss_lib" -type f -exec chmod 644 {} +
install -d "$pkgdir/usr/bin"
ln -s /usr/lib/anirss/anirss "$pkgdir/usr/bin/anirss"
```

Keep the other `install -Dm644 …` lines for README/LICENSE/completions
untouched.

- [ ] **Step 2: Repeat for `packaging/aur/anirss-git/PKGBUILD`**

Apply the same change.

- [ ] **Step 3: Commit**

```bash
git add packaging/aur/anirss/PKGBUILD packaging/aur/anirss-git/PKGBUILD
git commit -m "aur: install anirss launcher + anirss_lib to /usr/lib/anirss, symlink to /usr/bin"
```

---

## Task 17: README + final verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Non-interactive use" section**

Add the section near the existing "Usage" content:

```markdown
## Non-interactive use

For scripts, cron jobs, or anyone who already knows what they want, anirss
can run without ever opening fzf or prompting:

```sh
# Subscribe straight from a search query
ANIRSS_QBT_PASSWORD=... anirss --subscribe Frieren

# Download the top 5 results by download count
ANIRSS_QBT_PASSWORD=... anirss --download 5 "Frieren 1080p" --name Frieren

# Download every match
ANIRSS_QBT_PASSWORD=... anirss --download-all "Frieren 1080p"

# Treat the top match as a movie (saves to movie_path)
ANIRSS_QBT_PASSWORD=... anirss --movie "Some Movie 2024"

# Pipe the password instead of using the env var
echo "$pw" | anirss --password-stdin --subscribe Frieren

# Subscribe to a prebuilt nyaa RSS URL without ever opening the action menu
ANIRSS_QBT_PASSWORD=... anirss --subscribe -S "https://nyaa.si/?page=rss&q=Frieren"
```

The flags `--subscribe`, `--download-all`, `--download N`, and `--movie` are
mutually exclusive. Any one of them flips anirss into non-interactive mode:

* no fzf pickers
* no name prompt (use `--name`, or anirss derives one from `items[0].title`)
* no password retry loop — anirss either uses a cached SID, the
  `ANIRSS_QBT_PASSWORD` env var, or one line read from stdin when
  `--password-stdin` is set, and fails fast otherwise

A `--password PW` CLI flag is intentionally **not** provided; it would leak
the password to `ps aux` and shell history. Use the env var or
`--password-stdin`.
```

- [ ] **Step 2: Final test sweep**

Run: `uvx pytest -q`
Expected: all green.

Run: `./anirss --help` — confirm help shows the new flags.
Run: `./anirss --version` — confirm `0.3.0`.

- [ ] **Step 3: Final commit**

```bash
git add README.md
git commit -m "README: document non-interactive flags and bump to v0.3.0"
```

---

## Self-Review

- [x] **Spec coverage**: every spec section maps to at least one task.
  - §Layout → Tasks 1-13
  - §Terminal-size fix (B1) → Task 2
  - §SID-cookie tests (B2) → Task 8
  - §cmd_remove double-fetch (B3) → Task 9
  - §P1 sentinel → Task 11 (replaced with `force_url` return value)
  - §P2 fzf parsing → Task 6 (`_parse_fzf_output`)
  - §P3 main split → Task 11
  - §P4 URL classification → Task 10 (`cli/urls.py`)
  - §Non-interactive mode → Tasks 10, 11
  - §Packaging → Tasks 14-16
  - §Test plan → Tasks 2, 8, 10, 11

- [x] **Placeholder scan**: no TBDs / "implement later" / "similar to Task N" anywhere.

- [x] **Type consistency**: `ParsedArgs` referenced consistently; `UrlKind` enum names used in both implementation (Task 10) and consumer (Task 11); `FzfOutput` field names match between Task 6 and consumers in Task 10.

- [x] **Test isolation**: New `tests/` directory keeps new tests away from the legacy `test_anirss.py`. The legacy file's monkeypatches are explicitly redirected to `anirss_lib.config` so they actually take effect.
