"""anirss - search nyaa.si, then either subscribe (RSS rule) or download now in qBittorrent.

Usage:
    anirss                        Prompt for a search.
    anirss [search query]         Search, refine, choose subscribe/download/movie.
    anirss <nyaa-rss-url>         Extract `q=` from the URL and run as a fresh search.
    anirss <magnet|*.torrent>     Download a single torrent now.
                                  (magnet URI, remote *.torrent URL, or local *.torrent path)

    anirss -e <endpoint> [query]  Start on a specific endpoint (see [[endpoint]]
                                  in config; Ctrl-E switches inside the picker).
    anirss -c <client> ...        Use another qBittorrent for this run (see
                                  [[qbittorrent.client]] in config). Goes with
                                  every other flag: -c seedbox -Q, -c local --serve.

    anirss -S <url>               Subscribe: skip search/refine, go to action menu.
    anirss -Sy                    Sync the feed cache from qBittorrent.
    anirss -Sh <url>              Subscribe - save under [downloads].hidden_base.

    anirss -T <path.torrent>      Upload a local .torrent file directly.
                                  (tab-completes filesystem paths)
    anirss -Th <path.torrent>     Upload - save under [downloads].hidden_base.

    h modifier                    Add to -S, -T, or -R to use hidden_base
                                  (config: [downloads].hidden_base, default
                                  /srv/media/Hentai). E.g. -Sh, -Th, -Rsh.

    anirss -Q                     List subscribed feeds (cached).
    anirss -Qj                    List subscribed feeds as JSON.

    anirss -R <name>...           Remove feed + rule.
    anirss -Rs <name>...          + remove torrents (keep files).
    anirss -Rn <name>...          + delete files (torrents stay; will error in qB).
    anirss -Rns <name>...         + everything (clean uninstall).
    anirss --noconfirm            Skip the y/N prompt for -R*.

Non-interactive (skip fzf, skip Name prompt):
    --subscribe                   Subscribe to query/URL without showing the action menu.
    --download-all                Download every matching item.
    --download N                  Download top N items by download count.
    --movie                       Top-1 to movie_path.
    --name NAME                   Provide the subscription/download name.
    --password-stdin              Read qBittorrent password from stdin.

    Or set ANIRSS_QBT_PASSWORD in the environment.

Other:
    anirss --no-seed              Set qBittorrent to pause torrents at ratio 0.
    anirss --config               Print the resolved config and exit.
    anirss --migrate-config       Append any new default sections to your config.toml.
    anirss --refresh-on-demand    Start on-demand endpoints that have subscriptions,
                                  let qBittorrent poll them, stop them again.
                                  (run hourly by anirss-on-demand.timer)
    anirss --version              Print version and exit.

Backend (for frontends and other apps; see docs/agents/):
    anirss --serve [--host H] [--port P]
                                  Serve the JSON HTTP API (default 127.0.0.1:8787,
                                  config [server]). Bearer token: see docs/agents/.
    Python: `from anirss_lib.api import Anirss`.

Config: ~/.config/anirss/config.toml (auto-created on first run).
State:  ~/.local/state/anirss/   (feeds.txt cache, qbt.sid session cookie, anirss.log;
                                  a named client's session is qbt-<name>.sid)
"""

__version__ = "0.6.0"
