#!/usr/bin/env bash
# packaging/aur/publish.sh — push anirss + anirss-git to the AUR.
#
# Prerequisites (one-time):
#   1. AUR account at https://aur.archlinux.org/
#   2. Public key from ~/.ssh/id_ed25519.pub uploaded to your AUR profile
#      (https://aur.archlinux.org/account/) so SSH auth works.
#
# Run from repo root or anywhere — paths are resolved against this script.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK_DIR:-$HOME/.cache/anirss-aur}"
mkdir -p "$WORK"

publish_one() {
    local name="$1"          # anirss | anirss-git
    local src_dir="$HERE/$name"

    [[ -f "$src_dir/PKGBUILD" ]] || { echo "missing $src_dir/PKGBUILD" >&2; exit 1; }
    [[ -f "$src_dir/.SRCINFO" ]] || { echo "missing $src_dir/.SRCINFO (run 'makepkg --printsrcinfo > .SRCINFO' in $src_dir)" >&2; exit 1; }

    local repo="$WORK/$name"
    if [[ -d "$repo/.git" ]]; then
        echo "==> $name: pulling existing AUR clone in $repo"
        git -C "$repo" pull --ff-only || true
    else
        echo "==> $name: cloning ssh://aur@aur.archlinux.org/$name.git into $repo"
        rm -rf "$repo"
        git clone "ssh://aur@aur.archlinux.org/$name.git" "$repo"
    fi

    cp "$src_dir/PKGBUILD" "$repo/PKGBUILD"
    cp "$src_dir/.SRCINFO" "$repo/.SRCINFO"

    if git -C "$repo" diff --quiet && git -C "$repo" diff --cached --quiet; then
        # New AUR repo (no commits yet) still needs an initial commit.
        if [[ -z "$(git -C "$repo" log --oneline 2>/dev/null)" ]]; then
            git -C "$repo" add PKGBUILD .SRCINFO
            git -C "$repo" commit -m "$name $(awk -F= '/^pkgver=/{print $2}' "$src_dir/PKGBUILD") initial"
        else
            echo "    no changes to push"
            return 0
        fi
    else
        git -C "$repo" add PKGBUILD .SRCINFO
        git -C "$repo" commit -m "Update $name $(awk -F= '/^pkgver=/{print $2}' "$src_dir/PKGBUILD")"
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
