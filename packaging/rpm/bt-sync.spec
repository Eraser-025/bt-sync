# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
#
# RPM package for Fedora and Fedora Atomic desktops (Bazzite, Silverblue,
# Kinoite...). Built on COPR; on Bazzite install it with rpm-ostree.
Name:           bt-sync
Version:        1.0.3
Release:        1%{?dist}
Summary:        Sync Bluetooth pairing keys between Linux and Windows (dual boot)
License:        GPL-3.0-or-later
URL:            https://github.com/Eraser-025/bt-sync
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  make
BuildRequires:  gettext
BuildRequires:  desktop-file-utils
BuildRequires:  systemd-rpm-macros

Requires:       python3
Requires:       python3-pyqt6
Requires:       python3-hivex
Requires:       bluez
Requires:       polkit
Requires:       util-linux
Requires:       xdg-utils
Requires:       systemd
Recommends:     ntfs-3g
Recommends:     qt6-qttranslations

%description
bt-sync reads the Bluetooth pairing keys of Linux (BlueZ) and of the Windows
installation on the same computer, shows the differences and copies the keys
in the direction you choose, so that your devices work on both systems without
pairing them again. It includes a tray app, a background scan and a backup of
the pairing keys.

%prep
%autosetup

%build
%make_build

%install
%make_install PREFIX=%{_prefix}
# README is shipped with %%doc below
rm -rf %{buildroot}%{_docdir}/%{name}
%find_lang %{name}

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{name}.desktop

%post
%systemd_post bt-sync-scan.timer bt-sync-scan.path
%tmpfiles_create %{name}.conf

%preun
%systemd_preun bt-sync-scan.timer bt-sync-scan.path bt-sync-scan.service

%postun
%systemd_postun bt-sync-scan.service

%files -f %{name}.lang
%license LICENSE
%doc README.md
%{_bindir}/bt-sync
%{_bindir}/bt-sync-gui
%{_prefix}/lib/bt-sync/
%{_unitdir}/bt-sync-scan.service
%{_unitdir}/bt-sync-scan.timer
%{_unitdir}/bt-sync-scan.path
%{_tmpfilesdir}/%{name}.conf
%config(noreplace) %{_sysconfdir}/bt-sync.conf
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg
%{_datadir}/polkit-1/actions/io.github.eraser025.btsync.policy

%changelog
* Mon Nov 09 2026 Eraser-025 <349610023+Eraser-025@users.noreply.github.com> - 1.0.3-1
- Show a clear error when the Qt bindings are missing or broken

* Mon Nov 02 2026 Eraser-025 <349610023+Eraser-025@users.noreply.github.com> - 1.0.2-1
- Delete the pairing of a device from Linux or Windows
- Fix reading LE keys without optional registry values (IRK, KeyLength)

* Mon Nov 02 2026 Eraser-025 <349610023+Eraser-025@users.noreply.github.com> - 1.0.1-1
- Settings dialog: text is never cut, in any language or font size

* Mon Nov 02 2026 Eraser-025 <349610023+Eraser-025@users.noreply.github.com> - 1.0.0-1
- First release
