#!/usr/bin/env bash
# install.sh — install or update anirss. Safe to re-run.
#
# - First run:  installs deps reminder, drops the binary in ~/.local/bin,
#               seeds a default config at ~/.config/anirss/config.toml.
# - Re-run:     pulls latest (if a clean git checkout), refreshes the binary,
#               appends any new default config sections via --migrate-config.
#
# Usage:
#     ./install.sh                     # install/update to ~/.local/bin
#     PREFIX=/usr/local ./install.sh   # install to /usr/local/bin (may need sudo)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${PREFIX:-$HOME/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/anirss"
TARGET="$BIN_DIR/anirss"

red()  { printf '\033[31m%s\033[0m' "$1"; }
yel()  { printf '\033[33m%s\033[0m' "$1"; }
grn()  { printf '\033[32m%s\033[0m' "$1"; }

err()  { echo "$(red "ERROR:") $1" >&2; exit 1; }
warn() { echo "$(yel "WARN:")  $1" >&2; }
ok()   { echo "$(grn "OK:")    $1"; }

# --- pull latest, but only if this is a clean git checkout ---
if [ -d "$REPO_DIR/.git" ]; then
    if git -C "$REPO_DIR" diff --quiet && git -C "$REPO_DIR" diff --cached --quiet; then
        git -C "$REPO_DIR" pull --ff-only && ok "pulled latest"
    else
        warn "uncommitted changes in $REPO_DIR — skipping git pull"
    fi
fi

# --- dependency checks ---
command -v python3 >/dev/null 2>&1 || err "python3 not found"

if ! python3 -c "import tomllib" 2>/dev/null; then
    err "Python 3.11+ required (need stdlib 'tomllib'). Found: $(python3 --version 2>&1)"
fi

if ! command -v fzf >/dev/null 2>&1; then
    echo "fzf not found — required for the interactive search/refine picker."
    if   command -v apt-get >/dev/null 2>&1; then fzf_cmd="sudo apt-get install -y fzf"
    elif command -v pacman  >/dev/null 2>&1; then fzf_cmd="sudo pacman -S --noconfirm fzf"
    elif command -v dnf     >/dev/null 2>&1; then fzf_cmd="sudo dnf install -y fzf"
    elif command -v zypper  >/dev/null 2>&1; then fzf_cmd="sudo zypper install -y fzf"
    elif command -v brew    >/dev/null 2>&1; then fzf_cmd="brew install fzf"
    elif command -v apk     >/dev/null 2>&1; then fzf_cmd="sudo apk add fzf"
    else fzf_cmd=""
    fi
    if [ -n "$fzf_cmd" ]; then
        echo "Suggested: $fzf_cmd"
        read -r -p "Run it now? [y/N] " reply
        case "$reply" in
            [yY]|[yY][eE][sS]) eval "$fzf_cmd" || err "fzf install failed — re-run install.sh after fixing" ;;
            *) err "install fzf manually, then re-run install.sh" ;;
        esac
    else
        err "couldn't detect your package manager — install fzf manually, then re-run install.sh"
    fi
fi

[ -f "$REPO_DIR/anirss" ] || err "anirss script not found at $REPO_DIR/anirss"

# --- install or refresh the binary ---
mkdir -p "$BIN_DIR"
if [ -L "$TARGET" ] && [ "$(readlink -f "$TARGET" 2>/dev/null)" = "$REPO_DIR/anirss" ]; then
    ok "symlink at $TARGET — picks up changes automatically"
elif [ -e "$TARGET" ]; then
    install -m 755 "$REPO_DIR/anirss" "$TARGET"
    ok "refreshed $TARGET"
else
    install -m 755 "$REPO_DIR/anirss" "$TARGET"
    ok "installed $TARGET"
fi

# --- PATH check ---
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR is not in your PATH. Add to your shell rc:"
        echo '         export PATH="$HOME/.local/bin:$PATH"'
        ;;
esac

# --- shell completions (best-effort, user-level) ---
# zsh: pick a directory that's already on $fpath whenever possible so completions
# work without the user editing their .zshrc. Order of preference:
#   1. $ZSH_CUSTOM/completions (Oh My Zsh users)
#   2. ~/.oh-my-zsh/custom/completions (Oh My Zsh default)
#   3. ~/.zfunc (a common manual setup; on fpath if user wired it up)
#   4. $XDG_DATA_HOME/zsh/site-functions (fallback; needs fpath edit)
needs_fpath_edit=""
if [ -f "$REPO_DIR/completions/_anirss" ]; then
    if [ -n "${ZSH_CUSTOM:-}" ] && [ -d "$ZSH_CUSTOM" ]; then
        zsh_dir="$ZSH_CUSTOM/completions"
    elif [ -d "$HOME/.oh-my-zsh/custom" ]; then
        zsh_dir="$HOME/.oh-my-zsh/custom/completions"
    elif [ -d "$HOME/.zfunc" ]; then
        zsh_dir="$HOME/.zfunc"
    else
        zsh_dir="${XDG_DATA_HOME:-$HOME/.local/share}/zsh/site-functions"
        needs_fpath_edit=1
    fi
    mkdir -p "$zsh_dir"
    install -m 644 "$REPO_DIR/completions/_anirss" "$zsh_dir/_anirss"
    ok "zsh completion -> $zsh_dir/_anirss"
    if [ -n "$needs_fpath_edit" ]; then
        warn "$zsh_dir is not on a default \$fpath. Add to ~/.zshrc (before 'compinit'):"
        echo "         fpath=(\"$zsh_dir\" \$fpath)"
    fi
    # zsh caches completion lookups in .zcompdump and won't re-scan fpath until
    # the dump is stale or removed. Wipe it so the new completion is picked up
    # on next shell start. compinit will rebuild it automatically.
    if ls "$HOME"/.zcompdump* >/dev/null 2>&1; then
        rm -f "$HOME"/.zcompdump* 2>/dev/null || true
        ok "wiped stale ~/.zcompdump* (zsh will rebuild on next start)"
    fi
    echo "         start a new shell or run 'exec zsh' to use it"
fi
if [ -f "$REPO_DIR/completions/anirss.bash" ]; then
    bash_dir="${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions"
    mkdir -p "$bash_dir"
    install -m 644 "$REPO_DIR/completions/anirss.bash" "$bash_dir/anirss"
    ok "bash completion -> $bash_dir/anirss"
fi

# --- config: bootstrap on first install, migrate on updates ---
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    # Running anirss with --config triggers default-config creation when missing.
    "$TARGET" --config >/dev/null 2>&1 || true
    if [ -f "$CONFIG_DIR/config.toml" ]; then
        ok "config at $CONFIG_DIR/config.toml"
        echo
        echo "Edit it to match your setup:"
        echo "  • [qbittorrent]   url, username  — your qBittorrent WebUI"
        echo "  • [downloads]     save_base      — where series/bulk downloads go"
        echo "  • [downloads]     movie_path     — where movies go (single files, no subdir)"
    fi
else
    "$TARGET" --migrate-config
fi

echo
ok "done. Try: anirss --help"
