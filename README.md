# anirss

A small CLI that searches [nyaa.si](https://nyaa.si) and hands the result to [qBittorrent](https://www.qbittorrent.org/) - either as a recurring RSS rule, a one-shot bulk download, or a single movie.

Built around an interactive [fzf](https://github.com/junegunn/fzf) picker that lets you narrow a noisy result set into something you'd actually want to subscribe to.

> Built with UNIX in mind - Linux and macOS. Windows users: WSL.

## Install

Requires Python 3.11+ and `fzf`.

### Arch Linux (AUR)

```sh
yay -S anirss          # stable release
yay -S anirss-git      # tracks main
```

(Or `paru -S …` - any AUR helper works.)

### macOS / Linux (Homebrew)

```sh
brew tap marcusbandit/anirss
brew install anirss
```

Bleeding-edge from `main`:

```sh
brew install --HEAD marcusbandit/anirss/anirss
```

### From source

```sh
git clone https://github.com/marcusbandit/anirss.git
cd anirss
./install.sh           # installs to ~/.local/bin
```

`./install.sh` is idempotent - re-run it any time to pull, refresh the binary, and append any new default config sections. (No separate update script.)

After the first install, edit `~/.config/anirss/config.toml` to point at your qBittorrent WebUI.

## Endpoints

anirss can search more than just nyaa.si. Configure one or more `[[endpoint]]` blocks in `~/.config/anirss/config.toml`, each naming a site to search. This is what ships in the default config:

```toml
[[endpoint]]
name = "nyaa"
kind = "nyaa"   # nyaa-style software: q/c/f params + seeders/size stats
url = "https://nyaa.si/"
category = "1_0"
filter = "0"

# kind = "rss" fits any site with an RSS search URL. Put {query} where the
# search terms go; extra fixed params are fine. Stats columns show only when
# the feed carries them. Uncomment to enable AniRena as a fallback:
#[[endpoint]]
#name = "anirena"
#kind = "rss"
#url = "https://www.anirena.com/rss?q={query}&adult=1"

# Erai-raws members-only tokenized feed also works as kind = "rss" (get your
# token from their RSS page). anirss recognizes erai feeds and cleans up the
# titles, and subscribing registers the show's own /anime-list/<slug>/feed/
# in qBittorrent (validated with one request; falls back to this search URL)
# with the query enforced via the rule's mustContain. Quality tokens (1080p,
# WEB-DL, AVC, …) are never sent to the search - Erai's `s=` only matches
# post text and would return nothing - they filter the results client-side
# using the feed's erai:resolution/infomkv/linktype metadata:
#[[endpoint]]
#name = "erai"
#kind = "rss"
#url = "https://www.erai-raws.info/episodes/feed/?res=1080p&type=torrent&subs%5B0%5D=us&s={query}&token=YOUR_TOKEN"
```

There are two kinds:

- `kind = "nyaa"`: nyaa-style software (nyaa.si and its clones), with `q`/`c`/`f` query params plus seeder/size stats columns.
- `kind = "rss"`: any site with an RSS search feed. Put `{query}` wherever the search terms belong in `url`; extra fixed params (like AniRena's `adult=1`) are fine. Stats columns (seeders, size) only show up when the feed actually carries them.

Endpoints are tried in priority order, the order they appear in the config; the first one is what anirss starts on. To start on a different one, pass `-e <name>` (also `--endpoint <name>` or `--endpoint=<name>`):

```sh
anirss -e anirena Frieren
```

While searching or refining, `Ctrl-E` switches endpoints on the fly: with two configured it just cycles between them, with three or more it opens a small picker.

On the initial search, if the active endpoint returns zero results or can't be reached, anirss automatically probes the other configured endpoints in priority order and switches to the first one with hits, e.g.:

```text
nyaa: 0 results, switched to anirena (27)
```

During refining (token picks, filters, excludes, Best Fit), zero-result fetches stay on the current endpoint and revert to the previous state; switch manually with Ctrl-E.

Subscribing to the same show from a second endpoint doesn't overwrite the existing qBittorrent rule/feed; the name gets suffixed with ` @<endpoint>` (e.g. `Frieren @anirena`) so both stick around side by side.

### Per-endpoint overrides and on-demand endpoints

Any `[[endpoint]]` can override `save_base`, `movie_path` and `tags`, so a TV tracker can land in its own folder with its own tag instead of the anime ones.

An endpoint backed by a local service, such as a Jackett torznab feed, can be **on-demand**: give it a `start` command, a `ready` URL and a `stop` command, and the service only runs while something needs it.

```toml
[[endpoint]]
name = "1337x"
kind = "rss"   # torznab feeds parse as rss (seeders, leechers, size, category)
url = "http://127.0.0.1:9117/api/v2.0/indexers/1337x/results/torznab/api?apikey=KEY&t=search&q={query}"
start = "systemd-run --user --scope --quiet podman pod start anirss-1337x"
stop = "podman pod stop anirss-1337x"
ready = "http://127.0.0.1:9117/api/v2.0/indexers/1337x/results/torznab/api?apikey=KEY&t=caps"
timeout = 60          # seconds per search (on-demand default 60, otherwise 10)
save_base = "~/Downloads/TV"
movie_path = "~/Downloads/Movies"
tags = ["TV"]
```

- Picking the endpoint (`-e`, `Ctrl-E`, or it being first) starts the service and prints `1337x: starting connection...` / `1337x: connected (7s)`. anirss stops it again on exit, unless someone else still uses it or it was already running before.
- Auto-fallback and the live preview never start an on-demand endpoint; they skip it while it is off.
- Downloads hand qBittorrent the magnet behind the service's download link, so nothing depends on the service afterwards.
- Subscriptions stay ordinary qBittorrent feeds + rules. `anirss --refresh-on-demand` starts each on-demand endpoint that has subscriptions, lets qBittorrent refresh those feeds and grab the matches, then stops it. `install.sh` installs `anirss-on-demand.timer`, a systemd user timer that runs it hourly.

`[search]` (the old single-endpoint config block) is now the deprecated legacy fallback: it only applies when no `[[endpoint]]` is defined at all. Existing configs that only have `[search]` keep working exactly as before.

## Tags

Everything anirss hands to qBittorrent is tagged `Anime`: one-off downloads, uploaded `.torrent` files, movies, and the RSS auto-download rules it creates, so a subscription keeps tagging every new episode it grabs. Change or extend the list in `~/.config/anirss/config.toml`:

```toml
[qbittorrent]
tags = ["Anime", "anirss"]
```

Set `tags = []` to tag nothing. Tagging RSS rules needs qBittorrent 4.6 or newer; on older builds the rules still work, just untagged.

## Several qBittorrents

One qBittorrent is the usual setup: `[qbittorrent] url` says where it is. To use more than one, a seedbox and the qbittorrent-nox on this machine say, name each in a `[[qbittorrent.client]]` block:

```toml
[qbittorrent]
username = "me"   # the default for every block; so is any other key here

[[qbittorrent.client]]
name = "seedbox"
url = "http://192.0.2.10:8080"

[[qbittorrent.client]]
name = "local"
url = "http://localhost:8080"
username = "admin"
tags = ["Anime", "local"]
```

The first block is the default. `-c <name>` (also `--client <name>` or `--client=<name>`) uses another one for that run, with every other flag:

```bash
anirss -c local Frieren 1080p    # search, then subscribe or download on the local one
anirss -c local -Q               # the local one's subscriptions
anirss -c seedbox -Rs Frieren    # remove from the seedbox
```

A seedbox saves to its own disk, so give its block the paths as they are on that machine; a block without them uses `[downloads]`:

```toml
[[qbittorrent.client]]
name = "seedbox"
url = "http://192.0.2.10:8080"
save_base = "/home/me/anime"       # on the seedbox; `~` is only allowed for a local client
movie_path = "/home/me/movies"
```

anirss only reads or deletes files itself for a client on this machine (a local url, or `local_files = true` for a NAS mounted at the same path). On any other, `-Rn` is refused and `-Rns` has qBittorrent delete the files, so a remove on the seedbox never touches a folder of the same name here.

Each client keeps its own session and saved password, so switching never logs the other one out: the first login to a client asks for its password (`qBittorrent password for local`) and offers to remember it. A config without `[[qbittorrent.client]]` blocks works exactly as before. The `-R` tab completion lists the subscriptions of whichever client last refreshed the cache (`-Sy`).

## Usage

```text
anirss                      Prompt for a search.
anirss [search query]       Search, refine, choose subscribe / download / movie.
anirss <nyaa-rss-url>       Extract the search query from the URL and run it.
anirss <magnet | .torrent>  Download a single torrent now.

anirss -S <url>             Subscribe to a URL: skip search/refine, go to action menu.
anirss -Sy                  Sync the feed cache from qBittorrent.

anirss -Q                   List subscribed feeds (cached).
anirss -Qj                  List subscribed feeds as JSON.

anirss -R  <name>...        Remove feed + rule.
anirss -Rs <name>...        + remove torrents (keep files).
anirss -Rn <name>...        + delete files (torrents stay; will error in qB).
anirss -Rns <name>...       + everything (clean uninstall).

anirss --noconfirm          Skip the y/N prompt for -R*.
anirss --no-seed            Set qBittorrent to pause torrents at ratio 0.
anirss --config             Print resolved config and exit.
anirss --migrate-config     Append any new default config sections.
anirss --refresh-on-demand  Let qBittorrent poll on-demand endpoint feeds.
anirss --version            Print version.
```

The `-R*` flags follow pacman-style modifier composition (`-n` deletes files, `-s` removes torrents from qB, both can be combined).

### Non-interactive use

For scripts, cron jobs, or anyone who already knows what they want, `anirss` can run without ever opening fzf or prompting:

```sh
# Subscribe straight from a search query
ANIRSS_QBT_PASSWORD=... anirss --subscribe Frieren

# Download the top 5 results by download count
ANIRSS_QBT_PASSWORD=... anirss --download 5 "Frieren 1080p" --name Frieren

# Download every match
ANIRSS_QBT_PASSWORD=... anirss --download-all "Frieren 1080p"

# Treat the top match as a movie (saves to movie_path)
ANIRSS_QBT_PASSWORD=... anirss --movie "Some Movie 2024"

# Pipe the password instead of using the env var
echo "$pw" | anirss --password-stdin --subscribe Frieren

# Subscribe to a prebuilt nyaa RSS URL without ever opening the action menu
ANIRSS_QBT_PASSWORD=... anirss --subscribe -S "https://nyaa.si/?page=rss&q=Frieren"
```

`--subscribe`, `--download-all`, `--download N`, and `--movie` are mutually exclusive. Any one of them flips `anirss` into non-interactive mode: no fzf pickers, no `Name:` prompt (use `--name`, or the name is derived from the top result), and no password retry loop. Use the cached SID, the `ANIRSS_QBT_PASSWORD` env var, or one line read from stdin via `--password-stdin`. A `--password PW` CLI flag is intentionally not provided - `ps aux` and shell history both leak it.

### Tab completion

Both packages (Brew, AUR) install a `zsh` completion at `…/zsh/site-functions/_anirss` and a `bash` completion at `…/bash-completion/completions/anirss`. From-source installs land them under `~/.local/share/`. Feed names auto-complete after `-R*` from the cache at `~/.local/state/anirss/feeds.txt`, refreshed on every successful qBittorrent login (or run `anirss -Sy` to force).

### Note: bare-URL behavior changed in v0.2.0

In v0.1.0, `anirss <nyaa-rss-url>` subscribed to the URL directly. In v0.2.0, it instead extracts the `q=` parameter and runs it as a fresh search - useful for "share a link, repeat the search" workflows. To subscribe explicitly, use `anirss -S <url>`. Pasting a non-nyaa URL will print an error pointing you at `-S`.

## Using anirss as a backend

anirss can be imported as a Python library or run as a JSON HTTP server for GUIs and other apps. The integration docs live in [docs/agents/](docs/agents/README.md). They are written mainly for coding agents implementing against anirss; if the project grows, human-facing docs will be hosted on a proper docs site instead.

```python
from anirss_lib.api import Anirss

ani = Anirss()                                  # reads ~/.config/anirss/config.toml
res = ani.search("Frieren 1080p")
ani.subscribe(res.query, endpoint=res.endpoint, items=res.items)
```

```sh
anirss --serve                                  # prints the bearer token on first start
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8787/api/v1/search?q=Frieren+1080p"
```
