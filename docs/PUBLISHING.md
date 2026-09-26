# Publishing Momento

How Momento gets from this repository to people's computers: which channels
exist, which to do first, and the exact steps for each. Everything the steps
refer to lives in `packaging/` and `.github/workflows/release.yml`.

## The channels at a glance

| Channel | Who can install it | Review before it goes live? | What users type |
|---|---|---|---|
| **GitHub Releases** (.deb/.rpm/.pkg) | Fedora, Ubuntu 25.04+, Debian 13+, Arch family | No, live when the tag is pushed | `sudo dnf install ./momento-0.1.0-1.noarch.rpm` |
| **AUR** | Arch, CachyOS, EndeavourOS, Manjaro | No | `yay -S momento` or `paru -S momento` |
| **Fedora COPR** | Fedora 41+ (and EPEL-free RHEL clones) | No (self-service) | `sudo dnf copr enable mehulchachada/momento && sudo dnf install momento` |
| **openSUSE Build Service (OBS)** | openSUSE, and it can also build .deb/.rpm for Debian, Ubuntu, Fedora | No (self-service) | `sudo zypper addrepo …home:mehulchachada.repo && sudo zypper install momento` |
| **Ubuntu PPA** (Launchpad) | Ubuntu 25.04+ | No (self-service) | `sudo add-apt-repository ppa:mehulchachada/momento && sudo apt install momento` |
| **Flathub** | Every distro, including SteamOS, Bazzite, Silverblue, Ubuntu 24.04 | **Yes**, a human review (days to weeks) | `flatpak install flathub io.github.mehulchachada.Momento` |
| Official Fedora / Debian / Arch repos | That distro | Yes, and you need a sponsor or trusted user | `sudo dnf install momento`, etc. |

**Recommended order.** Ship the channels that go live immediately first:
GitHub Releases → AUR → COPR. Then submit to Flathub, which is the most
important long-term channel: one package that works on every distro,
including the immutable ones (SteamOS, Bazzite, Silverblue) where Momento's
target users are and where installing system packages is awkward or
impossible. OBS and a PPA are optional extras. Official distro repositories
come last, once the app is stable, because each needs a sponsor and months of
back-and-forth.

**Ubuntu 24.04 LTS** has no PySide6 packages, so the .deb/PPA cannot target
it. Point 24.04 users to the Flatpak.

## 1. Cutting a release (needed by every channel)

1. Bump `__version__` in `momento/__init__.py` (e.g. `0.1.1`), and add a
   `<release version="0.1.1" date="YYYY-MM-DD">` entry at the top of
   `<releases>` in `packaging/flatpak/io.github.mehulchachada.Momento.metainfo.xml`
   (Flathub shows it as the changelog; newest first).
2. Commit, then tag and push the tag:
   ```sh
   git tag -a v0.1.1 -m "Momento 0.1.1"
   git push origin main v0.1.1
   ```
3. `.github/workflows/release.yml` runs on the tag. It:
   - refuses to continue if the tag and `__version__` differ, or if
     `packaging/systemd/momento.service` drifted from `data/momento.service`
     (the only allowed difference is `ExecStart=/usr/bin/momento daemon`);
   - runs the core unit tests and validates the desktop file and metainfo;
   - builds `momento_<ver>_all.deb`, `momento-<ver>-1.noarch.rpm` and
     `momento-<ver>-1-any.pkg.tar.zst` with [nfpm](https://nfpm.goreleaser.com/)
     from `packaging/nfpm.yaml`;
   - makes `momento-<ver>.tar.gz` with `git archive` and a `SHA256SUMS` file;
   - installs each package in a clean Debian 13, Ubuntu 26.04, Fedora and Arch
     container (dependencies come from those distros' own repositories) and
     runs `momento --version` and `momento settings`;
   - creates the GitHub Release with all of the above attached, install
     instructions, and auto-generated notes.
4. Copy the tarball's checksum from `SHA256SUMS`. AUR, COPR, OBS and Flathub
   all download **that** tarball
   (`https://github.com/mehulchachada/momento/releases/download/v<ver>/momento-<ver>.tar.gz`),
   because its checksum never changes. GitHub's automatic "Source code"
   archives are generated on the fly and are not guaranteed to stay
   byte-identical.

Build the packages locally the same way (nothing is installed on your system):

```sh
VERSION=0.1.0 nfpm package -f packaging/nfpm.yaml -p deb       -t dist/
VERSION=0.1.0 nfpm package -f packaging/nfpm.yaml -p rpm       -t dist/
VERSION=0.1.0 nfpm package -f packaging/nfpm.yaml -p archlinux -t dist/
```

### What the release packages contain

The nfpm packages are `noarch` and independent of the Python minor version:
the code goes to `/usr/share/momento/momento/`, and `/usr/bin/momento` is a
small wrapper (`packaging/nfpm/momento.sh`) that runs
`/usr/bin/python3 -m momento` with that directory on `PYTHONPATH`. This also
avoids any clash with the unrelated `momento` SDK on PyPI, whose import name is
also `momento`. The AUR, COPR and Debian packages instead install into
`site-packages` the normal distro way.

All packages ship the systemd **user** unit to
`/usr/lib/systemd/user/momento.service` but never enable it; each user runs
`systemctl --user enable --now momento.service` once.

User-facing commands for the release assets:

```sh
# Fedora 41+
sudo dnf install ./momento-0.1.0-1.noarch.rpm
# Ubuntu 25.04+ / 26.04 LTS, Debian 13+
sudo apt install ./momento_0.1.0_all.deb
# Arch family
sudo pacman -U momento-0.1.0-1-any.pkg.tar.zst
sudo pacman -S --needed gst-plugin-va layer-shell-qt     # recommended extras
# then, as your normal user
systemctl --user enable --now momento.service
```

GPU drivers are not dependencies because they depend on your hardware:
- **Fedora + AMD:** Fedora's Mesa has H.264 encoding switched off. Install
  `mesa-va-drivers-freeworld` from RPM Fusion.
- **Fedora/Ubuntu + Intel:** `intel-media-driver` / `intel-media-va-driver-non-free`.
- **NVIDIA:** the proprietary driver (NVENC). On Fedora, also
  `gstreamer1-plugins-bad-freeworld` from RPM Fusion.

## 2. AUR (Arch User Repository)

No review; the package is live the moment you push. The name `momento` is
free on the AUR and in the official Arch repositories (checked 2026-09-26).

One-time setup:
1. Create an account at <https://aur.archlinux.org/register>.
2. Add your SSH **public** key under *My Account → SSH Public Key*
   (`ssh-keygen -t ed25519 -f ~/.ssh/aur`, then paste `~/.ssh/aur.pub`).
3. Add to `~/.ssh/config`:
   ```
   Host aur.archlinux.org
     IdentityFile ~/.ssh/aur
     User aur
   ```

First upload and every update (on Arch, or in an Arch distrobox/container):

```sh
git clone ssh://aur@aur.archlinux.org/momento.git aur-momento   # empty repo the first time
cp packaging/arch/PKGBUILD aur-momento/
cd aur-momento
# set pkgver=, reset pkgrel=1, then:
updpkgsums                              # fills sha256sums from the release tarball
makepkg -si                             # build + install to test it
namcap PKGBUILD *.pkg.tar.zst           # lint
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD .SRCINFO
git commit -m "Update to 0.1.0"
git push
```

Copy the updated `PKGBUILD` and `.SRCINFO` back into `packaging/arch/` so the
repository stays the source of truth. Users install with `yay -S momento` or
`paru -S momento`.

## 3. Fedora COPR

Self-service build service run by Fedora. No review.

One-time setup:
1. Create a Fedora account (FAS) at <https://accounts.fedoraproject.org>.
2. Log in at <https://copr.fedorainfracloud.org>, create a project named
   `momento`, and tick the chroots `fedora-43-x86_64`,
   `fedora-44-x86_64`, `fedora-rawhide-x86_64` (add `aarch64` too; the package
   is noarch). Enable "Follow Fedora branching".
3. Install the CLI and put your API token (from *API* in the COPR web UI) in
   `~/.config/copr`:
   ```sh
   sudo dnf install copr-cli rpm-build rpmdevtools   # or in a Fedora toolbox/distrobox
   ```

Each release:

```sh
cd packaging/fedora
# bump Version:, reset Release: 1, add a %changelog entry
spectool -g -C . momento.spec                      # downloads momento-<ver>.tar.gz
rpmbuild -bs momento.spec --define "_sourcedir $PWD" --define "_srcrpmdir $PWD"
copr-cli build momento momento-*.src.rpm
```

Alternative, fully automatic: in the COPR project choose *Packages → New
package → SCM*, clone URL `https://github.com/mehulchachada/momento`,
subdirectory `packaging/fedora`, spec file `momento.spec`, build method
`rpkg`, and tick "Auto-rebuild". Then add COPR's webhook URL (*Settings →
Integrations*) to the GitHub repository's webhooks for "Tag" events.

Users install with:

```sh
sudo dnf copr enable mehulchachada/momento
sudo dnf install momento
systemctl --user enable --now momento.service
```

On Bazzite/Silverblue this would mean `rpm-ostree` layering, which slows every
system update: point those users to the Flatpak instead.

## 4. openSUSE Build Service (OBS) — documented route

OBS (<https://build.opensuse.org>) builds from one project for openSUSE
Tumbleweed/Leap and, if you want, also Fedora, Debian and Ubuntu, and hosts
the repositories. It is self-service.

1. Create an account; you get a `home:mehulchachada` project.
2. Create a package `momento` and upload `momento-<ver>.tar.gz` and a spec.
   `packaging/fedora/momento.spec` works for the Fedora targets unchanged. For
   openSUSE the dependency names differ, so wrap them in
   `%if 0%{?suse_version}` blocks: `python3-gobject`, `python3-dbus-python`,
   `python3-pyside6`, `typelib-1_0-Gst-1_0`, `gstreamer-plugin-pipewire`,
   `gstreamer-plugins-good`, `gstreamer-plugins-bad`, `layer-shell-qt6`, and
   `%{_userunitdir}` is the same. The AAC encoder (`gstreamer-plugins-libav`)
   and H.264 VA encoding come from **Packman**, which OBS cannot depend on:
   make them `Recommends:` and tell users to add Packman.
3. For Debian/Ubuntu targets on OBS, upload `momento_<ver>.orig.tar.gz`, a
   `.dsc`, and `debian.tar.xz` made from `packaging/debian/` (OBS's
   "debtransform" builds from those).
4. Use the `osc` CLI (`osc checkout home:mehulchachada momento`, `osc add`,
   `osc commit`) or the web UI; add a `_service` file with `download_url` to
   fetch the tarball automatically.

Users on Tumbleweed:

```sh
sudo zypper addrepo https://download.opensuse.org/repositories/home:mehulchachada/openSUSE_Tumbleweed/home:mehulchachada.repo
sudo zypper install momento
```

## 5. Ubuntu PPA (Launchpad) — documented route

A PPA is a personal apt repository built by Launchpad. It only accepts
**source** packages, signed with your GPG key, and builds one upload per
Ubuntu series (e.g. `plucky`, `questing`, `resolute`). Ubuntu 24.04 (`noble`)
cannot be targeted (no PySide6).

1. Create a Launchpad account, upload your GPG key and sign the Ubuntu Code
   of Conduct, then create a PPA named `momento`.
2. In an Ubuntu container/distrobox with `devscripts debhelper dh-python
   pybuild-plugin-pyproject`:
   ```sh
   tar xf momento-0.1.0.tar.gz && cp momento-0.1.0.tar.gz momento_0.1.0.orig.tar.gz
   cd momento-0.1.0 && cp -r packaging/debian debian
   dch -v 0.1.0-1~ppa1~resolute1 -D resolute "PPA build for Ubuntu 26.04"
   debuild -S -sa -k<YOUR_GPG_KEY_ID>
   dput ppa:mehulchachada/momento ../momento_0.1.0-1~ppa1~resolute1_source.changes
   ```
   Repeat the `dch`/`debuild`/`dput` step for each series with its own suffix.
3. `packaging/debian/` builds with `dpkg-buildpackage` on Debian 13; the same
   directory can later be the starting point for an official Debian package.

Users install with:

```sh
sudo add-apt-repository ppa:mehulchachada/momento
sudo apt install momento
systemctl --user enable --now momento.service
```

## 6. Flathub (reviewed; the recommended primary channel)

### What is ready

- `packaging/flatpak/io.github.mehulchachada.Momento.yml` — the manifest.
  Runtime `org.kde.Platform` 6.11 with base app `io.qt.PySide.BaseApp` 6.11
  (PySide6 built against the runtime's Qt, so the layer-shell Qt plugin can
  load). It builds `layer-shell-qt`, `PyGObject` and `dbus-python` from
  checksummed tarballs and installs Momento from the release tarball.
- `packaging/flatpak/io.github.mehulchachada.Momento.metainfo.xml` — AppStream
  metadata (passes `appstreamcli validate`).
- Everything else Momento needs is already in the runtime: GStreamer 1.26
  (pipewiresrc, pulsesrc, splitmuxsink, mpegtsmux, h264parse, `va`,
  nvcodec, gst-libav), ffmpeg/ffprobe, pactl, pw-dump, xdg-user-dir.
  H.264 via VA-API and AAC work because Flatpak automatically installs the
  runtime extensions `org.freedesktop.Platform.GL.default//25.08-extra` (Mesa
  with the H.264 encoder enabled) and `org.freedesktop.Platform.codecs-extra`
  (x264, full libavcodec). There is no `ffmpeg-full` extension for 25.08
  anymore; `codecs-extra` replaced it.

### Code changes that must be released first

Without these the Flatpak installs but does not work properly. They are
provided as patches (apply with `git apply`, in this order):

1. `packaging/flatpak/background-portal.patch` — new `momento/background.py`
   and a call from the daemon. A Flatpak cannot install a systemd unit, so the
   daemon asks the Background portal (`org.freedesktop.portal.Background.RequestBackground`
   with `autostart: true` and `commandline: ["momento", "daemon"]`). The desktop
   then starts `flatpak run --command=momento io.github.mehulchachada.Momento daemon`
   at every login and lets it keep running with no window open.
2. `packaging/flatpak/flatpak-sandbox.patch`:
   - **Socket path.** Every `flatpak run` gets its own private
     `$XDG_RUNTIME_DIR`; only `$XDG_RUNTIME_DIR/app/$FLATPAK_ID` is shared. The
     control socket and overlay pidfile move there, otherwise
     `flatpak run io.github.mehulchachada.Momento save 30s` can never reach the
     daemon.
   - **Starting the daemon from the clip bar.** In the sandbox there is no
     `systemctl`, and a child process dies when the overlay exits (a sandbox
     ends with its first process). The overlay starts the daemon with
     `flatpak-spawn momento daemon`, which creates an independent instance.
   - **Opening the bar from the app menu.** Each instance has its own PID
     namespace, so the pidfile toggle can't see a bar the daemon opened. In the
     Flatpak, `momento overlay` asks the daemon (new `overlay` IPC command) to
     open or close it, and only runs locally when no daemon is up.
   - **layer-shell detection.** The plugin is at
     `/app/lib/plugins/wayland-shell-integration/liblayer-shell.so`, found by
     Qt through `QT_PLUGIN_PATH`; the check now also looks there.

Things that already work in the sandbox without changes: `ffmpeg`, `pactl`,
`pw-dump` and `xdg-user-dir` are in the runtime; the Videos folder resolves
correctly; the ScreenCast and GlobalShortcuts portals identify the app by its
Flatpak id (the host-only `Registry.Register` call fails harmlessly); the
replay buffer lives in `~/.var/app/io.github.mehulchachada.Momento/cache/`.
The systemd memory limits in `momento.service` do not apply to the Flatpak.

### Test-building locally

```sh
flatpak install --user flathub org.flatpak.Builder
flatpak run org.flatpak.Builder --user --install --force-clean \
    --install-deps-from=flathub build-dir packaging/flatpak/io.github.mehulchachada.Momento.yml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest packaging/flatpak/io.github.mehulchachada.Momento.yml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder repo repo   # after --repo=repo
```

To build an unreleased tree, replace the momento module's `archive` source
with `- type: dir` / `path: ../..`.

### Submitting

1. Tag a release that contains the two patches, and put its tarball URL and
   sha256 into the manifest's last module.
2. Make real screenshots. Flathub requires at least one reachable screenshot
   URL pinned to a tag (not `main`); the current one is `assets/clip-bar.png`
   at `v0.1.0`. A wider shot of the bar over a game (16:9, e.g. 1920×1080)
   reads much better in the store. Add more `<screenshot>` entries as you go.
3. Fork <https://github.com/flathub/flathub> and create a branch **from the
   `new-pr` branch** (not `master`).
4. Add `io.github.mehulchachada.Momento.yml` at the repository root (the
   metainfo stays in this repository; the manifest installs it from the
   tarball).
5. Open a pull request **against `new-pr`** titled
   `Add io.github.mehulchachada.Momento`. Comment `bot, build` to get a test
   build.
6. Answer the review. Expect questions about these permissions; the reasons,
   ready to paste:
   - `--filesystem=xdg-run/pipewire-0`: desktop audio that follows the default
     output, and capturing Steam Game Mode's `gamescope` PipeWire node, which
     has no portal.
   - `--talk-name=org.freedesktop.Notifications`: "Saved last 5 minutes"
     notifications from the background daemon.
   - `--filesystem=xdg-videos`: clips are written to `~/Videos/Momento`.
   - `--device=dri`: hardware H.264 encoding.
7. **App-id verification:** `io.github.mehulchachada.Momento` is verified by
   logging in to the Flathub website with the GitHub account `mehulchachada`
   (Developer portal → Verification). Because the id starts with
   `io.github.<user>`, no website or DNS record is needed.
8. After merge, Flathub creates `github.com/flathub/io.github.mehulchachada.Momento`
   and gives you write access. Future updates are pull requests there; the
   `x-checker-data` entries let Flathub's bot open those PRs automatically
   when a new GitHub release or layer-shell-qt/PyGObject/dbus-python version
   appears.

Users install with:

```sh
flatpak install flathub io.github.mehulchachada.Momento
flatpak run io.github.mehulchachada.Momento daemon   # first start; afterwards it autostarts
```

For a custom keyboard shortcut in the Flatpak, bind
`flatpak run io.github.mehulchachada.Momento save 30s`.

## 7. Official distro repositories

Worth doing once Momento is stable and has users, not before:
- **Fedora:** needs a Fedora packager account and a sponsor; submit a package
  review on Bugzilla. `packaging/fedora/momento.spec` already follows the
  Python packaging guidelines (pyproject macros), so it is a good starting
  point.
- **Debian/Ubuntu:** file an ITP bug and find a Debian Developer to sponsor
  uploads, typically through the Debian Python Team. Ubuntu picks it up from
  Debian automatically.
- **Arch [extra]:** a package maintainer has to adopt it, usually after it is
  popular on the AUR.

## 8. Why not AppImage

An AppImage bundles everything into one file, which fits poorly here:
Momento's hardware encoding needs the **host's** VA-API/NVIDIA drivers
matched to the host's Mesa and kernel, GStreamer plugins that load those
drivers, and a layer-shell Qt plugin built against the exact Qt it runs in.
Bundling GStreamer and Qt breaks the driver side; not bundling them makes it
no more portable than the distro packages. It also can't install the systemd
unit or use the Background portal for autostart. Flatpak solves exactly these
problems (GL/VA driver extensions matched to the runtime), so it is the
portable format to use.

## 9. Name notes

- `momento` is free on the AUR, in the Arch, Fedora and Debian archives, and
  on Flathub (as an app id nothing collides: ours is
  `io.github.mehulchachada.Momento`).
- The unrelated *Momento* serverless cache has a CLI also called `momento`
  (distributed by that company via Homebrew and its own packages) and a Python
  SDK named `momento` on **PyPI**. That is why Momento is not published to
  PyPI under that name (use e.g. `momento-replay` if it ever is), and why the
  release packages keep the code out of `site-packages`. If the clash ever
  becomes a real problem for distro packages, rename the package to
  `momento-replay` with `Provides: momento` and keep the command name.
