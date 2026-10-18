"""qBittorrent-side actions: add torrents, manage subscriptions, path utilities."""

import base64
import json
import os
import re
import shutil
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from anirss_lib.ansi import C_BLD, C_CYN, C_DIM, C_OFF
from anirss_lib.config import CONFIG_PATH, STATE_DIR
from anirss_lib.errors import AnirssError, NotFoundError, QbtError, ValidationError
from anirss_lib.logging import log
from anirss_lib.qbt.session import QbtSession


# -------- path utilities --------

def validate_name(name: str) -> str:
    """A subscription/download name becomes a directory under save_base, so
    it must be one plain path component. Returns it stripped; raises
    ValidationError otherwise."""
    name = (name or "").strip()
    if not name:
        raise ValidationError("name cannot be empty")
    if "/" in name or "\\" in name:
        raise ValidationError("name must not contain '/' or '\\'")
    if name in (".", "..") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValidationError(f"invalid name {name!r}")
    return name


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
        raise ValidationError(f"refusing to rmtree suspicious path: {path!r}")
    try:
        shutil.rmtree(p)
    except OSError as e:
        raise AnirssError(f"rmtree {p}: {e}") from e


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


# -------- tags --------

def _clean_tags(tags: list[str] | None) -> list[str]:
    """Trim, drop blanks, de-duplicate (order-preserving). qBittorrent splits
    tags on commas itself, so a comma inside one would silently become two."""
    seen: list[str] = []
    for tag in tags or []:
        for piece in str(tag).split(","):
            piece = piece.strip()
            if piece and piece not in seen:
                seen.append(piece)
    return seen


def _tags_param(tags: list[str] | None) -> dict:
    """`torrents/add` takes tags as one comma-separated field; omit it entirely
    when nothing is configured so qBittorrent's own defaults still apply."""
    clean = _clean_tags(tags)
    return {"tags": ",".join(clean)} if clean else {}


def _tags_note(tags: list[str] | None) -> str:
    clean = _clean_tags(tags)
    return f" {C_DIM}[{', '.join(clean)}]{C_OFF}" if clean else ""


# -------- user-facing actions --------

def _unique_rule_name(qbt: QbtSession, name: str, feed_url: str,
                      endpoint_name: str) -> str:
    """Same show subscribed from a different endpoint gets ' @endpoint'
    suffixed so the existing rule and feed aren't silently overwritten.
    Re-subscribing the same feed keeps the name (idempotent overwrite)."""
    try:
        # This read is best-effort: on any qB or parse error keep the
        # plain name.
        rules = qbt.get_json("/api/v2/rss/rules")
        if not isinstance(rules, dict) or name not in rules:
            return name
        feeds = rules[name].get("affectedFeeds") or []
    except Exception:  # noqa: BLE001
        return name
    if feed_url in feeds or not endpoint_name:
        return name
    return f"{name} @{endpoint_name}"


@dataclass
class SubscribeResult:
    name: str            # the rule name actually used (may gain " @endpoint")
    feed_url: str
    save_path: str
    must_contain: str
    must_not_contain: str
    tags: list[str]


@dataclass
class AddResult:
    count: int
    save_path: str
    tags: list[str]
    # Info-hashes known up front (magnets, uploaded bytes). Remote .torrent
    # URLs are fetched by qBittorrent itself, so theirs aren't known here.
    hashes: list[str] = field(default_factory=list)


_BTIH_RE = re.compile(r"xt=urn:btih:([A-Za-z0-9]+)")


def magnet_hash(link: str) -> str | None:
    """Lower-hex info-hash of a magnet link (hex or base32 form), else None."""
    m = _BTIH_RE.search(link) if link.startswith("magnet:") else None
    if not m:
        return None
    raw = m.group(1)
    if len(raw) == 40:
        return raw.lower() if re.fullmatch(r"[0-9A-Fa-f]{40}", raw) else None
    if len(raw) == 32:
        try:
            return base64.b32decode(raw.upper()).hex()
        except ValueError:
            return None
    return None


def subscribe_rule(qbt: QbtSession, feed_url: str, name: str, save_base: str,
                   endpoint_name: str = "", must_not_contain: str = "",
                   must_contain: str = "",
                   tags: list[str] | None = None) -> SubscribeResult:
    """Register `feed_url` as a qB RSS feed plus an auto-download rule that
    saves under save_base/<name>. Silent; raises QbtError."""
    name = _unique_rule_name(qbt, name, feed_url, endpoint_name)
    save_path = os.path.join(save_base, name)
    qbt.post("/api/v2/rss/addFeed", url=feed_url, path=name)
    rule = {
        "enabled": True,
        "mustContain": must_contain,
        "mustNotContain": must_not_contain,
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
    rule_tags = _clean_tags(tags)
    if rule_tags:
        # Rules only learned about tags in qBittorrent 4.6, via `torrentParams`,
        # which supersedes the flat keys above wholesale when present, so the
        # save path has to be restated inside it along with the manual-TMM flag
        # that the flat `savePath` used to imply. Older builds ignore the block
        # and fall back to the flat keys: untagged, but otherwise correct.
        rule["torrentParams"] = {
            "save_path": save_path,
            "use_auto_tmm": False,
            "tags": rule_tags,
        }
    qbt.post("/api/v2/rss/setRule", ruleName=name, ruleDef=json.dumps(rule))
    return SubscribeResult(name, feed_url, save_path, must_contain,
                           must_not_contain, rule_tags)


def add_links(qbt: QbtSession, links: list[str], save_path: str,
              tags: list[str] | None = None,
              torrents: dict[str, bytes] | None = None) -> AddResult:
    """Add magnet / http(s) .torrent links, saving into `save_path`.

    `torrents` maps a link to its already-downloaded .torrent bytes; those
    are uploaded instead of sent as URLs. One qB already has is skipped,
    since qB answers a duplicate upload with HTTP 409."""
    from anirss_lib.stats import info_hash
    torrents = torrents or {}
    file_hashes = {link: info_hash(data).hex() for link, data in torrents.items()}
    have: set[str] = set()
    if file_hashes:
        have = {t.get("hash") for t in qbt.get_json(
            "/api/v2/torrents/info", hashes="|".join(file_hashes.values()))}
    urls = []
    for link in links:
        h = file_hashes.get(link)
        if h is None:
            urls.append(link)
        elif h not in have:
            add_torrent_bytes(qbt, torrents[link], _link_file_name(link),
                              save_path, tags)
            have.add(h)
    if urls:
        body = qbt.post(
            "/api/v2/torrents/add",
            urls="\n".join(urls),
            savepath=save_path,
            **_tags_param(tags),
        )
        err = _torrents_add_error(body)
        if err:
            raise QbtError(err)
    hashes = [h for h in (file_hashes.get(link) or magnet_hash(link)
                          for link in links) if h]
    return AddResult(len(links), save_path, _clean_tags(tags), hashes)


def _link_file_name(link: str) -> str:
    """The upload's file name: the last path segment of the link."""
    path = urllib.parse.urlsplit(link).path
    return urllib.parse.unquote(path.rsplit("/", 1)[-1]) or "file.torrent"


def add_torrent_bytes(qbt: QbtSession, data: bytes, file_name: str,
                      save_path: str, tags: list[str] | None = None) -> AddResult:
    """Upload .torrent bytes (multipart `torrents=`). qBittorrent's `urls=`
    only handles http/magnet, and a path on this host means nothing to a
    remote qBittorrent, so the bytes travel instead."""
    if not data:
        raise ValidationError(f"{file_name} is empty")
    body = qbt.post_multipart(
        "/api/v2/torrents/add",
        file_field="torrents",
        file_name=file_name or "file.torrent",
        file_bytes=data,
        savepath=save_path,
        **_tags_param(tags),
    )
    err = _torrents_add_error(body)
    if err:
        raise QbtError(err)
    from anirss_lib.stats import info_hash
    try:
        hashes = [info_hash(data).hex()]
    except (ValueError, IndexError, TypeError):
        hashes = []
    return AddResult(1, save_path, _clean_tags(tags), hashes)


def read_torrent_file(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ValidationError(f"could not read {path}: {e}") from e
    if not data:
        raise ValidationError(f"{path} is empty")
    return data


# -------- CLI wrappers: the cores above plus the `==>` progress lines --------

def do_subscribe(qbt: QbtSession, feed_url: str, name: str, save_base: str,
                 endpoint_name: str = "", must_not_contain: str = "",
                 must_contain: str = "",
                 tags: list[str] | None = None) -> str:
    res = subscribe_rule(qbt, feed_url, name, save_base,
                         endpoint_name=endpoint_name,
                         must_not_contain=must_not_contain,
                         must_contain=must_contain, tags=tags)
    print(f"{C_CYN}==>{C_OFF} added feed {C_BLD}{res.name}{C_OFF}")
    print(f"{C_CYN}==>{C_OFF} added rule  {C_BLD}{res.name}{C_OFF} -> {res.save_path}"
          f"{_tags_note(tags)}")
    return res.name


def do_download(qbt: QbtSession, links: list[str], name: str, save_base: str,
                tags: list[str] | None = None) -> None:
    save_path = os.path.join(save_base, name)
    print(f"{C_CYN}==>{C_OFF} adding {len(links)} torrent(s) -> {save_path}"
          f"{_tags_note(tags)}")
    add_links(qbt, links, save_path, tags)


def do_upload_local_torrent(qbt: QbtSession, path: str, name: str,
                            save_base: str, tags: list[str] | None = None) -> None:
    """Upload a local .torrent file's bytes to qBittorrent."""
    save_path = os.path.join(save_base, name)
    data = read_torrent_file(path)
    file_name = os.path.basename(path) or "file.torrent"
    print(f"{C_CYN}==>{C_OFF} uploading {C_BLD}{file_name}{C_OFF} "
          f"({_human_bytes(len(data))}) -> {save_path}{_tags_note(tags)}")
    add_torrent_bytes(qbt, data, file_name, save_path, tags)


def do_movie(qbt: QbtSession, title: str, link: str, movie_path: str,
             tags: list[str] | None = None) -> None:
    print(f"{C_CYN}==>{C_OFF} downloading movie -> {movie_path}{_tags_note(tags)}")
    print(f"    {C_DIM}{title}{C_OFF}")
    add_links(qbt, [link], movie_path, tags)


# -------- listing / removal cores (used by -Q / -R and the API) --------

@dataclass
class RuleInfo:
    name: str
    feed_url: str
    save_path: str
    rule_enabled: bool
    torrent_count: int
    file_count: int


def list_subscriptions(qbt: QbtSession) -> list[RuleInfo]:
    """Every qB RSS rule, sorted case-insensitively, with how many torrents
    qB holds under its save path and how many files sit there on disk (0
    when qBittorrent runs on another host and the path isn't visible)."""
    rules = qbt.get_json("/api/v2/rss/rules") or {}
    if not isinstance(rules, dict):
        rules = {}
    info = qbt.get_json("/api/v2/torrents/info") if rules else []
    # Pre-count torrents per save_path so each lookup is O(1).
    counts: dict[str, int] = {}
    if isinstance(info, list):
        for t in info:
            if isinstance(t, dict):
                sp = _norm_path(t.get("save_path", ""))
                counts[sp] = counts.get(sp, 0) + 1
    out: list[RuleInfo] = []
    for name, rule in sorted(rules.items(), key=lambda kv: kv[0].lower()):
        feeds = rule.get("affectedFeeds") or []
        save_path = rule.get("savePath") or ""
        out.append(RuleInfo(
            name=name,
            feed_url=feeds[0] if feeds else "",
            save_path=save_path,
            rule_enabled=bool(rule.get("enabled", True)),
            torrent_count=counts.get(_norm_path(save_path), 0),
            file_count=_dir_file_count(save_path),
        ))
    return out


@dataclass
class RemovePlan:
    name: str
    feed_url: str
    feed_path: str | None
    save_path: str
    torrents: list[dict]
    size_bytes: int
    torrent_count_in_qb: int


def plan_remove(qbt: QbtSession, names: list[str], *, drop_torrents: bool,
                drop_files: bool, save_base: str,
                local_files: bool = True) -> list[RemovePlan]:
    """Validate every target up front (so a typo can't half-destroy) and
    gather what removal would touch. Raises NotFoundError on unknown names.

    Without `local_files` the client's files are on another machine: they
    are never read here, so the size is the torrents' as qBittorrent says,
    and deleting them without their torrents is refused, since only
    qBittorrent can reach them."""
    if drop_files and not drop_torrents and not local_files:
        raise ValidationError("this client's files are not on this machine: anirss "
                              "can only delete them through qBittorrent, together "
                              "with their torrents")
    rules = qbt.get_json("/api/v2/rss/rules") or {}
    if not isinstance(rules, dict):
        rules = {}
    plans: list[RemovePlan] = []
    missing: list[str] = []
    for name in names:
        rule = rules.get(name)
        if not isinstance(rule, dict):
            missing.append(name)
            continue
        feeds = rule.get("affectedFeeds") or []
        feed_url = feeds[0] if feeds else ""
        save_path = _norm_path(rule.get("savePath") or os.path.join(save_base, name))
        feed_path = _find_feed_path(qbt, feed_url) if feed_url else None
        torrents = (_torrents_for_savepath(qbt, save_path)
                    if (drop_torrents or drop_files) else [])
        if not drop_files:
            size_bytes = 0
        elif local_files:
            size_bytes = _dir_size_bytes(save_path) if os.path.isdir(save_path) else 0
        else:
            size_bytes = sum(t["size"] for t in torrents
                             if isinstance(t.get("size"), int))
        plans.append(RemovePlan(name, feed_url, feed_path, save_path, torrents,
                                size_bytes, len(torrents)))
    if missing:
        raise NotFoundError(f"unknown feed(s): {', '.join(missing)} "
                            f"(run `anirss -Q` to list)")
    return plans


def _is_inside(path: str, roots: list[str], *, resolve: bool = True) -> bool:
    # Another machine's paths cannot be resolved here: compared as written
    norm = os.path.realpath if resolve else os.path.normpath
    real = norm(path)
    for root in roots:
        root_real = norm(_norm_path(root))
        if real != root_real and real.startswith(root_real.rstrip(os.sep) + os.sep):
            return True
    return False


def check_confined(plans: list[RemovePlan], roots: list[str], *,
                   local_files: bool = True) -> None:
    """Raise ValidationError if any plan's files lie outside `roots`."""
    outside = [p.save_path for p in plans
               if not _is_inside(p.save_path, roots, resolve=local_files)]
    if outside:
        raise ValidationError("refusing to delete files outside the download "
                              f"roots: {', '.join(outside)}")


def execute_remove(qbt: QbtSession, plans: list[RemovePlan], *,
                   drop_torrents: bool, drop_files: bool,
                   confine_to: list[str] | None = None,
                   local_files: bool = True,
                   notify=None) -> None:
    """Carry out plan_remove()'s plans: torrents, then feed, then rule, then
    files. `confine_to` (the API passes the configured download roots)
    refuses to delete files anywhere but strictly inside one of them.
    `notify(msg)` receives one line per completed step."""
    notify = notify or (lambda _msg: None)
    if drop_files and confine_to is not None:
        check_confined(plans, confine_to, local_files=local_files)
    for p in plans:
        if drop_torrents and p.torrents:
            hashes = "|".join(t["hash"] for t in p.torrents
                              if isinstance(t.get("hash"), str))
            qbt.post("/api/v2/torrents/delete", hashes=hashes,
                     deleteFiles=("true" if drop_files else "false"))
            notify(f"removed {len(p.torrents)} torrent(s) for {p.name}")
        if p.feed_path:
            qbt.post("/api/v2/rss/removeItem", path=p.feed_path)
            notify(f"removed feed {p.name}")
        elif p.feed_url:
            log("WARN", f"no qB feed-path matched url={p.feed_url!r} for rule {p.name!r}")
        qbt.post("/api/v2/rss/removeRule", ruleName=p.name)
        notify(f"removed rule {p.name}")
        # Files: delete the directory unless qB already cleaned it (drop_torrents+drop_files).
        # Never on another machine's client: the same path here is not its.
        if drop_files and not drop_torrents and local_files and os.path.isdir(p.save_path):
            _safe_rmtree(p.save_path)
            notify(f"deleted files at {p.save_path}")


TORRENT_FIELDS = ("hash", "name", "state", "progress", "size", "dlspeed",
                  "upspeed", "eta", "num_seeds", "num_leechs", "ratio",
                  "save_path", "content_path", "added_on", "completion_on",
                  "tags", "category")


def list_torrents(qbt: QbtSession, *, save_path: str | None = None,
                  hashes: list[str] | None = None) -> list[dict]:
    """qB's torrent list trimmed to TORRENT_FIELDS, optionally narrowed to
    one save path (a subscription's folder) or a set of info-hashes. Paths
    are as qBittorrent sees them (its host, not necessarily this one)."""
    params = {"hashes": "|".join(hashes)} if hashes else {}
    info = qbt.get_json("/api/v2/torrents/info", **params)
    if not isinstance(info, list):
        return []
    norm = _norm_path(save_path) if save_path else None
    return [{k: t.get(k) for k in TORRENT_FIELDS} for t in info
            if isinstance(t, dict)
            and (norm is None or _norm_path(t.get("save_path", "")) == norm)]
