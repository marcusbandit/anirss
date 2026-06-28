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
    hidden_base: str


class SearchConfig(TypedDict):
    nyaa_url: str
    category: str
    filter: str


class BestfitConfig(TypedDict):
    preferred_groups: list[str]
    source_order: list[str]
    preferred_resolution: str


class LoggingConfig(TypedDict):
    log_path: str


class DisplayConfig(TypedDict):
    show_leechers: bool
    force_show_seeders: bool


class AnirssConfig(TypedDict):
    qbittorrent: QbtConfig
    downloads: DownloadsConfig
    search: SearchConfig
    bestfit: BestfitConfig
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
# Used when an op flag carries the `h` modifier (e.g. `-Sh`, `-Th`).
# Per-name subdirectory is created under this just like save_base.
hidden_base = "/srv/media/Hentai"

[search]
nyaa_url = "https://nyaa.si/"
# nyaa category id: "1_0" = Anime (all), "1_2" = English-translated, etc.
category = "1_0"
# nyaa filter: "0" = no filter, "1" = no remakes, "2" = trusted only
filter = "0"

[bestfit]
# "[★ Try Best Fit]" in the refine picker auto-pins the best quality profile
# (group + resolution + source) and refetches nyaa for every matching release.
# Release groups ranked most-to-least trusted; the first listed wins ties.
preferred_groups = ["Erai-raws", "SubsPlease", "ASW", "EMBER", "Judas"]
# Source types best-to-worst. WEB-DL always beats WebRip; reorder to taste.
source_order = ["WEB-DL", "WEB", "BluRay", "WEBRip", "HDTV"]
# "highest" picks the sharpest available (2160p > 1080p > 720p). Set to a
# number like "1080" to treat that as the sweet spot and avoid huge 4K files.
preferred_resolution = "highest"

[logging]
log_path = "~/.local/state/anirss/anirss.log"

[display]
# Show leechers count alongside seeders (e.g. "45s/5l"). Off by default.
show_leechers = false
# Below ~70 columns the seeders column is auto-hidden so the title still
# fits. Set this to `true` to always render seeders regardless of width.
force_show_seeders = false
"""

DEFAULT_CONFIG: AnirssConfig = cast(AnirssConfig, tomllib.loads(DEFAULT_CONFIG_TOML))


def _deep_merge(dst, src):
    """Recursively merge `src` into `dst` (type-erased; works on dicts and TypedDicts)."""
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


SECTION_HEADER_RE = re.compile(r"^\[([^\[\]]+)\]\s*$", re.MULTILINE)


def _split_toml_sections(text: str) -> list[tuple[str, str]]:
    """Split a TOML string into (section_name, full_block_including_header) pairs.
    A leading preamble (before the first [section] line) is returned as ('', body).
    """
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
    """Append default-config sections that aren't present in the user's
    config.toml. Existing keys, values, and comments are left untouched.
    """
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
