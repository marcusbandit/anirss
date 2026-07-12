"""qBittorrent-side actions: add torrents, manage subscriptions, path utilities."""

import json
import os
import shutil
from pathlib import Path

from anirss_lib.ansi import C_BLD, C_CYN, C_DIM, C_OFF
from anirss_lib.config import CONFIG_PATH, STATE_DIR
from anirss_lib.logging import die
from anirss_lib.qbt.session import QbtSession


# -------- path utilities --------

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
    """Recursive size of a directory (follows nothing, swallows OSError per-entry)."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _dir_file_count(path: str) -> int:
    """Recursive count of regular files under `path`. Returns 0 if missing."""
    if not os.path.isdir(path):
        return 0
    total = 0
    for _root, _dirs, files in os.walk(path, followlinks=False):
        total += len(files)
    return total


def _safe_rmtree(path: str) -> None:
    """rmtree with a paranoia guard against `~`, `/`, empty, and config/state dirs."""
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


# -------- torrent / feed lookups used by the remove flow --------

def _find_feed_path(qbt: QbtSession, rule_url: str) -> str | None:
    """Walk the RSS items tree and return the qB feed-path whose `url` matches `rule_url`."""
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


# -------- torrent add error parsing --------

def _torrents_add_error(body: str) -> str | None:
    """Return an error message if the add response indicates failure, else None.

    qBittorrent <5 returns the literal `Ok.` on success. v5+ returns JSON like
    `{"success_count":N,"pending_count":N,"failure_count":N,...}` where
    `pending_count` is async work that will land shortly — also success.
    """
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


# -------- user-facing actions --------

def _unique_rule_name(qbt: QbtSession, name: str, feed_url: str,
                      endpoint_name: str) -> str:
    """Same show subscribed from a different endpoint gets ' @endpoint'
    suffixed so the existing rule and feed aren't silently overwritten.
    Re-subscribing the same feed keeps the name (idempotent overwrite)."""
    try:
        # qbt.get() die()s (SystemExit) on HTTP errors; this read is
        # best-effort, so swallow that too and keep the plain name.
        rules = qbt.get_json("/api/v2/rss/rules")
        if not isinstance(rules, dict) or name not in rules:
            return name
        feeds = rules[name].get("affectedFeeds") or []
    except (Exception, SystemExit):
        return name
    if feed_url in feeds or not endpoint_name:
        return name
    return f"{name} @{endpoint_name}"


def do_subscribe(qbt: QbtSession, feed_url: str, name: str, save_base: str,
                 endpoint_name: str = "") -> str:
    name = _unique_rule_name(qbt, name, feed_url, endpoint_name)
    save_path = os.path.join(save_base, name)
    print(f"{C_CYN}==>{C_OFF} adding feed {C_BLD}{name}{C_OFF}")
    qbt.post("/api/v2/rss/addFeed", url=feed_url, path=name)
    rule = {
        "enabled": True,
        "mustContain": "",
        "mustNotContain": "",
        "useRegex": False,
        "episodeFilter": "",
        "smartFilter": False,
        "previouslyMatchedEpisodes": [],
        "affectedFeeds": [feed_url],
        "ignoreDays": 0,
        "lastMatch": "",
        "addPaused": None,
        "assignedCategory": "",
        "savePath": save_path,
    }
    print(f"{C_CYN}==>{C_OFF} adding rule  {C_BLD}{name}{C_OFF} -> {save_path}")
    qbt.post("/api/v2/rss/setRule", ruleName=name, ruleDef=json.dumps(rule))
    return name


def do_download(qbt: QbtSession, links: list[str], name: str, save_base: str) -> None:
    save_path = os.path.join(save_base, name)
    print(f"{C_CYN}==>{C_OFF} adding {len(links)} torrent(s) -> {save_path}")
    body = qbt.post(
        "/api/v2/torrents/add",
        urls="\n".join(links),
        savepath=save_path,
    )
    err = _torrents_add_error(body)
    if err:
        die(err)


def do_upload_local_torrent(qbt: QbtSession, path: str, name: str,
                            save_base: str) -> None:
    """Upload a local .torrent file's bytes to qBittorrent.

    qBittorrent's `urls=` field only handles http/magnet, so we read the
    file here and POST multipart with `torrents=<bytes>`. Works even when
    qBittorrent is on another host.
    """
    save_path = os.path.join(save_base, name)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        die(f"could not read {path}: {e}")
    if not data:
        die(f"{path} is empty")
    file_name = os.path.basename(path) or "file.torrent"
    print(f"{C_CYN}==>{C_OFF} uploading {C_BLD}{file_name}{C_OFF} "
          f"({_human_bytes(len(data))}) -> {save_path}")
    body = qbt.post_multipart(
        "/api/v2/torrents/add",
        file_field="torrents",
        file_name=file_name,
        file_bytes=data,
        savepath=save_path,
    )
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
