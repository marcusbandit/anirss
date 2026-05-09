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

After install, run `anirss --config` once to seed `~/.config/anirss/config.toml`, then edit it to point at your qBittorrent WebUI.
