# Publishing Momento

A short checklist for releasing Momento. Packaging files live in `packaging/`.

## 1. Before the first release

- [ ] Run `make lint test` and try a real install on at least one distro.
- [ ] Bump `__version__` in `momento/__init__.py` and add a `<release>` entry to `packaging/flatpak/io.github.mehulchachada.Momento.metainfo.xml`.
- [ ] After tagging, replace the `0000…` sha256 placeholders with the value from the release's `SHA256SUMS`: `packaging/arch/PKGBUILD` (+ `.SRCINFO`) and the last module of `packaging/flatpak/io.github.mehulchachada.Momento.yml`.
- [ ] Before a Flathub submission, apply `packaging/flatpak/background-portal.patch` and then `flatpak-sandbox.patch` with `git apply`, and release that version. The Flatpak does not work properly without them.

## 2. Release

```sh
git tag -a v0.1.0 -m "Momento 0.1.0"
git push origin main v0.1.0
```

GitHub then builds the packages and attaches them to the release, with a `SHA256SUMS` file:

- **Fedora:** `sudo dnf install ./momento-0.1.0-1.noarch.rpm`
- **Ubuntu 25.04+ / Debian 13+:** `sudo apt install ./momento_0.1.0_all.deb`
- **Arch:** `sudo pacman -U momento-0.1.0-1-any.pkg.tar.zst`

After any package install, each user runs `systemctl --user enable --now momento.service` once.

## 3. Channels

### AUR (Arch)

No review. Users type `yay -S momento`. Files: `packaging/arch/`.

1. Make an account at aur.archlinux.org and add your SSH key.
2. `git clone ssh://aur@aur.archlinux.org/momento.git` and copy in `PKGBUILD`.
3. `updpkgsums && makepkg -si && makepkg --printsrcinfo > .SRCINFO`
4. Commit and push. Copy `PKGBUILD` and `.SRCINFO` back to `packaging/arch/`.

### Fedora COPR

No review. Users type `sudo dnf copr enable mehulchachada/momento && sudo dnf install momento`. File: `packaging/fedora/momento.spec`.

1. Make a Fedora account, then a `momento` project at copr.fedorainfracloud.org.
2. Bump `Version:` in the spec.
3. Build a source RPM (`spectool -g`, `rpmbuild -bs`) and run `copr-cli build momento momento-*.src.rpm`.
4. Or let COPR build from the GitHub repo on every tag (Packages → SCM, auto-rebuild).

### openSUSE Build Service

No review. Users add the `home:mehulchachada` repo with `zypper addrepo` and run `sudo zypper install momento`.

1. Make an account at build.opensuse.org.
2. Create a `momento` package and upload the release tarball and `packaging/fedora/momento.spec`, with openSUSE package names.
3. Commit with `osc` or the web page. It can also build for Fedora, Debian and Ubuntu.

### Ubuntu PPA

No review. Users type `sudo add-apt-repository ppa:mehulchachada/momento && sudo apt install momento`. Files: `packaging/debian/`. Ubuntu 24.04 is not possible (no PySide6).

1. Make a Launchpad account, upload your GPG key, create a `momento` PPA.
2. Build a signed source package with `debuild -S` from `packaging/debian/`.
3. `dput ppa:mehulchachada/momento` it, once per Ubuntu version.

### Flathub

Reviewed by people, takes days to weeks. The most important channel: it works everywhere, including SteamOS and Bazzite. Users type `flatpak install flathub io.github.mehulchachada.Momento`. Files: `packaging/flatpak/`.

1. Release a version that includes the two patches, and put its tarball and sha256 in the manifest.
2. Fork github.com/flathub/flathub, branch from `new-pr`, add the `.yml` manifest.
3. Open a pull request against `new-pr`, comment `bot, build`, answer the review.
4. Verify the app id by logging in to flathub.org with the `mehulchachada` GitHub account.

## Why no AppImage

Momento needs your system's own graphics drivers to record with the GPU.
An AppImage can't match those drivers reliably, and it can't start Momento at login.
Flatpak solves both, so it is the one-file-for-every-distro option.
