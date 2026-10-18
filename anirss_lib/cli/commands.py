"""High-level commands: cmd_query (-Q), cmd_sync (-Sy), cmd_remove (-R*)."""

import json
import os
from dataclasses import asdict
import shutil
import subprocess

from anirss_lib.ansi import (
    C_BLD, C_CYN, C_DIM, C_GRN, C_OFF, C_RED, C_YEL, FZF_BINDS,
)
from anirss_lib.config import FEEDS_CACHE_PATH, QbtConfig
from anirss_lib.logging import die
from anirss_lib.qbt.actions import (
    _human_bytes, execute_remove, list_subscriptions, plan_remove,
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
    subs = list_subscriptions(qbt)

    # Always refresh the cache as a side effect of -Q.
    write_feed_cache([r.name for r in subs])

    if json_format:
        print(json.dumps([asdict(r) for r in subs], indent=2))
        return
    if not subs:
        print(f"{C_DIM}(no feeds subscribed){C_OFF}")
        return

    for r in subs:
        n_torrents, n_files = r.torrent_count, r.file_count
        t_unit = "torrent" if n_torrents == 1 else "torrents"
        f_unit = "file" if n_files == 1 else "files"
        t_color = f"{C_BLD}{C_YEL}" if n_torrents > 0 else C_DIM
        f_color = f"{C_BLD}{C_YEL}" if n_files > 0 else C_DIM
        disabled = "" if r.rule_enabled else f"{C_RED}[disabled]{C_OFF} "
        print(
            f"{disabled}{C_BLD}{C_CYN}{r.name}{C_OFF} "
            f"{C_DIM}({C_OFF}{t_color}{n_torrents}{C_OFF}{C_DIM} {t_unit}){C_OFF}"
        )
        print(
            f"  {C_DIM}{C_CYN}└─{C_OFF}{C_GRN}{r.save_path}{C_OFF} "
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
               drop_files: bool, noconfirm: bool, save_base: str,
               local_files: bool = True) -> None:
    if drop_files and not drop_torrents and not local_files:
        die(f"{qbt_cfg.get('name', 'this client')}'s files are not on this machine: "
            f"remove them with their torrents (-Rns), which has qBittorrent delete them")
    qbt = login_with_retry(qbt_cfg)
    targets = _resolve_remove_targets(args, qbt)
    plans = plan_remove(qbt, targets, drop_torrents=drop_torrents,
                        drop_files=drop_files, save_base=save_base,
                        local_files=local_files)

    print()
    for p in plans:
        print(f"{C_BLD}About to remove:{C_OFF}")
        print(f"  {C_CYN}Feed:{C_OFF}    {p.name}")
        print(f"  {C_CYN}Rule:{C_OFF}    {p.name}")
        if drop_torrents:
            kept = "files deleted" if drop_files else "files kept"
            print(f"  {C_CYN}Torrents:{C_OFF} {len(p.torrents)} in qB "
                  f"(will be removed from qB, {kept})")
        elif drop_files and p.torrent_count_in_qb:
            print(f"  {C_YEL}Note:{C_OFF}    {p.torrent_count_in_qb} torrent(s) "
                  f"will be left in qB and will error (-n without -s)")
        if drop_files:
            if not local_files:
                shown = f"{p.save_path} {C_DIM}(on {qbt_cfg.get('name', 'the client')}){C_OFF}"
            elif os.path.isdir(p.save_path):
                shown = p.save_path
            else:
                shown = f"{p.save_path} {C_DIM}(does not exist){C_OFF}"
            print(f"  {C_CYN}Files:{C_OFF}   {shown}  "
                  f"{C_DIM}({_human_bytes(p.size_bytes)}){C_OFF}")
        print()

    if not noconfirm:
        try:
            reply = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            die("cancelled")
        if reply not in ("y", "yes"):
            print(f"{C_DIM}cancelled.{C_OFF}")
            return

    execute_remove(qbt, plans, drop_torrents=drop_torrents, drop_files=drop_files,
                   local_files=local_files,
                   notify=lambda msg: print(f"{C_GRN}OK:{C_OFF} {msg}"))
    refresh_feed_cache(qbt)
