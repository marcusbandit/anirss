"""The CLI for endpoint repos and the endpoint list:

    --add-repo <url>        fetch, verify, show the key, trust on a yes
    --repos                 refresh every repo and list what it offers
    --remove-repo <url|name>
    --install [id]          install an endpoint from a trusted repo
    --update-endpoints      move installed endpoints to their newest version
    --endpoints             list configured endpoints and where they came from
    --add-endpoint <url>    add one site by hand
    --remove-endpoint <name>

Thin printing and prompting over Anirss's repo methods."""

import getpass
import shutil
import sys
import time
from dataclasses import dataclass, field

from anirss_lib.ansi import C_BLD, C_CYN, C_DIM, C_GRN, C_OFF, C_RED, C_YEL
from anirss_lib.errors import AnirssError, FetchError, ValidationError
from anirss_lib.logging import die

# Where people find repos: the same pointer AniBeam's onboarding gives.
WHERE_TO_FIND = "https://everythingmoe.com"

FLAGS = ("--add-repo", "--repos", "--remove-repo", "--install",
         "--update-endpoints", "--endpoints", "--add-endpoint", "--remove-endpoint")


@dataclass
class _Opts:
    positional: list[str] = field(default_factory=list)
    name: str | None = None
    repo: str | None = None
    noconfirm: bool = False

    @classmethod
    def parse(cls, args: list[str]) -> "_Opts":
        out = cls()
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--noconfirm":
                out.noconfirm = True
            elif a in ("--name", "--repo"):
                if i + 1 >= len(args):
                    die(f"{a} requires a value")
                setattr(out, a[2:], args[i + 1])
                i += 1
            elif a.startswith(("--name=", "--repo=")):
                key, value = a[2:].split("=", 1)
                setattr(out, key, value)
            elif a.startswith("--"):
                die(f"unknown flag: {a}")
            else:
                out.positional.append(a)
            i += 1
        return out


def run(flag: str, args: list[str]) -> None:
    from anirss_lib.api import Anirss
    from anirss_lib.config import CONFIG_PATH
    opts = _Opts.parse(args)
    ani = Anirss(config_path=CONFIG_PATH)
    {
        "--add-repo": cmd_add_repo,
        "--repos": cmd_repos,
        "--remove-repo": cmd_remove_repo,
        "--install": cmd_install,
        "--update-endpoints": cmd_update,
        "--endpoints": cmd_endpoints,
        "--add-endpoint": cmd_add_endpoint,
        "--remove-endpoint": cmd_remove_endpoint,
    }[flag](ani, opts)


# ---- small helpers ---------------------------------------------------------

def _one(opts: _Opts, usage: str) -> str:
    if len(opts.positional) != 1:
        die(f"usage: {usage}")
    return opts.positional[0]


def confirm(question: str, noconfirm: bool = False) -> bool:
    if noconfirm:
        return True
    try:
        reply = input(f"{question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return reply in ("y", "yes")


class Cancelled(AnirssError):
    """Ctrl-C or end of input at a prompt."""


def ask(label: str, *, secret: bool = False) -> str:
    try:
        if secret and sys.stdin.isatty():
            return getpass.getpass(f"{label}: ").strip()
        return input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Cancelled("cancelled") from None


def choose(options: list[str], header: str) -> int | None:
    """Index of the option picked, with fzf when it is installed and a
    numbered list when it is not. None when cancelled."""
    if not options:
        return None
    if shutil.which("fzf") and sys.stdin.isatty():
        from anirss_lib.ansi import ansi_strip
        from anirss_lib.fzf import fzf_pick_one
        labels = [f"{i + 1:>2}  {o}" for i, o in enumerate(options)]
        pick = fzf_pick_one(labels, header, prompt_label="> ")
        if pick is None:
            return None
        return int(ansi_strip(pick).split()[0]) - 1
    print(header)
    for i, o in enumerate(options, start=1):
        print(f"  {C_BLD}{i:>2}{C_OFF}  {o}")
    while True:
        reply = ask(f"Number (1-{len(options)}, Enter cancels)")
        if not reply:
            return None
        if reply.isdigit() and 1 <= int(reply) <= len(options):
            return int(reply) - 1
        print(f"{C_YEL}type a number from the list{C_OFF}")


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    s = max(0, int(time.time() - ts))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = s // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


def _needs(d) -> str:
    if not d.auth:
        return ""
    return "asks for " + ", ".join(f.label for f in d.auth)


def needs_short(d) -> str:
    """What an endpoint asks for, short enough for one row."""
    if len(d.auth) == 1:
        return f"asks for {d.auth[0].label}"
    return f"asks for {len(d.auth)} settings" if d.auth else ""


def _row(mark: str, ident: str, name: str, note: str, width: int) -> str:
    from anirss_lib import terminal
    line = f"  {mark} {ident:<{width}}  {name:<24}"
    room = terminal.get_size().columns - (width + 32)
    if note and len(note) > room:
        note = needs_short_text(note, room)
    return f"{line} {C_DIM}{note}{C_OFF}" if note else line.rstrip()


def needs_short_text(note: str, room: int) -> str:
    return note[:max(10, room - 1)].rstrip(" ,") + "…"


# ---- repos -----------------------------------------------------------------

def print_index(url: str, index) -> None:
    nsfw = f"  {C_RED}NSFW{C_OFF}" if index.nsfw else ""
    usable = [d for d in index.endpoints if d.searchable]
    print(f"  {C_CYN}Repo{C_OFF}       {C_BLD}{index.name}{C_OFF}{nsfw}")
    print(f"  {C_CYN}Address{C_OFF}    {url}")
    print(f"  {C_CYN}Key{C_OFF}        {C_BLD}{index.fingerprint}{C_OFF}")
    print(f"  {C_CYN}Endpoints{C_OFF}  {len(index.endpoints)}, "
          f"{len(usable)} of them searchable here")
    width = max((len(d.id) for d in index.endpoints), default=0)
    for d in index.endpoints:
        if d.searchable:
            print(_row(f"{C_GRN}+{C_OFF}", d.id, d.name, _needs(d), width))
        else:
            print(_row(f"{C_DIM}·{C_OFF}", d.id, d.name,
                       f"not searchable here ({d.kind})", width))
    for ident, why in index.skipped:
        print(f"  {C_YEL}!{C_OFF} {ident}  {C_DIM}left out: {why}{C_OFF}")


def add_repo(ani, url: str, *, noconfirm: bool = False) -> bool:
    """Fetch, show, ask, trust. True when the repo was trusted."""
    print(f"{C_CYN}==>{C_OFF} fetching {url}")
    index = ani.preview_repo(url)
    print(f"{C_GRN}signature checks out.{C_OFF}")
    print_index(url, index)
    print()
    import textwrap

    from anirss_lib import terminal
    print(C_DIM + textwrap.fill(
        "Trusting a repo means anirss sends your searches to the sites its endpoints "
        "name. If someone runs the repo for you, check the key with them.",
        min(78, terminal.get_size().columns - 2)) + C_OFF)
    if not confirm("Trust this repo?", noconfirm):
        print(f"{C_DIM}not trusted, nothing saved.{C_OFF}")
        return False
    repo = ani.trust_repo(url, index.key, index=index)
    print(f"{C_GRN}✓{C_OFF} Trusted {C_BLD}{repo.name}{C_OFF}")
    return True


def cmd_add_repo(ani, opts: _Opts) -> None:
    url = _one(opts, "anirss --add-repo <url>")
    if add_repo(ani, url, noconfirm=opts.noconfirm):
        print(f"  Install an endpoint from it: {C_BLD}anirss --install{C_OFF}")


def cmd_repos(ani, opts: _Opts) -> None:
    current = ani.repos()
    if not current:
        print("No endpoint repos yet. anirss ships with no endpoints; a repo is a "
              "list of them that someone keeps.")
        print(f"  Add one:            {C_BLD}anirss --add-repo <url>{C_OFF}")
        print(f"  Where to find repos: {WHERE_TO_FIND}")
        return
    print(f"{C_CYN}==>{C_OFF} checking {len(current)} repo(s) for updates")
    repos = ani.repos(refresh=True)
    offers = ani.repo_endpoints()
    for repo in repos:
        nsfw = f"  {C_RED}NSFW{C_OFF}" if repo.nsfw else ""
        print()
        print(f"{C_BLD}{repo.name}{C_OFF}{nsfw}  {C_DIM}{repo.fingerprint}{C_OFF}")
        print(f"  {C_DIM}{repo.url}{C_OFF}")
        if repo.error:
            print(f"  {C_YEL}! {repo.error}; using the list from "
                  f"{_ago(repo.fetched_at)}{C_OFF}")
        mine = [o for o in offers if o.repo == repo.url]
        width = max((len(o.definition.id) for o in mine), default=0)
        for o in mine:
            d = o.definition
            if o.update:
                print(_row(f"{C_YEL}↑{C_OFF}", d.id, d.name,
                           f"update {o.installed_version} → {d.version}: "
                           f"anirss --update-endpoints", width))
            elif o.installed_as:
                as_name = "" if o.installed_as == d.id else f" as {o.installed_as}"
                print(_row(f"{C_GRN}●{C_OFF}", d.id, d.name, f"installed{as_name}", width))
            elif o.note:
                print(_row(f"{C_DIM}·{C_OFF}", d.id, d.name, f"not searchable here "
                           f"({d.kind})", width))
            else:
                print(_row("○", d.id, d.name, _needs(d), width))
    from anirss_lib import repos as repos_mod
    from anirss_lib.repos import load_state
    for inst in repos_mod.orphaned(load_state(ani.repos_file)):
        print(f"{C_YEL}!{C_OFF} {inst.name}: its repo no longer offers it. It keeps "
              f"working until you remove it: anirss --remove-endpoint {inst.name}")


def cmd_remove_repo(ani, opts: _Opts) -> None:
    ref = _one(opts, "anirss --remove-repo <url|name>")
    if not confirm(f"Stop trusting {ref}?", opts.noconfirm):
        print(f"{C_DIM}cancelled.{C_OFF}")
        return
    repo = ani.remove_repo(ref)
    kept = [n for n, i in ani.installed_endpoints().items() if i.repo == repo.url]
    print(f"{C_GRN}✓{C_OFF} Removed {C_BLD}{repo.name}{C_OFF}")
    if kept:
        print(f"  {C_DIM}{', '.join(kept)} stay installed and keep working; remove "
              f"them with anirss --remove-endpoint <name>{C_OFF}")


# ---- endpoints from repos -----------------------------------------------------

def ask_fields(fields) -> dict[str, str]:
    values: dict[str, str] = {}
    for f in fields:
        print()
        print(f"  {C_BLD}{f.label}{C_OFF}")
        if f.help:
            print(f"  {C_DIM}{f.help}{C_OFF}")
        if f.help_url:
            print(f"  {C_DIM}{f.help_url}{C_OFF}")
        if f.key == "host" and not f.secret:
            print(f"  {C_DIM}host or host:port, for example 127.0.0.1:9117{C_OFF}")
        while True:
            v = ask(f"  {f.label}", secret=f.secret)
            if v:
                values[f.key] = v
                break
            print(f"  {C_YEL}this one is needed; Ctrl-C cancels{C_OFF}")
    return values


def install(ani, offer, *, name: str | None = None) -> None:
    d = offer.definition
    print(f"{C_CYN}==>{C_OFF} installing {C_BLD}{d.name}{C_OFF} from {offer.repo_name}")
    values = {}
    if d.auth:
        n = len(d.auth)
        print(f"  It needs {n} setting{'' if n == 1 else 's'} before it can search.")
        values = ask_fields(d.auth)
    first = not ani.endpoints()
    ep = ani.install_endpoint(d.id, values, repo=offer.repo, name=name)
    print(f"{C_GRN}✓{C_OFF} Installed {C_BLD}{ep.name}{C_OFF}")
    if first:
        print(f"  It is your only endpoint, so every search uses it: "
              f"{C_BLD}anirss <words>{C_OFF}")
    else:
        print(f"  Search it: {C_BLD}anirss -e {ep.name} <words>{C_OFF} "
              f"(Ctrl-E switches endpoints while searching)")


def cmd_install(ani, opts: _Opts) -> None:
    if len(opts.positional) > 1:
        die("usage: anirss --install [id] [--repo <url|name>] [--name <name>]")
    if opts.positional:
        offer = ani.repo_endpoint(opts.positional[0], repo=opts.repo)
        if offer.note:
            die(f"{offer.definition.id}: {offer.note}")
        install(ani, offer, name=opts.name)
        return
    if not ani.repos():
        die("no endpoint repos yet: add one with `anirss --add-repo <url>` "
            f"(where to find repos: {WHERE_TO_FIND})")
    offers = [o for o in ani.repo_endpoints()
              if not o.note and not o.installed_as
              and (opts.repo is None or opts.repo in (o.repo, o.repo_name))]
    if not offers:
        print("Everything your repos offer that anirss can search is installed.")
        return
    labels = [f"{o.definition.name}  {C_DIM}{o.definition.id} · {o.repo_name}"
              f"{' · ' + _needs(o.definition) if o.definition.auth else ''}{C_OFF}"
              for o in offers]
    i = choose(labels, "Pick an endpoint to install")
    if i is None:
        print(f"{C_DIM}cancelled.{C_OFF}")
        return
    install(ani, offers[i], name=opts.name)


def cmd_update(ani, opts: _Opts) -> None:
    if ani.repos():
        print(f"{C_CYN}==>{C_OFF} checking repos")
        ani.repos(refresh=True)
    updates = [o for o in ani.repo_endpoints() if o.update]
    if not updates:
        print("Every installed endpoint is up to date.")
        return
    for o in updates:
        d = o.definition
        name = o.installed_as or d.id
        print(f"{C_CYN}==>{C_OFF} {name}: {o.installed_version} → {d.version}")
        fields = ani.update_needs(name)
        creds = None
        if fields:
            print(f"  {C_YEL}This version changes what it asks for or where it sends "
                  f"it ({d.host}), so it asks again.{C_OFF}")
            creds = ask_fields(fields)
        ani.update_endpoint(name, creds)
        print(f"{C_GRN}✓{C_OFF} {name} is on version {d.version}")


# ---- the endpoint list -------------------------------------------------------

def cmd_endpoints(ani, opts: _Opts) -> None:
    eps = ani.endpoints()
    if not eps:
        print("No search endpoints yet.")
        print(f"  From a repo:   {C_BLD}anirss --add-repo <url>{C_OFF}, "
              f"then {C_BLD}anirss --install{C_OFF}")
        print(f"  By hand:       {C_BLD}anirss --add-endpoint <url>{C_OFF}")
        print(f"  Where to find repos: {WHERE_TO_FIND}")
        return
    installed = ani.installed_endpoints()
    repos = {r.url: r for r in ani.repos()}
    offers = {(o.repo, o.definition.id): o for o in ani.repo_endpoints()}
    print(f"{C_BLD}Endpoints{C_OFF}, in the order anirss tries them:")
    nw = max(len(e.name) for e in eps)
    for i, ep in enumerate(eps, start=1):
        inst = installed.get(ep.name)
        if inst is not None:
            repo = repos.get(inst.repo)
            where = f"from {repo.name if repo else inst.repo}, v{inst.version}"
            offer = offers.get((inst.repo, inst.id))
            if offer is not None and offer.update:
                where += f" {C_YEL}(v{offer.definition.version} available){C_OFF}{C_DIM}"
        else:
            where = "by hand"
        if ep.on_demand:
            where += ", on-demand"
        print(f"  {i:>2}  {C_BLD}{ep.name:<{nw}}{C_OFF}  {ep.kind:<4}  "
              f"{ani.endpoint_view(ep)['url']}  {C_DIM}{where}{C_OFF}")


def add_by_hand(ani, url: str, *, name: str | None = None, noconfirm: bool = False):
    """Guess, try and add one site by its address. Returns the endpoint, or
    None when the user said no. Raises AnirssError."""
    from anirss_lib import endpoint_config
    from anirss_lib import endpoints as endpoints_mod
    from anirss_lib.endpoints import Endpoint
    taken = tuple(e.name for e in ani.endpoints())
    try:
        spec = endpoint_config.guess_spec(url, name=name, taken=taken)
    except FetchError as e:
        raise FetchError(f"{e}. Check the address, or put {{query}} where the "
                         f"search words go") from None
    except ValidationError:
        if "{query}" in url or not sys.stdin.isatty():
            raise
        print("That address is not a nyaa-style site and has no {query} in it.")
        print(f"{C_DIM}If you copied it from a search you ran on the site, type the "
              f"words you searched for and anirss puts {{query}} there.{C_OFF}")
        words = ask("Search words in the address")
        spec = endpoint_config.guess_spec(endpoint_config.put_query(url, words),
                                          name=name, taken=taken)
    ep = Endpoint(name=spec["name"], kind=spec["kind"], url=spec["url"],
                  category=spec.get("category", "1_0"), filter=spec.get("filter", "0"))
    print(f"{C_CYN}==>{C_OFF} trying a search on {spec['name']} ({spec['kind']})")
    try:
        n = len(endpoints_mod.fetch_items(ep, "a"))
        print(f"  {C_GRN}answers{C_OFF} ({n} result{'' if n == 1 else 's'} for a test search)")
    except FetchError as e:
        print(f"  {C_YEL}{e}{C_OFF}")
        if not confirm("It did not answer. Add it anyway?", noconfirm):
            print(f"{C_DIM}not added.{C_OFF}")
            return None
    ep, replaced = ani.add_endpoint(spec)
    print(f"{C_GRN}✓{C_OFF} {'Replaced' if replaced else 'Added'} {C_BLD}{ep.name}{C_OFF}. "
          f"Search it: {C_BLD}anirss -e {ep.name} <words>{C_OFF}")
    return ep


def cmd_add_endpoint(ani, opts: _Opts) -> None:
    url = _one(opts, "anirss --add-endpoint <url> [--name <name>]")
    add_by_hand(ani, url, name=opts.name, noconfirm=opts.noconfirm)


def cmd_remove_endpoint(ani, opts: _Opts) -> None:
    name = _one(opts, "anirss --remove-endpoint <name>")
    ani.endpoint(name)
    if not confirm(f"Remove the endpoint {name}? Its subscriptions keep running "
                   f"in qBittorrent.", opts.noconfirm):
        print(f"{C_DIM}cancelled.{C_OFF}")
        return
    ani.remove_endpoint(name)
    print(f"{C_GRN}✓{C_OFF} Removed {C_BLD}{name}{C_OFF}")
