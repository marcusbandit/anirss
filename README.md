# anirss

A small CLI that searches [nyaa.si](https://nyaa.si) and hands the result to [qBittorrent](https://www.qbittorrent.org/) — either as a recurring RSS rule, a one-shot bulk download, or a single movie.

Built around an interactive [fzf](https://github.com/junegunn/fzf) picker that lets you narrow a noisy result set into something you'd actually want to subscribe to.

> Built with UNIX in mind — Linux and macOS. Windows users: WSL.

## Install

Requires Python 3.11+ and `fzf`.

### Arch Linux (AUR)

```sh
yay -S anirss          # stable release
yay -S anirss-git      # tracks main
```

(Or `paru -S …` — any AUR helper works.)

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

`./install.sh` is idempotent — re-run it any time to pull, refresh the binary, and append any new default config sections. (No separate update script.)

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

`[search]` (the old single-endpoint config block) is now the deprecated legacy fallback: it only applies when no `[[endpoint]]` is defined at all. Existing configs that only have `[search]` keep working exactly as before.

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

`--subscribe`, `--download-all`, `--download N`, and `--movie` are mutually exclusive. Any one of them flips `anirss` into non-interactive mode: no fzf pickers, no `Name:` prompt (use `--name`, or the name is derived from the top result), and no password retry loop. Use the cached SID, the `ANIRSS_QBT_PASSWORD` env var, or one line read from stdin via `--password-stdin`. A `--password PW` CLI flag is intentionally not provided — `ps aux` and shell history both leak it.

### Tab completion

Both packages (Brew, AUR) install a `zsh` completion at `…/zsh/site-functions/_anirss` and a `bash` completion at `…/bash-completion/completions/anirss`. From-source installs land them under `~/.local/share/`. Feed names auto-complete after `-R*` from the cache at `~/.local/state/anirss/feeds.txt`, refreshed on every successful qBittorrent login (or run `anirss -Sy` to force).

### Note: bare-URL behavior changed in v0.2.0

In v0.1.0, `anirss <nyaa-rss-url>` subscribed to the URL directly. In v0.2.0, it instead extracts the `q=` parameter and runs it as a fresh search — useful for "share a link, repeat the search" workflows. To subscribe explicitly, use `anirss -S <url>`. Pasting a non-nyaa URL will print an error pointing you at `-S`.
