"""The downloaded shows on this machine, for people who watch in their own
player: which episodes a show folder holds, in episode order, which one is
next, and how to open it in mpv, VLC or whatever the system opens videos
with. Plus what qBittorrent is downloading now and finished lately.

Silent core logic; the CLI (`anirss --play`, `--status`) prints."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from anirss_lib.compat import IS_WINDOWS, detached_flags
from anirss_lib.config import STATE_DIR
from anirss_lib.errors import NotFoundError, ValidationError

PLAYED_PATH = STATE_DIR / "played.json"

VIDEO_EXT = frozenset({".mkv", ".mp4", ".m4v", ".avi", ".webm", ".mov", ".ts",
                       ".m2ts", ".wmv", ".flv", ".ogm"})
# What a file still being written looks like, depending on the client.
INCOMPLETE_EXT = (".!qb", ".part", ".crdownload", ".tmp")

# Players anirss knows, in the order it looks for them.
PLAYERS = ("mpv", "vlc", "celluloid", "haruna", "smplayer", "mplayer")

_SEASON_EP = re.compile(r"\b[Ss](\d{1,2})\s?[Ee](\d{1,4}(?:\.\d)?)(?:v\d+)?\b")
_DASH_EP = re.compile(r"\s-\s(\d{1,4}(?:\.\d)?)(?:v\d+)?(?=[\s\[\(._]|$)")
_WORD_EP = re.compile(r"\b(?:ep|episode|e)\s?(\d{1,4})(?:v\d+)?\b", re.IGNORECASE)
_BRACKET_EP = re.compile(r"[\[\(_\s](\d{2,3})(?:v\d+)?[\]\)_\s]")
_SEASON = re.compile(r"\b(?:s|season\s?)(\d{1,2})\b", re.IGNORECASE)
_RESOLUTION = re.compile(r"\b(?:480|540|576|720|1080|1440|2160|4320)p?\b")


@dataclass
class Episode:
    path: str
    name: str            # the file name
    season: int | None
    number: float | None

    @property
    def label(self) -> str:
        if self.number is None:
            return self.name
        n = int(self.number) if self.number == int(self.number) else self.number
        return f"S{self.season:02d}E{n:02}" if self.season else f"{n:02}"


@dataclass
class Show:
    name: str            # the folder name, which is the subscription's name
    path: str
    episodes: list[Episode] = field(default_factory=list)
    newest: float = 0.0  # mtime of its newest episode


def parse_episode(filename: str) -> tuple[int | None, float | None]:
    """(season, episode) read from a release's file name, as well as one
    can: `S02E05`, `Show - 05`, `Ep 5`, `[05]`. Resolutions never count."""
    stem = os.path.splitext(filename)[0]
    m = _SEASON_EP.search(stem)
    if m:
        return int(m.group(1)), float(m.group(2))
    # Drop bracketed metadata ([1080p], [ABCD1234]) before the looser forms.
    plain = _RESOLUTION.sub(" ", stem)
    season = None
    s = _SEASON.search(plain)
    if s:
        season = int(s.group(1))
    for pattern in (_DASH_EP, _WORD_EP, _BRACKET_EP):
        m = pattern.search(f" {plain} ")
        if m:
            return season, float(m.group(1))
    return season, None


def _sort_key(ep: Episode) -> tuple:
    return (ep.season or 0, ep.number if ep.number is not None else float("inf"),
            ep.name.lower())


def episodes_in(folder: str, *, skip: set[str] | frozenset = frozenset()) -> list[Episode]:
    """Every video file under `folder` (batch subfolders included), in
    episode order. Files still downloading, and paths in `skip` (what
    qBittorrent says is incomplete), are left out."""
    out: list[Episode] = []
    skip_real = {os.path.realpath(p) for p in skip}
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if any(root == s or root.startswith(s.rstrip(os.sep) + os.sep) for s in skip_real):
            continue
        for f in files:
            low = f.lower()
            if low.endswith(INCOMPLETE_EXT) or os.path.splitext(low)[1] not in VIDEO_EXT:
                continue
            if "sample" in low and os.path.getsize(os.path.join(root, f)) < 100 << 20:
                continue
            path = os.path.join(root, f)
            if os.path.realpath(path) in skip_real:
                continue
            season, number = parse_episode(f)
            out.append(Episode(path=path, name=f, season=season, number=number))
    out.sort(key=_sort_key)
    return out


def shows(save_base: str, *, skip: set[str] | frozenset = frozenset()) -> list[Show]:
    """The show folders under `save_base` that hold at least one episode,
    the most recently updated first."""
    try:
        entries = sorted(os.scandir(save_base), key=lambda e: e.name.lower())
    except OSError:
        return []
    out: list[Show] = []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        eps = episodes_in(entry.path, skip=skip)
        if eps:
            newest = max(os.path.getmtime(e.path) for e in eps)
            out.append(Show(name=entry.name, path=entry.path, episodes=eps, newest=newest))
    out.sort(key=lambda s: s.newest, reverse=True)
    return out


def find_show(all_shows: list[Show], query: str) -> Show:
    """A show by name: exact (any case), else the one name that starts
    with or contains `query`."""
    q = query.strip().casefold()
    for test in (lambda n: n == q, lambda n: n.startswith(q), lambda n: q in n):
        hits = [s for s in all_shows if test(s.name.casefold())]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(s.name for s in hits[:6])
            raise ValidationError(f"{query!r} matches {len(hits)} shows: {names}")
    raise NotFoundError(f"no downloaded show matches {query!r}")


# ---- what was played ---------------------------------------------------------

def _load_played(path: Path | None = None) -> dict:
    path = PLAYED_PATH if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def last_played(show: Show, path: Path | None = None) -> str | None:
    """The file (relative to the show folder) anirss last played for it."""
    entry = _load_played(path).get(os.path.realpath(show.path))
    return entry.get("file") if isinstance(entry, dict) else None


def next_episode(show: Show, path: Path | None = None) -> Episode | None:
    """The episode after the last one played, or the first. None when the
    last one played is the newest there is (caught up)."""
    last = last_played(show, path)
    rels = [os.path.relpath(e.path, show.path) for e in show.episodes]
    if last is None or last not in rels:
        return show.episodes[0] if show.episodes else None
    i = rels.index(last)
    return show.episodes[i + 1] if i + 1 < len(show.episodes) else None


def mark_played(show: Show, ep: Episode, path: Path | None = None) -> None:
    path = PLAYED_PATH if path is None else path
    data = _load_played(path)
    data[os.path.realpath(show.path)] = {"file": os.path.relpath(ep.path, show.path),
                                         "at": int(time.time())}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def incomplete_paths(torrents: list[dict], folder: str) -> set[str]:
    """Content paths of torrents under `folder` that qBittorrent has not
    finished, so a half-written file is never played."""
    root = os.path.realpath(folder)
    out = set()
    for t in torrents:
        content = t.get("content_path") or ""
        if not content or (t.get("progress") or 0) >= 1:
            continue
        real = os.path.realpath(content)
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            out.add(content)
    return out


# ---- the player --------------------------------------------------------------

def player_command(configured: str = "") -> tuple[list[str], str] | None:
    """(command, name) to open a video with: [player] command when set, else
    the first known player on PATH, else the system's opener. None on
    Windows with nothing configured, where os.startfile opens it."""
    if configured.strip():
        parts = shlex.split(configured, posix=not IS_WINDOWS)
        return parts, os.path.basename(parts[0])
    for name in PLAYERS:
        found = shutil.which(name)
        if found:
            return [found], name
    if sys.platform == "darwin":
        if os.path.isdir("/Applications/IINA.app"):
            return ["open", "-a", "IINA"], "IINA"
        return ["open"], "the default player"
    if IS_WINDOWS:
        return None
    opener = shutil.which("xdg-open")
    if opener:
        return [opener], "the default player"
    return None


def play(path: str, configured: str = "") -> str:
    """Opens `path` in the player, detached so the terminal is free again.
    Returns the player's name. Raises ValidationError when there is none."""
    found = player_command(configured)
    if found is None:
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
            return "the default player"
        raise ValidationError("no video player found: install mpv or VLC, or set "
                              "[player] command in config.toml")
    cmd, name = found
    if any("{file}" in part for part in cmd):
        argv = [part.replace("{file}", path) for part in cmd]
    else:
        argv = [*cmd, path]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **detached_flags())
    except OSError as e:
        raise ValidationError(f"couldn't start {name}: {e}") from None
    return name


# ---- what qBittorrent is doing ---------------------------------------------------

@dataclass
class Activity:
    downloading: list[dict]   # progress < 1, the furthest along first
    finished: list[dict]      # completed within `days`, the newest first


def activity(torrents: list[dict], roots: list[str], *, days: float = 7.0,
             now: float | None = None) -> Activity:
    """anirss's torrents (saved under one of its download roots), split into
    what is still coming and what finished lately. Paths compare as written,
    since they are qBittorrent's and may be another machine's."""
    now = time.time() if now is None else now
    norm = [os.path.normpath(r).rstrip(os.sep) for r in roots if r]

    def ours(t: dict) -> bool:
        sp = os.path.normpath(t.get("save_path") or "")
        return any(sp == r or sp.startswith(r + os.sep) for r in norm)
    mine = [t for t in torrents if ours(t)]
    down = sorted((t for t in mine if (t.get("progress") or 0) < 1),
                  key=lambda t: -(t.get("progress") or 0))
    cutoff = now - days * 86400
    done = sorted((t for t in mine if (t.get("progress") or 0) >= 1
                   and (t.get("completion_on") or 0) > cutoff),
                  key=lambda t: -(t.get("completion_on") or 0))
    return Activity(downloading=down, finished=done)


def show_of(t: dict, save_base: str) -> str:
    """The show folder a torrent saves into, or "" when it is not under
    `save_base` (a movie)."""
    sp = os.path.normpath(t.get("save_path") or "")
    base = os.path.normpath(save_base).rstrip(os.sep)
    if not sp.startswith(base + os.sep):
        return ""
    return sp[len(base) + 1:].split(os.sep, 1)[0]
