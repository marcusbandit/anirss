"""`anirss --setup`, which the first interactive run opens by itself: what
anirss does, then connecting qBittorrent, where downloads go, and search
endpoints from a repo. Every step can be skipped and done later; a skipped
one leaves a pointer on the last screen, never a nag.

It writes only the keys it asked about into config.toml, and remembers that
it ran in ~/.config/anirss/onboarded so it opens once."""

import getpass
import os
import shutil
import sys
import textwrap
import time

import anirss_lib
from anirss_lib import terminal
from anirss_lib.ansi import C_BLD, C_CYN, C_DIM, C_GRN, C_OFF, C_RED, C_YEL
from anirss_lib.cli import repos as repo_cli
from anirss_lib.config import CONFIG_PATH
from anirss_lib.errors import AnirssError, QbtError

ONBOARDED = CONFIG_PATH.parent / "onboarded"
STEPS = 3

# qBittorrent's Web UI, with the menu labels as qBittorrent shows them.
WEBUI_STEPS = [
    "Open qBittorrent, then Tools › Options (macOS: qBittorrent › Settings).",
    "Pick Web UI in the list on the left.",
    "Tick \"Web User Interface (Remote control)\". Note the Port: 8080 unless you change it.",
    "Under Authentication, set a Username and a Password.",
    "Click OK.",
]
PORTS = (8080, 8081, 8090, 9090)


# ---- the marker --------------------------------------------------------------

def needs_onboarding(cfg) -> bool:
    """True on a first run: setup never ran and nothing is configured to
    search. An existing setup gets the marker quietly, so an upgrade never
    walks a working user through setup."""
    if ONBOARDED.exists():
        return False
    if cfg.get("endpoint"):
        mark_onboarded()
        return False
    return True


def mark_onboarded() -> None:
    try:
        ONBOARDED.parent.mkdir(parents=True, exist_ok=True)
        ONBOARDED.write_text(f"anirss {anirss_lib.__version__} "
                             f"{time.strftime('%Y-%m-%d')}\n", encoding="utf-8")
    except OSError:
        pass


# ---- drawing -----------------------------------------------------------------

def _width() -> int:
    return max(40, min(78, terminal.get_size().columns - 4))


def say(text: str = "", *, dim: bool = False, indent: int = 2) -> None:
    """A paragraph, wrapped to the terminal."""
    if not text:
        print()
        return
    pad = " " * indent
    # Commands are glued with no-break spaces so a line never splits one.
    body = textwrap.fill(text, _width(), initial_indent=pad, subsequent_indent=pad,
                         break_on_hyphens=False, break_long_words=False)
    body = body.replace("\u00a0", " ")
    print(f"{C_DIM}{body}{C_OFF}" if dim else body)


def cmd(text: str) -> str:
    """A command for say(): bold, and never split across lines."""
    return f"{C_BLD}{text.replace(' ', chr(0xA0))}{C_OFF}"


def screen(step: int | None, title: str) -> None:
    terminal.clear_screen()
    where = f"step {step} of {STEPS} · " if step else ""
    print()
    print(f"  {C_BLD}{C_CYN}anirss setup{C_OFF}  {C_DIM}{where}{title}{C_OFF}")
    print(f"  {C_DIM}{'─' * min(_width(), 60)}{C_OFF}")
    print()


def ok(text: str) -> None:
    print(f"  {C_GRN}✓{C_OFF} {text}")


def warn(text: str) -> None:
    print(f"  {C_YEL}!{C_OFF} {text}")


def ask(label: str, default: str = "", *, secret: bool = False) -> str:
    shown = f" [{default}]" if default else ""
    try:
        if secret and sys.stdin.isatty():
            value = getpass.getpass(f"  {label}{shown}: ")
        else:
            value = input(f"  {label}{shown}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        raise _Cancel() from None
    return value.strip() or default


def yes(question: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    reply = ask(f"{question} {hint}").lower()
    if not reply:
        return default
    return reply in ("y", "yes")


class _Cancel(Exception):
    """Ctrl-C or end of input: leave setup, keep what was saved so far."""


def _home(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


# ---- the steps ---------------------------------------------------------------

def welcome() -> bool:
    """False when the user skips setup altogether."""
    screen(None, "welcome")
    say("anirss finds anime releases on the search sites you add and hands them "
        "to qBittorrent. Subscribe to a show and every new episode downloads by "
        "itself into that show's folder, or pick releases and download them now. "
        "Then watch them in whatever player you like.")
    say()
    say(f"Three short steps: connect qBittorrent, choose where downloads go, add "
        f"search endpoints. Each can be skipped and done later with "
        f"{cmd('anirss --setup')}.")
    say()
    return ask("Press Enter to start, or type s to skip setup").lower() not in ("s", "skip")


def _candidates(cfg) -> list[str]:
    urls = [cfg["qbittorrent"]["url"].rstrip("/")]
    for port in PORTS:
        u = f"http://localhost:{port}"
        if u not in urls:
            urls.append(u)
    return urls


def _find_qbt(cfg) -> str | None:
    from anirss_lib.qbt.session import qbt_answer
    for url in _candidates(cfg):
        if qbt_answer(url, timeout=1.5) == "qbt":
            return url
    return None


def _login(url: str, username: str, client: str):
    """(session, username) after a login on the spot, or (None, username)."""
    from anirss_lib.qbt.session import (
        QbtSession,
        _make_qbt_opener,
        _save_password,
        qbt_login,
    )
    open_sess = QbtSession(_make_qbt_opener(url)[0], url)
    if open_sess.is_alive():
        ok("qBittorrent lets anirss in from this machine without a password.")
        return open_sess, username
    say("Log in with the Web UI's username and password, the ones under "
        "Authentication in qBittorrent's Web UI settings.", dim=True)
    username = ask("Username", username or "admin")
    for attempt in range(1, 4):
        password = ask(f"Password ({attempt}/3)", secret=True)
        if not password:
            warn("empty, try again")
            continue
        qbt, err = qbt_login(url, username, password, client=client)
        if qbt is not None:
            if yes("Remember the password, so anirss logs in by itself?"):
                if _save_password(password, client):
                    ok("Saved, readable by you only.")
                else:
                    warn("Couldn't save it; anirss asks again next time.")
            return qbt, username
        warn(f"{err}. qBittorrent locks an address out for a while after a few "
             f"wrong passwords, so check it in its Web UI settings first.")
    return None, username


def step_qbt(cfg, notes: list[str]) -> None:
    from anirss_lib import config_edit
    from anirss_lib.qbt.actions import RSS_PREFS, ensure_rss_on, rss_prefs_off
    from anirss_lib.qbt.session import (
        _is_local_url,
        _launch_qbt,
        _qbt_start_command,
        _wait_until_reachable,
        qbt_answer,
    )
    screen(1, "connect qBittorrent")
    say("anirss drives qBittorrent through its Web UI, the remote-control page "
        "qBittorrent can serve. It adds feeds, rules and torrents there and never "
        "touches the rest of your torrents. qBittorrent can be on this machine or "
        "another one, a seedbox say.")
    say()
    multi = bool(cfg["qbittorrent"].get("client"))
    if multi:
        warn("config.toml names several qBittorrents; setup checks the first and "
             "leaves the blocks as they are.")
    url = _find_qbt(cfg)
    while url is None:
        configured = cfg["qbittorrent"]["url"]
        start = _qbt_start_command(cfg["qbittorrent"])
        nox = start and "nox" in os.path.basename(start[0]) and _is_local_url(configured)
        if nox and yes("qbittorrent-nox is installed but not running. Start it now?"):
            _launch_qbt(start)
            print(f"  {C_DIM}waiting for its Web UI…{C_OFF}")
            if _wait_until_reachable(configured, timeout=20):
                url = configured
                break
        print(f"  {C_YEL}No qBittorrent Web UI answers here yet.{C_OFF} Turn it on:")
        print()
        for i, line in enumerate(WEBUI_STEPS, start=1):
            print(f"    {C_BLD}{i}.{C_OFF} {line}")
        print()
        say("qbittorrent-nox, the version without a window, has the Web UI on "
            "already (port 8080) and prints a temporary password the first time "
            "it starts. qBittorrent on another machine? Type its address, like "
            "http://192.168.1.20:8080.", dim=True)
        say()
        reply = ask("Enter looks again · an address · s skips")
        if reply.lower() in ("s", "skip"):
            notes.append("qBittorrent: anirss --setup, or set [qbittorrent] url "
                         "in config.toml")
            return
        if reply:
            candidate = reply if "://" in reply else f"http://{reply}"
            answer = qbt_answer(candidate)
            if answer == "qbt":
                url = candidate.rstrip("/")
            elif answer == "other":
                warn(f"{candidate} answers, but it is not qBittorrent's Web UI. "
                     f"Check the port.")
            else:
                warn(f"nothing answers at {candidate}.")
            continue
        url = _find_qbt(cfg)
    screen(1, "connect qBittorrent")
    ok(f"Found qBittorrent's Web UI at {C_BLD}{url}{C_OFF}")
    print()
    client = cfg["qbittorrent"].get("name", "") if not multi else ""
    qbt, username = _login(url, cfg["qbittorrent"].get("username", "admin"), client)
    if qbt is None:
        notes.append("qBittorrent login: anirss asks for the password on the next "
                     "run, or set ANIRSS_QBT_PASSWORD")
    else:
        try:
            version = qbt.get("/api/v2/app/version").strip()
        except QbtError:
            version = ""
        ok(f"Logged in as {C_BLD}{username}{C_OFF}"
           f"{f', qBittorrent {version}' if version else ''}")
        off = rss_prefs_off(qbt)
        if off:
            print()
            what = " and ".join(RSS_PREFS[k] for k in off)
            say(f"qBittorrent ships with {what} off, and a subscription needs "
                f"{'it' if len(off) == 1 else 'both'}: without "
                f"{'it' if len(off) == 1 else 'them'} no new episode ever downloads.")
            if yes("Turn them on now?"):
                ensure_rss_on(qbt)
                ok("RSS fetching and auto-download are on.")
            else:
                notes.append("RSS in qBittorrent: your first subscription turns it on")
        else:
            ok("RSS fetching and auto-download are on.")
    if not multi:
        config_edit.set_values(CONFIG_PATH, "qbittorrent",
                               {"url": url, "username": username})
        cfg["qbittorrent"]["url"], cfg["qbittorrent"]["username"] = url, username
    print()
    ask("Press Enter to continue")


def step_downloads(cfg, notes: list[str]) -> None:
    from anirss_lib import config_edit
    from anirss_lib.qbt.clients import is_local_url
    screen(2, "where downloads go")
    local = is_local_url(cfg["qbittorrent"]["url"])
    say("Each show gets its own folder in one place, and movies go to another. "
        "Point your player or media server (mpv, VLC, Jellyfin, Plex, Kodi) at the "
        "shows folder and new episodes turn up there by themselves.")
    if not local:
        say()
        warn("qBittorrent is on another machine, so these are folders on that "
             "machine, written out in full.")
    print()
    shows = cfg["downloads"]["save_base"]
    movies = cfg["downloads"]["movie_path"]
    print(f"    {C_CYN}Shows{C_OFF}   {_home(shows)}/{C_DIM}<show name>/{C_OFF}")
    print(f"    {C_CYN}Movies{C_OFF}  {_home(movies)}/")
    print()
    say("Enter keeps a folder, or type another.", dim=True)
    new_shows = ask("Shows folder", _home(shows))
    new_movies = ask("Movies folder", _home(movies))
    changed = {}
    if os.path.expanduser(new_shows) != shows:
        changed["save_base"] = new_shows
    if os.path.expanduser(new_movies) != movies:
        changed["movie_path"] = new_movies
    if changed:
        config_edit.set_values(CONFIG_PATH, "downloads", changed)
        cfg["downloads"]["save_base"] = os.path.expanduser(new_shows)
        cfg["downloads"]["movie_path"] = os.path.expanduser(new_movies)
        ok("Saved.")
    else:
        ok("Keeping those.")
    print()
    ask("Press Enter to continue")


def step_endpoints(notes: list[str]) -> None:
    from anirss_lib.api import Anirss
    screen(3, "search endpoints")
    ani = Anirss(config_path=CONFIG_PATH)
    say("anirss ships with no search sites. You add them from an endpoint repo: a "
        "signed list of endpoints that someone keeps. You check the repo's key "
        "once, then install the endpoints you want; anirss asks for any key or "
        "token one needs. A repo made for AniBeam works here too.")
    say()
    print(f"  Where to find repos: {C_BLD}{repo_cli.WHERE_TO_FIND}{C_OFF}")
    have = ani.endpoints()
    if have:
        print()
        ok(f"Already set up: {', '.join(e.name for e in have)}")
    print()
    while True:
        reply = ask("Repo URL · h adds one site by hand · Enter skips")
        if not reply:
            if not ani.endpoints():
                notes.append("search endpoints: anirss --add-repo <url>, then "
                             "anirss --install")
            return
        try:
            if reply.lower() in ("h", "hand"):
                addr = ask("The site's address (a search URL with {query}, or a "
                           "nyaa-style site)")
                if addr and repo_cli.add_by_hand(ani, addr):
                    return
            elif _trust(ani, reply):
                _install_loop(ani)
                return
        except AnirssError as e:
            warn(str(e))
        print()


def _trust(ani, url: str) -> bool:
    screen(3, "check the repo")
    print(f"  {C_DIM}fetching {url}{C_OFF}")
    index = ani.preview_repo(url)
    screen(3, "check the repo")
    ok("The signature checks out: this list is exactly what the key's owner signed.")
    print()
    repo_cli.print_index(url, index)
    print()
    say("Trusting a repo means anirss sends your searches to the sites its endpoints "
        "name. If someone runs the repo for you, check that key with them; anirss "
        "refuses any later list signed by another key.", dim=True)
    say()
    if not yes("Trust this repo?", default=False):
        warn("Not trusted, nothing saved.")
        return False
    repo = ani.trust_repo(url, index.key, index=index)
    ok(f"Trusted {C_BLD}{repo.name}{C_OFF}")
    return True


def _install_loop(ani) -> None:
    while True:
        offers = [o for o in ani.repo_endpoints() if not o.note and not o.installed_as]
        if not offers:
            return
        screen(3, "install endpoints")
        mine = [e.name for e in ani.endpoints()]
        if mine:
            ok(f"Installed so far: {', '.join(mine)}")
            print()
        say("Pick one to install. Those that ask for a setting (an API key, a "
            "token, your own Jackett's address) ask for it next.", dim=True)
        print()
        labels = [f"{o.definition.name}  {C_DIM}{o.definition.id}"
                  f"{' · ' + repo_cli.needs_short(o.definition) if o.definition.auth else ''}"
                  f"{C_OFF}" for o in offers]
        i = repo_cli.choose(labels, "Pick an endpoint to install (Esc when done)")
        if i is None:
            return
        d = offers[i].definition
        screen(3, f"install {d.name}")
        values = {}
        if d.auth:
            n = len(d.auth)
            say(f"{d.name} needs {n} setting{'' if n == 1 else 's'} before it can "
                f"search. They go into config.toml, readable by you only.")
            values = repo_cli.ask_fields(d.auth)
            print()
        try:
            ep = ani.install_endpoint(d.id, values, repo=offers[i].repo)
            ok(f"Installed {C_BLD}{ep.name}{C_OFF}")
        except AnirssError as e:
            warn(str(e))
        print()
        if not yes("Install another?", default=False):
            return


def done(cfg, notes: list[str]) -> None:
    from anirss_lib.api import Anirss
    screen(None, "done")
    try:
        names = [e.name for e in Anirss(config_path=CONFIG_PATH).endpoints()]
    except AnirssError:
        names = []
    ready = not any(n.startswith(("qBittorrent", "search")) for n in notes)
    print(f"  {C_GRN}{C_BLD}{'✓ anirss is ready' if ready else '✓ setup saved'}{C_OFF}")
    print()
    print(f"    {C_CYN}qBittorrent{C_OFF}  {cfg['qbittorrent']['url']}")
    print(f"    {C_CYN}Shows{C_OFF}        {_home(cfg['downloads']['save_base'])}")
    print(f"    {C_CYN}Endpoints{C_OFF}    {', '.join(names) if names else C_DIM + 'none yet' + C_OFF}")
    if notes:
        print()
        print(f"  {C_DIM}Still to do, whenever you like:{C_OFF}")
        for n in notes:
            print(f"    {C_DIM}· {n}{C_OFF}")
    print()
    if names:
        print(f"  Search:             {C_BLD}anirss Frieren{C_OFF}")
    print(f"  Check everything:   {C_BLD}anirss --doctor{C_OFF}")
    print(f"  Run this again:     {C_BLD}anirss --setup{C_OFF}")
    print()


def run(cfg, *, first_run: bool = False) -> bool:
    """The whole walk. True when it finished (the caller may go on to the
    search the user typed); False when skipped or cancelled."""
    notes: list[str] = []
    try:
        if not welcome():
            mark_onboarded()
            print(f"  {C_DIM}Skipped. {C_BLD}anirss --setup{C_OFF}{C_DIM} runs it "
                  f"any time.{C_OFF}")
            return False
        step_qbt(cfg, notes)
        step_downloads(cfg, notes)
        step_endpoints(notes)
    except _Cancel:
        print(f"  {C_DIM}Setup stopped; what you saved stays. "
              f"{C_BLD}anirss --setup{C_OFF}{C_DIM} picks it up again.{C_OFF}")
        return False
    mark_onboarded()
    done(cfg, notes)
    if first_run and sys.stdin.isatty():
        try:
            input(f"  {C_DIM}Press Enter to start searching…{C_OFF}")
        except (EOFError, KeyboardInterrupt):
            print()
            return False
    return True


def cmd_setup(cfg) -> None:
    if not sys.stdin.isatty():
        print(f"{C_RED}error:{C_OFF} anirss --setup asks questions; run it in a terminal",
              file=sys.stderr)
        sys.exit(1)
    if not shutil.which("fzf"):
        from anirss_lib.compat import install_hint
        warn(f"fzf is not installed. Setup works without it, but the search picker "
             f"needs it: {install_hint()}")
    run(cfg)
