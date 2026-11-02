#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
#
# Build dist/bt-sync_<version>-1_all.deb (for Debian, Ubuntu, Linux Mint,
# Pop!_OS, Zorin...). Needs: make, gettext, dpkg-deb.
set -eu
cd "$(dirname "$0")/../.."
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' btsync/__init__.py)
STAGE=$(mktemp -d)
chmod 755 "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

make DESTDIR="$STAGE" PREFIX=/usr install
install -Dm644 packaging/deb/copyright "$STAGE/usr/share/doc/bt-sync/copyright"
mkdir -p "$STAGE/DEBIAN"
sed "s/@VERSION@/$VERSION/" packaging/deb/control > "$STAGE/DEBIAN/control"
echo "Installed-Size: $(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)" >> "$STAGE/DEBIAN/control"
install -m755 packaging/deb/postinst packaging/deb/prerm packaging/deb/postrm "$STAGE/DEBIAN/"
echo /etc/bt-sync.conf > "$STAGE/DEBIAN/conffiles"

mkdir -p dist
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "dist/bt-sync_${VERSION}-1_all.deb"
