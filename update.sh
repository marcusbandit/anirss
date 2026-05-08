#!/usr/bin/env bash
# update.sh — pull latest, refresh the installed binary, migrate config.
#
# Safe to run repeatedly. Does NOT touch your existing config values; only
# appends new default sections via `anirss --migrate-config`.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${PREFIX:-$HOME/.local/bin}"
TARGET="$BIN_DIR/anirss"

red()  { printf '\033[31m%s\033[0m' "$1"; }
yel()  { printf '\033[33m%s\033[0m' "$1"; }
grn()  { printf '\033[32m%s\033[0m' "$1"; }

err()  { echo "$(red "ERROR:") $1" >&2; exit 1; }
warn() { echo "$(yel "WARN:")  $1" >&2; }
ok()   { echo "$(grn "OK:")    $1"; }

cd "$REPO_DIR"

if [ -d .git ]; then
    if ! git diff --quiet || ! git diff --cached --quiet; then
        warn "uncommitted changes in $REPO_DIR — skipping git pull"
    else
        git pull --ff-only || err "git pull failed; resolve and re-run"
        ok "pulled latest"
    fi
else
    warn "$REPO_DIR is not a git checkout — skipping git pull"
fi

[ -f "$REPO_DIR/anirss" ] || err "no anirss script at $REPO_DIR/anirss"

if [ -L "$TARGET" ] && [ "$(readlink -f "$TARGET" 2>/dev/null)" = "$REPO_DIR/anirss" ]; then
    ok "symlink at $TARGET — picks up changes automatically"
elif [ -e "$TARGET" ]; then
    install -m 755 "$REPO_DIR/anirss" "$TARGET"
    ok "refreshed $TARGET"
else
    err "no install at $TARGET — run ./install.sh first"
fi

# Append any new config sections (preserves all existing user values).
"$TARGET" --migrate-config

echo
ok "anirss is up to date"
