#!/usr/bin/env bash
# install.sh — install anirss into ~/.local/bin and seed a default config.
#
# Usage:
#     ./install.sh              # install to ~/.local/bin
#     PREFIX=/usr/local ./install.sh   # install to /usr/local/bin (may need sudo)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${PREFIX:-$HOME/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/anirss"

red()  { printf '\033[31m%s\033[0m' "$1"; }
yel()  { printf '\033[33m%s\033[0m' "$1"; }
grn()  { printf '\033[32m%s\033[0m' "$1"; }

err()  { echo "$(red "ERROR:") $1" >&2; exit 1; }
warn() { echo "$(yel "WARN:")  $1" >&2; }
ok()   { echo "$(grn "OK:")    $1"; }

# --- dependency checks ---
command -v python3 >/dev/null 2>&1 || err "python3 not found"

if ! python3 -c "import tomllib" 2>/dev/null; then
    err "Python 3.11+ required (need stdlib 'tomllib'). Found: $(python3 --version 2>&1)"
fi

if ! command -v fzf >/dev/null 2>&1; then
    warn "fzf not found — the interactive picker won't work. Install fzf for the full TUI."
fi

[ -f "$REPO_DIR/anirss" ] || err "anirss script not found at $REPO_DIR/anirss"

# --- install ---
mkdir -p "$BIN_DIR"
install -m 755 "$REPO_DIR/anirss" "$BIN_DIR/anirss"
ok "installed $BIN_DIR/anirss"

# --- PATH check ---
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR is not in your PATH. Add to your shell rc:"
        echo '         export PATH="$HOME/.local/bin:$PATH"'
        ;;
esac

# --- config bootstrap ---
mkdir -p "$CONFIG_DIR"
# Running anirss with --config triggers default-config creation when missing.
"$BIN_DIR/anirss" --config >/dev/null 2>&1 || true

if [ -f "$CONFIG_DIR/config.toml" ]; then
    ok "config at $CONFIG_DIR/config.toml"
    echo
    echo "Edit it to match your setup:"
    echo "  • [qbittorrent]   url, username  — your qBittorrent WebUI"
    echo "  • [downloads]     save_base      — where series/bulk downloads go"
    echo "  • [downloads]     movie_path     — where movies go (single files, no subdir)"
fi

echo
ok "done. Try: anirss --help"
