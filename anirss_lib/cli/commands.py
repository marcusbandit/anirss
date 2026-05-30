"""High-level commands: cmd_query (-Q), cmd_sync (-Sy), cmd_remove (-R*)."""

import json
import os
import shutil
import subprocess

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
    qbt = login_with_retry(qbt_cfg)
    rules = qbt.get_json("/api/v2/rss/rules") or {}
    if not isinstance(rules, dict):
        rules = {}
    info = qbt.get_json("/api/v2/torrents/info")

    # Always refresh the cache as a side effect of -Q.
    write_feed_cache(list(rules.keys()))

    if not rules:
        if json_format:
            print("[]")
        else:
            print(f"{C_DIM}(no feeds subscribed){C_OFF}")
        return

    # Pre-count torrents per save_path so each lookup is O(1).
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
    """Return the list of rule names to remove. If args is empty and fzf is available,
    multi-select from the live qB rule list. Errors out with a clear message otherwise.
    """
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

    # Build per-target plans, validating up front so we don't half-destroy on a typo.
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
            # B3 fix: was a redundant second call to _torrents_for_savepath in
            # the (drop_files and not drop_torrents) branch. `torrents` is
            # populated whenever (drop_torrents or drop_files), so the count
            # is always available without a second fetch.
            "torrent_count_in_qb": len(torrents),
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
        # Files: delete the directory unless qB already cleaned it (drop_torrents+drop_files).
        if drop_files and not drop_torrents and os.path.isdir(p["save_path"]):
            _safe_rmtree(p["save_path"])
            print(f"{C_GRN}OK:{C_OFF} deleted files at {p['save_path']}")

    refresh_feed_cache(qbt)
