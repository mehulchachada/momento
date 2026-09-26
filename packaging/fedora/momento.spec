# Fedora / COPR spec for Momento.
#
# Build locally:  spectool -g momento.spec && rpmbuild -ba momento.spec
#   (or: fedpkg --release f44 mockbuild,  or: copr-cli build momento momento-*.src.rpm)
#
# Hardware H.264 encoding on AMD needs mesa-va-drivers-freeworld from RPM Fusion
# (Fedora's Mesa is built without the H.264 encoder); Intel needs
# intel-media-driver (RPM Fusion). Without either, Momento falls back to a
# software encoder if one is installed. That cannot be expressed as a Fedora
# dependency, so the post-install message in the README covers it.

%global appid io.github.mehulchachada.Momento

Name:           momento
Version:        0.1.0
Release:        1%{?dist}
Summary:        Instant replay for Linux gaming: save the last 15 s to 60 min

License:        MIT
URL:            https://github.com/mehulchachada/momento
Source0:        %{url}/releases/download/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
BuildRequires:  appstream
BuildRequires:  systemd-rpm-macros
# for the ffmpeg-based export tests in %%check
BuildRequires:  /usr/bin/ffmpeg
BuildRequires:  /usr/bin/ffprobe

Requires:       python3-gobject
Requires:       python3-dbus
Requires:       python3-pyside6
Requires:       gstreamer1
Requires:       gstreamer1-plugins-base
Requires:       gstreamer1-plugins-good
Requires:       gstreamer1-plugins-bad-free
Requires:       gstreamer1-plugin-libav
Requires:       pipewire-gstreamer
# ffmpeg-free (Fedora) or ffmpeg (RPM Fusion); pactl from pulseaudio-utils
Requires:       /usr/bin/ffmpeg
Requires:       /usr/bin/pactl
Requires:       xdg-desktop-portal
Requires:       hicolor-icon-theme
Recommends:     layer-shell-qt
Recommends:     pipewire-utils
Recommends:     xdg-user-dirs
Recommends:     mesa-va-drivers
Recommends:     python3-evdev

%description
Momento is instant replay for Linux gaming, like the Create button on a
PlayStation 5. It records the screen and sound in the background all the
time; one hotkey (Super+Shift+G) saves what just happened, from the last
15 seconds up to the full hour, to your Videos folder. Encoding is done on
the GPU through VA-API or NVENC, and clips are cut without re-encoding.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l momento

install -Dpm0644 data/%{appid}.desktop %{buildroot}%{_datadir}/applications/%{appid}.desktop
install -Dpm0644 assets/logo.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
install -Dpm0644 packaging/flatpak/%{appid}.metainfo.xml %{buildroot}%{_metainfodir}/%{appid}.metainfo.xml
# ExecStart=/usr/bin/momento; data/momento.service is the ~/.local variant.
install -Dpm0644 packaging/systemd/momento.service %{buildroot}%{_userunitdir}/momento.service

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop
appstreamcli validate --no-net %{buildroot}%{_metainfodir}/%{appid}.metainfo.xml
# Modules that need GStreamer/Qt at import time are left out of the import check.
%pyproject_check_import -e momento.pipeline -e momento.overlay -e momento.daemon -e momento.portal -e momento.hotkey -e momento.background
%{python3} -m unittest tests.test_core

%post
%systemd_user_post momento.service

%preun
%systemd_user_preun momento.service

%files -f %{pyproject_files}
%doc README.md data/config.example.toml
%{_bindir}/momento
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
%{_metainfodir}/%{appid}.metainfo.xml
%{_userunitdir}/momento.service

%changelog
* Sat Sep 26 2026 Mehul Chachada <mehulchachada@users.noreply.github.com> - 0.1.0-1
- First release
