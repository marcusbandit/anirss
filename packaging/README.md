# Packaging anirss

Files in this directory let `anirss` be installed via Homebrew (Brew) and the
Arch User Repository (AUR). Both are noarch — there is nothing to compile,
just a single Python script and its runtime deps (`python>=3.11`, `fzf`).

```
packaging/
├── brew/
│   └── anirss.rb              # Homebrew formula
└── aur/
    ├── anirss/PKGBUILD        # stable, pinned to v0.1.0 tag
    └── anirss-git/PKGBUILD    # tracks main
```

Before either channel can be published you need a real release tag. Everything
below assumes the source repo is `https://github.com/marcusbandit/anirss`.

---

## 1. Cut the v0.1.0 release

```sh
# from the repo root
git tag -a v0.1.0 -m "anirss 0.1.0"
git push origin v0.1.0
```

GitHub will auto-generate the source tarball at
`https://github.com/marcusbandit/anirss/archive/refs/tags/v0.1.0.tar.gz`.

Compute its SHA256:

```sh
curl -sL https://github.com/marcusbandit/anirss/archive/refs/tags/v0.1.0.tar.gz \
    | sha256sum
```

Use that hash to replace the `REPLACE_ME_AFTER_PUSHING_TAG` placeholder in:

- `packaging/brew/anirss.rb` — `sha256 "..."`
- `packaging/aur/anirss/PKGBUILD` — `sha256sums=('...')`

(`anirss-git/PKGBUILD` uses `'SKIP'` and never needs updating.)

---

## 2. Homebrew

A formula that isn't in `homebrew-core` lives in a *tap* — a separate
repo named `homebrew-<tap>` that Homebrew can add and search.

### One-time tap setup

Create a new public GitHub repo named exactly `homebrew-anirss`
(under your `marcusbandit` user), then:

```sh
git clone https://github.com/marcusbandit/homebrew-anirss.git
cd homebrew-anirss
mkdir -p Formula
cp /path/to/anirss/packaging/brew/anirss.rb Formula/anirss.rb
git add Formula/anirss.rb
git commit -m "anirss 0.1.0"
git push
```

### Test it locally before publishing

```sh
# audit (style + dependency checks)
brew audit --new --strict --online ./Formula/anirss.rb

# build & install from the local file
brew install --build-from-source ./Formula/anirss.rb
brew test ./Formula/anirss.rb
```

`brew audit --new` is the strict pass new formulas need to clear. Fix any
warning it surfaces (license SPDX mismatches, desc length, etc.) before
pushing the tap.

### Users install with

```sh
brew tap marcusbandit/anirss
brew install anirss
# or in one go:
brew install marcusbandit/anirss/anirss

# bleeding edge (uses the head: line in the formula):
brew install --HEAD marcusbandit/anirss/anirss
```

### Updating the formula on a new release

For each new tag:

1. Bump `url` to the new tag tarball.
2. Recompute and replace `sha256`.
3. Commit to the tap repo.

Users get the update via `brew update && brew upgrade anirss`.

### Submitting to homebrew-core (optional, later)

Once the project has some traction (the official cutoff is roughly
75 GitHub stars / forks / watchers, plus 30 days of stability), you can
open a PR against `Homebrew/homebrew-core` adding `Formula/a/anirss.rb`.
Until then, the personal tap is the supported route.

---

## 3. AUR

The AUR doesn't host content — each "package" is a separate git repo on
`aur.archlinux.org` whose only required file is a `PKGBUILD`.

### One-time AUR account setup

1. Create an account at <https://aur.archlinux.org/register>.
2. Add an SSH public key to your AUR profile.
3. Make sure `~/.ssh/config` knows about it:

   ```
   Host aur.archlinux.org
       IdentityFile ~/.ssh/aur
       User aur
   ```

### Publish `anirss` (stable)

```sh
# clone the empty repo the AUR creates the first time you push
git clone ssh://aur@aur.archlinux.org/anirss.git aur-anirss
cd aur-anirss

cp /path/to/anirss/packaging/aur/anirss/PKGBUILD .

# verify the build works end-to-end (creates anirss-0.1.0-1-any.pkg.tar.zst)
makepkg -f
# install the built package locally to smoke-test
sudo pacman -U anirss-0.1.0-1-any.pkg.tar.zst

# regenerate .SRCINFO every time you change PKGBUILD — AUR requires it
makepkg --printsrcinfo > .SRCINFO

git add PKGBUILD .SRCINFO
git commit -m "anirss 0.1.0"
git push origin master    # AUR uses 'master', not 'main'
```

### Publish `anirss-git` (tracks main)

```sh
git clone ssh://aur@aur.archlinux.org/anirss-git.git aur-anirss-git
cd aur-anirss-git

cp /path/to/anirss/packaging/aur/anirss-git/PKGBUILD .

# pkgver() reads the live repo to compute the version, so let makepkg fill it
makepkg -od                 # downloads sources + runs pkgver()
makepkg --printsrcinfo > .SRCINFO

git add PKGBUILD .SRCINFO
git commit -m "anirss-git initial"
git push origin master
```

### Users install with

```sh
# stable
yay -S anirss              # or: paru -S anirss

# git
yay -S anirss-git
```

### Updating on a new release

- `anirss`: bump `pkgver`, reset `pkgrel=1`, refresh `sha256sums`,
  regenerate `.SRCINFO`, commit, push.
- `anirss-git`: nothing to do — `pkgver()` recomputes on every build.
  Only push if you change `depends`, `package()`, etc.

### Linting

If `namcap` is installed:

```sh
namcap PKGBUILD
namcap anirss-0.1.0-1-any.pkg.tar.zst
```

Clean any warnings before pushing.
