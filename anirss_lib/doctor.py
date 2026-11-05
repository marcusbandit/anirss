"""Setup checks: everything a working anirss needs, each with what was found
and one line on how to fix it. Silent core logic behind `anirss --doctor` and
`GET /api/v1/doctor`; it never prompts, never starts qBittorrent and never
changes a setting."""

import os
import shutil
from dataclasses import dataclass

from anirss_lib.compat import install_hint
from anirss_lib.errors import AnirssError, FetchError, QbtAuthError, QbtError

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# The menu path that turns qBittorrent's Web UI on, with its real labels.
WEBUI_HOWTO = ("in qBittorrent: Tools > Options > Web UI (macOS: qBittorrent > "
               "Settings > Web UI), tick \"Web User Interface (Remote control)\", "
               "note the Port, set a username and password under Authentication")
RSS_HOWTO = ("anirss turns them on with your next subscription; or in qBittorrent: "
             "Tools > Options > RSS, tick \"Enable fetching RSS feeds\" and "
             "\"Enable auto downloading of RSS torrents\"")


@dataclass
class Check:
    id: str          # stable: "qbt_login", "endpoint:nyaa", "repo:<url>", ...
    title: str       # what is checked, a word or two
    status: str      # ok | warn | fail | skip
    detail: str      # what was found
    fix: str = ""    # how to fix it, when it is not ok


def _version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.lstrip("v").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def check_qbt(ani, client: dict) -> list[Check]:
    from anirss_lib.qbt.actions import RSS_PREFS, rss_prefs_off
    from anirss_lib.qbt.clients import local_files
    from anirss_lib.qbt.session import _qbt_start_command, qbt_answer
    from anirss_lib.redact import redact_url
    name = client.get("name", "")
    url = client["url"]
    shown = redact_url(url)
    tag = f" ({name})" if len(ani.clients()) > 1 else ""
    answer = qbt_answer(url)
    if answer == "other":
        return [Check(f"qbt:{name}", f"qBittorrent{tag}", FAIL,
                      f"something answers at {shown}, but not qBittorrent's Web UI",
                      "check the port in [qbittorrent] url (anirss --setup finds it)")]
    if answer is None:
        if local_files(client) and _qbt_start_command(client):
            fix = "anirss starts it on the next interactive run; or start it yourself"
        elif local_files(client):
            fix = "install qBittorrent (or qbittorrent-nox) and turn its Web UI on: " + WEBUI_HOWTO
        else:
            fix = "start it on that machine, and check the address in [qbittorrent] url"
        return [Check(f"qbt:{name}", f"qBittorrent{tag}", FAIL,
                      f"nothing answers at {shown}", fix)]
    out = [Check(f"qbt:{name}", f"qBittorrent{tag}", OK, f"answers at {shown}")]
    try:
        version = ani._with_qbt(lambda q: q.get("/api/v2/app/version").strip(), name)
        off = ani._with_qbt(rss_prefs_off, name)
    except QbtAuthError as e:
        fix = ("the password was refused: run anirss once and type the right one"
               if e.rejected else
               "run anirss once and save the password when asked, or set ANIRSS_QBT_PASSWORD")
        out.append(Check(f"qbt_login:{name}", f"login{tag}", FAIL, str(e), fix))
        return out
    except QbtError as e:
        out.append(Check(f"qbt_login:{name}", f"login{tag}", FAIL, str(e),
                         "check that the address is qBittorrent's Web UI"))
        return out
    out.append(Check(f"qbt_login:{name}", f"login{tag}", OK,
                     f"logged in as {client.get('username', '')}, qBittorrent {version}"))
    if version and _version_tuple(version) < (4, 6):
        out.append(Check(f"qbt_version:{name}", f"version{tag}", WARN,
                         f"qBittorrent {version}: subscriptions work, but their "
                         f"episodes are not tagged", "update qBittorrent to 4.6 or newer"))
    if off is None:
        out.append(Check(f"qbt_rss:{name}", f"RSS{tag}", WARN,
                         "couldn't read qBittorrent's RSS settings",
                         "look in Tools > Options > RSS: tick \"Enable fetching RSS "
                         "feeds\" and \"Enable auto downloading of RSS torrents\""))
    elif off:
        what = " and ".join(RSS_PREFS[k] for k in off)
        out.append(Check(f"qbt_rss:{name}", f"RSS{tag}", WARN,
                         f"{what} {'is' if len(off) == 1 else 'are'} off, so a "
                         f"subscription downloads nothing", RSS_HOWTO))
    else:
        out.append(Check(f"qbt_rss:{name}", f"RSS{tag}", OK,
                         "RSS fetching and auto-download are on"))
    return out


def check_endpoints(ani, *, search: bool) -> list[Check]:
    from anirss_lib import endpoints as endpoints_mod
    from anirss_lib import ondemand
    eps = ani.endpoints()
    if not eps:
        return [Check("endpoints", "endpoints", FAIL, "none configured, so there is "
                      "nothing to search",
                      "anirss --add-repo <url> then anirss --install, or "
                      "anirss --add-endpoint <url>")]
    out = [Check("endpoints", "endpoints", OK,
                 f"{len(eps)} configured: {', '.join(e.name for e in eps)}")]
    if not search:
        return out
    for ep in eps:
        if ep.on_demand and not ondemand.usable(ep):
            out.append(Check(f"endpoint:{ep.name}", ep.name, SKIP,
                             "on-demand and not running; it starts when you pick it"))
            continue
        try:
            n = len(endpoints_mod.fetch_items(ep, "a", boot=False))
        except FetchError as e:
            out.append(Check(f"endpoint:{ep.name}", ep.name, FAIL, str(e),
                             "check its address (anirss --endpoints) or your connection"))
            continue
        if n:
            out.append(Check(f"endpoint:{ep.name}", ep.name, OK,
                             f"answers ({n} results for a test search)"))
        else:
            out.append(Check(f"endpoint:{ep.name}", ep.name, WARN,
                             "answers, but a test search found nothing",
                             "try a real search; if that is empty too, check its address"))
    return out


def check_repos(ani, *, refresh: bool) -> list[Check]:
    from anirss_lib import repos as repos_mod
    try:
        repos = ani.repos(refresh=refresh)
    except AnirssError as e:
        return [Check("repos", "repos", FAIL, str(e))]
    if not repos:
        return []
    out: list[Check] = []
    offers = ani.repo_endpoints()
    for r in repos:
        updates = [o.definition.id for o in offers if o.repo == r.url and o.update]
        if r.error:
            out.append(Check(f"repo:{r.url}", r.name, WARN,
                             f"{r.error}; using the list it gave before",
                             "check the repo's address, or try again later"))
        elif updates:
            out.append(Check(f"repo:{r.url}", r.name, WARN,
                             f"updates for {', '.join(updates)}",
                             "anirss --update-endpoints"))
        else:
            out.append(Check(f"repo:{r.url}", r.name, OK, "verified, up to date"))
    for inst in repos_mod.orphaned(repos_mod.load_state(ani.repos_file)):
        out.append(Check(f"installed:{inst.name}", inst.name, WARN,
                         "its repo no longer offers it; it keeps working",
                         f"anirss --remove-endpoint {inst.name}"))
    return out


def check_downloads(ani) -> list[Check]:
    from anirss_lib.qbt.clients import download_root, local_files
    client = ani.clients()[0]
    if not local_files(client):
        return [Check("downloads", "downloads", SKIP,
                      f"files are on {client.get('name') or 'the qBittorrent machine'}")]
    out = []
    for key, label in (("save_base", "shows"), ("movie_path", "movies")):
        path = download_root(ani.config, client, key)
        if os.path.isdir(path):
            if os.access(path, os.W_OK):
                out.append(Check(f"downloads:{key}", label, OK, path))
            else:
                out.append(Check(f"downloads:{key}", label, FAIL,
                                 f"{path} is not writable", "fix its permissions"))
        else:
            out.append(Check(f"downloads:{key}", label, OK,
                             f"{path} (qBittorrent creates it on the first download)"))
    return out


def check_fzf() -> list[Check]:
    if shutil.which("fzf"):
        return [Check("fzf", "fzf", OK, "installed")]
    return [Check("fzf", "fzf", WARN, "not installed; the interactive search needs it "
                  "(--subscribe, --download N and --movie work without)", install_hint())]


def run(ani, *, search: bool = True, refresh: bool = True,
        fzf: bool = True) -> list[Check]:
    """Every check, in the order a setup is built: qBittorrent, endpoints,
    repos, folders, fzf. `search` sends one test search per endpoint and
    `refresh` fetches each repo again; both reach the network."""
    out: list[Check] = []
    for client in ani.clients():
        out.extend(check_qbt(ani, client))
    out.extend(check_endpoints(ani, search=search))
    out.extend(check_repos(ani, refresh=refresh))
    out.extend(check_downloads(ani))
    if fzf:
        out.extend(check_fzf())
    return out
