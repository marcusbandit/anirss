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

### Tab completion

Both packages (Brew, AUR) install a `zsh` completion at `…/zsh/site-functions/_anirss` and a `bash` completion at `…/bash-completion/completions/anirss`. From-source installs land them under `~/.local/share/`. Feed names auto-complete after `-R*` from the cache at `~/.local/state/anirss/feeds.txt`, refreshed on every successful qBittorrent login (or run `anirss -Sy` to force).

### Note: bare-URL behavior changed in v0.2.0

In v0.1.0, `anirss <nyaa-rss-url>` subscribed to the URL directly. In v0.2.0, it instead extracts the `q=` parameter and runs it as a fresh search — useful for "share a link, repeat the search" workflows. To subscribe explicitly, use `anirss -S <url>`. Pasting a non-nyaa URL will print an error pointing you at `-S`.
