"""qBittorrent-side actions: add torrents, manage subscriptions, path utilities."""

import base64
import json
import ntpath
import os
import posixpath
import re
import shutil
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from anirss_lib.ansi import C_BLD, C_CYN, C_DIM, C_OFF, C_YEL
from anirss_lib.config import CONFIG_PATH, STATE_DIR
from anirss_lib.errors import AnirssError, NotFoundError, QbtError, ValidationError
from anirss_lib.logging import log
from anirss_lib.qbt.session import QbtSession


# -------- path utilities --------

def join_save_path(base: str, name: str) -> str:
    """`base`/`name` the way the machine qBittorrent saves on writes paths: a
    POSIX base (/srv/anime) joins with "/" even when anirss runs on Windows,
    a Windows one (D:\\Anime) with "\\" even on Linux."""
    if base.startswith("/") and "\\" not in base:
        return posixpath.join(base, name)
    if ntpath.splitdrive(base)[0] or "\\" in base:
        return ntpath.join(base, name)
    return os.path.join(base, name)


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
    norm = os.path.normpath(os.path.expanduser(p))
    # On Windows both separators end a path (C:\\anime\\ and C:/anime/).
    return norm.rstrip("/\\" if os.name == "nt" else "/")


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
    if p in forbidden or len(p) <= 1 or os.path.splitdrive(p)[1] == "":
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


RULE_TAG_PREFIX = "anirss:"


def rule_tag(name: str) -> str:
    """The tag every torrent a rule adds carries, so the ones it added can be told
    apart in a folder other torrents share: a show's own folder (#38). A comma
    would split it in two, so none is kept."""
    return RULE_TAG_PREFIX + name.replace(",", " ").strip()


def rule_save_path(rule: dict) -> str:
    """Where a rule saves. qBittorrent 4.6 and later read `torrentParams`
    first, and may list the folder only there."""
    params = rule.get("torrentParams")
    inner = params.get("save_path") if isinstance(params, dict) else ""
    return (inner if isinstance(inner, str) else "") or rule.get("savePath") or ""


def owns_folder(save_path: str, name: str, bases: list[str]) -> bool:
    """Whether `save_path` is the folder anirss makes for the rule `name`,
    `<base>/<name>` under one of the download roots, and so holds only what
    the rule and downloads under its name put there. Any other folder, a
    show's own say, is shared."""
    norm = _norm_path(save_path)
    return any(norm == _norm_path(join_save_path(b, name)) for b in bases if b)


def _torrent_tags(t: dict) -> set[str]:
    raw = t.get("tags") or ""
    pieces = raw if isinstance(raw, list) else str(raw).split(",")
    return {str(p).strip() for p in pieces if str(p).strip()}


def _rule_torrents(info, save_path: str, name: str, owned: bool) -> list[dict]:
    """The torrents in qB's list `info` that are the rule's: every one under
    its own folder, or in a shared folder only those carrying its tag."""
    if not isinstance(info, list):
        return []
    norm = _norm_path(save_path)
    tag = rule_tag(name)
    return [t for t in info if isinstance(t, dict)
            and _norm_path(t.get("save_path", "")) == norm
            and (owned or tag in _torrent_tags(t))]


def _torrents_for_savepath(qbt: QbtSession, save_path: str, name: str,
                           owned: bool) -> list[dict]:
    return _rule_torrents(qbt.get_json("/api/v2/torrents/info"), save_path, name, owned)


def _inside(path: str, folder: str) -> bool:
    """Whether `path` lies strictly inside `folder`, links resolved."""
    real, root = os.path.realpath(path), os.path.realpath(folder)
    return real != root and real.startswith(root.rstrip(os.sep) + os.sep)


def _content_file_count(torrents: list[dict]) -> int:
    """Files on disk under the torrents' content paths (0 for paths not here)."""
    total = 0
    for t in torrents:
        path = t.get("content_path") or ""
        if path and os.path.isfile(path):
            total += 1
        elif path:
            total += _dir_file_count(path)
    return total


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
    # qBittorrent preferences this subscription turned on (RSS_PREFS keys);
    # empty when they were on already.
    turned_on: list[str] = field(default_factory=list)
    # RSS_PREFS keys still off afterwards (subscribed with enable_rss=False):
    # the rule exists but downloads nothing until they are on.
    rss_off: list[str] = field(default_factory=list)


# qBittorrent ships with both off, and a subscription does nothing without
# them: the feed is never fetched, or its matches are never downloaded.
RSS_PREFS = {
    "rss_processing_enabled": "RSS feed fetching",
    "rss_auto_downloading_enabled": "RSS auto-download",
}


def rss_prefs_off(qbt: QbtSession) -> list[str] | None:
    """The RSS_PREFS keys qBittorrent reports as off: empty when they are
    on, None when it could not be read (an old build, an odd proxy)."""
    try:
        prefs = qbt.get_json("/api/v2/app/preferences")
    except QbtError as e:
        log("WARN", f"can't read qBittorrent preferences: {e}")
        return None
    if not isinstance(prefs, dict) or not any(k in prefs for k in RSS_PREFS):
        return None
    return [k for k in RSS_PREFS if prefs.get(k) is False]


def ensure_rss_on(qbt: QbtSession) -> list[str]:
    """Turns on whichever of RSS_PREFS is off. Returns the keys it turned on.
    These are qBittorrent-wide: RSS rules of the user's own act on them too."""
    off = rss_prefs_off(qbt) or []
    if off:
        qbt.post("/api/v2/app/setPreferences", json=json.dumps({k: True for k in off}))
        log("INFO", f"turned on qBittorrent preferences: {off}")
    return off


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
                   tags: list[str] | None = None, *,
                   enable_rss: bool = True,
                   save_path: str | None = None) -> SubscribeResult:
    """Register `feed_url` as a qB RSS feed plus an auto-download rule that
    saves under save_base/<name>, or into `save_path` when given: a show's own
    folder, so the rule never saves anywhere else first. With `enable_rss`, turns on qBittorrent's
    RSS fetching and auto-download first when they are off (a subscription
    does nothing without them); without it, reports them in `rss_off`.
    Silent; raises QbtError."""
    turned_on = ensure_rss_on(qbt) if enable_rss else []
    rss_off = [] if enable_rss else (rss_prefs_off(qbt) or [])
    name = _unique_rule_name(qbt, name, feed_url, endpoint_name)
    if save_path is None:
        save_path = join_save_path(save_base, name)
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
    # Rules only learned about tags in qBittorrent 4.6, via `torrentParams`,
    # which supersedes the flat keys above wholesale when present, so the
    # save path has to be restated inside it along with the manual-TMM flag
    # that the flat `savePath` used to imply. Older builds ignore the block
    # and fall back to the flat keys: untagged, but otherwise correct. The
    # rule's own tag rides along, so its torrents can be told apart in a
    # folder it shares with others (#38).
    rule["torrentParams"] = {
        "save_path": save_path,
        "use_auto_tmm": False,
        "tags": _clean_tags([*rule_tags, rule_tag(name)]),
    }
    qbt.post("/api/v2/rss/setRule", ruleName=name, ruleDef=json.dumps(rule))
    return SubscribeResult(name, feed_url, save_path, must_contain,
                           must_not_contain, rule_tags, turned_on, rss_off)


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

def ask_rss_on(qbt: QbtSession) -> bool:
    """Interactive: when qBittorrent's RSS fetching or auto-download is off,
    say what that means and ask before turning it on. True to turn on."""
    off = rss_prefs_off(qbt)
    if not off:
        return True
    what = " and ".join(RSS_PREFS[k] for k in off)
    print(f"{C_YEL}qBittorrent has {what} off{C_OFF}, so a subscription never "
          f"downloads anything.")
    print(f"{C_DIM}Turning {'it' if len(off) == 1 else 'them'} on is for all of "
          f"qBittorrent: RSS rules of your own act on it too.{C_OFF}")
    try:
        reply = input("Turn on? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise AnirssError("cancelled") from None
    return reply not in ("n", "no")


def do_subscribe(qbt: QbtSession, feed_url: str, name: str, save_base: str,
                 endpoint_name: str = "", must_not_contain: str = "",
                 must_contain: str = "",
                 tags: list[str] | None = None, *, enable_rss: bool = True) -> str:
    res = subscribe_rule(qbt, feed_url, name, save_base,
                         endpoint_name=endpoint_name,
                         must_not_contain=must_not_contain,
                         must_contain=must_contain, tags=tags,
                         enable_rss=enable_rss)
    if res.turned_on:
        what = " and ".join(RSS_PREFS[k] for k in res.turned_on)
        print(f"{C_YEL}==>{C_OFF} turned on {what} in qBittorrent: "
              f"{'it was' if len(res.turned_on) == 1 else 'they were'} off, so no "
              f"subscription would ever download anything")
    if res.rss_off:
        print(f"{C_YEL}==>{C_OFF} RSS is off in qBittorrent, so nothing downloads yet. "
              f"Turn it on in Tools > Options > RSS when you want this to run.")
    print(f"{C_CYN}==>{C_OFF} added feed {C_BLD}{res.name}{C_OFF}")
    print(f"{C_CYN}==>{C_OFF} added rule  {C_BLD}{res.name}{C_OFF} -> {res.save_path}"
          f"{_tags_note(tags)}")
    return res.name


def do_download(qbt: QbtSession, links: list[str], name: str, save_base: str,
                tags: list[str] | None = None) -> None:
    save_path = join_save_path(save_base, name)
    print(f"{C_CYN}==>{C_OFF} adding {len(links)} torrent(s) -> {save_path}"
          f"{_tags_note(tags)}")
    add_links(qbt, links, save_path, tags)


def do_upload_local_torrent(qbt: QbtSession, path: str, name: str,
                            save_base: str, tags: list[str] | None = None) -> None:
    """Upload a local .torrent file's bytes to qBittorrent."""
    save_path = join_save_path(save_base, name)
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


def list_subscriptions(qbt: QbtSession, own_bases: list[str] | None = None
                       ) -> list[RuleInfo]:
    """Every qB RSS rule, sorted case-insensitively, with how many torrents
    are its and how many files sit on disk (0 when qBittorrent runs on
    another host and the path isn't visible). In its own `<base>/<name>`
    folder under one of `own_bases` that is the whole folder; in any other,
    a show's folder it shares, only the torrents carrying its tag and their
    files (#38). Without `own_bases`, every folder counts as its own."""
    rules = qbt.get_json("/api/v2/rss/rules") or {}
    if not isinstance(rules, dict):
        rules = {}
    info = qbt.get_json("/api/v2/torrents/info") if rules else []
    out: list[RuleInfo] = []
    for name, rule in sorted(rules.items(), key=lambda kv: kv[0].lower()):
        if not isinstance(rule, dict):
            continue
        feeds = rule.get("affectedFeeds") or []
        save_path = rule_save_path(rule)
        owned = own_bases is None or owns_folder(save_path, name, own_bases)
        mine = _rule_torrents(info, save_path, name, owned)
        out.append(RuleInfo(
            name=name,
            feed_url=feeds[0] if feeds else "",
            save_path=save_path,
            rule_enabled=bool(rule.get("enabled", True)),
            torrent_count=len(mine),
            file_count=(_dir_file_count(save_path) if owned
                        else _content_file_count(mine)),
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
    # Whether save_path is anirss's own folder for the rule; when it is not,
    # only the files of `torrents` are the rule's (#38).
    owns_folder: bool = True


def plan_remove(qbt: QbtSession, names: list[str], *, drop_torrents: bool,
                drop_files: bool, save_base: str,
                local_files: bool = True,
                own_bases: list[str] | None = None) -> list[RemovePlan]:
    """Validate every target up front (so a typo can't half-destroy) and
    gather what removal would touch. Raises NotFoundError on unknown names.

    A rule saving into its own `<base>/<name>` folder, under `own_bases`
    (`save_base` alone when not given), owns that folder and every torrent
    in it. One saving anywhere else, a show's folder say, shares it: only
    the torrents carrying its tag are its, and their files, never the
    folder (#38).

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
        save_path = _norm_path(rule_save_path(rule) or join_save_path(save_base, name))
        owned = owns_folder(save_path, name,
                            own_bases if own_bases is not None else [save_base])
        feed_path = _find_feed_path(qbt, feed_url) if feed_url else None
        torrents = (_torrents_for_savepath(qbt, save_path, name, owned)
                    if (drop_torrents or drop_files) else [])
        if not drop_files:
            size_bytes = 0
        elif local_files and owned:
            size_bytes = _dir_size_bytes(save_path) if os.path.isdir(save_path) else 0
        else:
            size_bytes = sum(t["size"] for t in torrents
                             if isinstance(t.get("size"), int))
        plans.append(RemovePlan(name, feed_url, feed_path, save_path, torrents,
                                size_bytes, len(torrents), owned))
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
        # Never on another machine's client: the same path here is not its. A
        # folder the rule shares is never deleted, only its torrents' files in it.
        if not (drop_files and not drop_torrents and local_files):
            continue
        if p.owns_folder and os.path.isdir(p.save_path):
            _safe_rmtree(p.save_path)
            notify(f"deleted files at {p.save_path}")
        elif not p.owns_folder:
            gone = [c for c in (t.get("content_path") or "" for t in p.torrents)
                    if c and os.path.lexists(c) and _inside(c, p.save_path)]
            for c in gone:
                if os.path.isdir(c) and not os.path.islink(c):
                    _safe_rmtree(c)
                else:
                    os.remove(c)
            if gone:
                notify(f"deleted {len(gone)} file(s) of {p.name} in {p.save_path}")


TORRENT_FIELDS = ("hash", "name", "state", "progress", "size", "dlspeed",
                  "upspeed", "eta", "num_seeds", "num_leechs", "ratio",
                  "save_path", "content_path", "added_on", "completion_on",
                  "tags", "category")


def list_torrents(qbt: QbtSession, *, save_path: str | None = None,
                  hashes: list[str] | None = None,
                  tag: str | None = None) -> list[dict]:
    """qB's torrent list trimmed to TORRENT_FIELDS, optionally narrowed to
    one save path (a subscription's folder), a tag (a rule's, in a folder it
    shares) or a set of info-hashes. Paths are as qBittorrent sees them (its
    host, not necessarily this one)."""
    params = {"hashes": "|".join(hashes)} if hashes else {}
    info = qbt.get_json("/api/v2/torrents/info", **params)
    if not isinstance(info, list):
        return []
    norm = _norm_path(save_path) if save_path else None
    return [{k: t.get(k) for k in TORRENT_FIELDS} for t in info
            if isinstance(t, dict)
            and (norm is None or _norm_path(t.get("save_path", "")) == norm)
            and (tag is None or tag in _torrent_tags(t))]


def name_torrents(qbt: QbtSession, name: str, save_base: str,
                  own_bases: list[str]) -> list[dict]:
    """The torrents of one subscription or download `name`: a rule of that
    name's, in the folder it saves into, only those carrying its tag when
    that folder is shared (#38); else what sits in `<save_base>/<name>`."""
    rules = qbt.get_json("/api/v2/rss/rules") or {}
    rule = rules.get(name) if isinstance(rules, dict) else None
    save_path = rule_save_path(rule) if isinstance(rule, dict) else ""
    if not save_path:
        return list_torrents(qbt, save_path=join_save_path(save_base, name))
    owned = owns_folder(save_path, name, own_bases)
    return list_torrents(qbt, save_path=save_path,
                         tag=None if owned else rule_tag(name))
