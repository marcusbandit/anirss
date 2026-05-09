#!/usr/bin/env bash
# packaging/aur/publish.sh — push anirss + anirss-git to the AUR.
#
# Prerequisites (one-time):
#   1. AUR account at https://aur.archlinux.org/
#   2. Public key from ~/.ssh/id_ed25519.pub uploaded to your AUR profile
#      (https://aur.archlinux.org/account/) so SSH auth works.
#
# This script multiplexes SSH connections so it asks for your key passphrase
# at most once for the entire run.
#
# Run from repo root or anywhere — paths are resolved against this script.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK_DIR:-$HOME/.cache/anirss-aur}"
mkdir -p "$WORK"

# --- SSH connection multiplexing --------------------------------------------
# Coalesce every SSH op the script runs into a single connection. The first
# connection prompts for the key passphrase; subsequent ones (the second push,
# any clones, etc.) reuse the master without prompting.
SSH_CTRL_DIR="$(mktemp -d -t anirss-aur-XXXX)"
SSH_CTRL_PATH="$SSH_CTRL_DIR/cm-%C"
export GIT_SSH_COMMAND="ssh -o ControlMaster=auto -o ControlPath=$SSH_CTRL_PATH -o ControlPersist=30"

cleanup() {
    # Best-effort: ask the master to exit cleanly, then drop the temp dir.
    ssh -O exit -o "ControlPath=$SSH_CTRL_PATH" -o "ControlMaster=no" \
        aur@aur.archlinux.org >/dev/null 2>&1 || true
    rm -rf "$SSH_CTRL_DIR" 2>/dev/null || true
}
trap cleanup EXIT

# --- per-package publish ----------------------------------------------------
publish_one() {
    local name="$1"
    local src_dir="$HERE/$name"

    [[ -f "$src_dir/PKGBUILD" ]] || { echo "missing $src_dir/PKGBUILD" >&2; exit 1; }
    [[ -f "$src_dir/.SRCINFO" ]] || { echo "missing $src_dir/.SRCINFO (run 'makepkg --printsrcinfo > .SRCINFO' in $src_dir)" >&2; exit 1; }

    local repo="$WORK/$name"
    if [[ ! -d "$repo/.git" ]]; then
        echo "==> $name: cloning ssh://aur@aur.archlinux.org/$name.git into $repo"
        rm -rf "$repo"
        # Clone with master as the local branch so push paths line up with the
        # AUR remote (which uses master, not main).
        git clone -b master "ssh://aur@aur.archlinux.org/$name.git" "$repo" 2>/dev/null \
            || git clone "ssh://aur@aur.archlinux.org/$name.git" "$repo"
    fi
    # No git pull: AUR is solo-maintained; this script is the only writer.

    cp "$src_dir/PKGBUILD" "$repo/PKGBUILD"
    cp "$src_dir/.SRCINFO" "$repo/.SRCINFO"

    local pkgver
    pkgver="$(awk -F= '/^pkgver=/{print $2}' "$src_dir/PKGBUILD")"

    if git -C "$repo" diff --quiet && git -C "$repo" diff --cached --quiet; then
        if [[ -z "$(git -C "$repo" log --oneline 2>/dev/null)" ]]; then
            # Brand-new AUR repo: nothing committed yet, ship the initial state.
            git -C "$repo" add PKGBUILD .SRCINFO
            git -C "$repo" commit -m "$name $pkgver initial"
        else
            echo "==> $name: no changes to push"
            return 0
        fi
    else
        git -C "$repo" add PKGBUILD .SRCINFO
        git -C "$repo" commit -m "Update $name $pkgver"
    fi

    git -C "$repo" push origin HEAD:master
    echo "==> $name: pushed"
}

publish_one anirss
publish_one anirss-git

echo
echo "Done. View at:"
echo "  https://aur.archlinux.org/packages/anirss"
echo "  https://aur.archlinux.org/packages/anirss-git"
