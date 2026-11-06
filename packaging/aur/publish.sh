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
    local src_dir="${2:-$HERE/$name}"

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

    # A VCS package's pkgver moves with every commit on main. Push it only when something
    # a user resolves against changed: the PKGBUILD beyond pkgver, or the provided version
    # (the tag), so subscribers are not notified for a bare pkgver bump.
    if [[ "$name" == *-git && -n "$(git -C "$repo" log --oneline 2>/dev/null)" ]] \
        && diff -q <(grep -v '^pkgver=' "$repo/PKGBUILD") <(grep -v '^pkgver=' "$src_dir/PKGBUILD") >/dev/null \
        && [[ "$(grep 'provides =' "$repo/.SRCINFO")" == "$(grep 'provides =' "$src_dir/.SRCINFO")" ]]; then
        echo "==> $name: only pkgver moved; nothing to push"
        git -C "$repo" checkout -q -- PKGBUILD .SRCINFO
        return 0
    fi

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

# anirss-git's provide carries its pkgver (anirss=0.8.0 out of 0.8.0.r3.gabc1234), and AUR
# helpers read it from .SRCINFO before they build anything, so the .SRCINFO has to name the
# tag main is at. `makepkg -od` clones the PKGBUILD's own source (main on GitHub) and runs its
# pkgver(), in a scratch copy, so neither this checkout's HEAD nor its tracked files count.
# Done before anything is pushed: a failure here leaves both packages as they were.
GIT_STAGE="$(mktemp -d -t anirss-git-stage-XXXX)"
trap 'cleanup; rm -rf "$GIT_STAGE"' EXIT
prepare_git() {
    cp "$HERE/anirss-git/PKGBUILD" "$GIT_STAGE/PKGBUILD"
    (cd "$GIT_STAGE" && makepkg -od --noprepare >/dev/null && makepkg --printsrcinfo > .SRCINFO.new) \
        || { echo "anirss-git: could not work out the version of main" >&2; exit 1; }
    mv "$GIT_STAGE/.SRCINFO.new" "$GIT_STAGE/.SRCINFO"
    echo "==> anirss-git: $(grep -m1 'pkgver =' "$GIT_STAGE/.SRCINFO" | xargs), $(grep -m1 'provides =' "$GIT_STAGE/.SRCINFO" | xargs)"
}

prepare_git
publish_one anirss
publish_one anirss-git "$GIT_STAGE"

echo
echo "Done. View at:"
echo "  https://aur.archlinux.org/packages/anirss"
echo "  https://aur.archlinux.org/packages/anirss-git"
