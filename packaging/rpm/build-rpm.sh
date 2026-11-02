#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
#
# Build dist/bt-sync-<version>-1.<dist>.noarch.rpm and the .src.rpm from the
# current source tree (the same thing COPR does from the release tarball).
# Needs: rpm-build, make, gettext, desktop-file-utils, systemd-rpm-macros.
set -eu
cd "$(dirname "$0")/../.."
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' btsync/__init__.py)
TOP=$(mktemp -d)
trap 'rm -rf "$TOP"' EXIT
mkdir -p "$TOP/SOURCES" dist
# same layout as GitHub's tag tarball: bt-sync-<version>/...
tar --exclude=.git --exclude=./build --exclude=./dist \
    --transform "s,^\.,bt-sync-$VERSION," -czf "$TOP/SOURCES/bt-sync-$VERSION.tar.gz" .
rpmbuild --define "_topdir $TOP" -ba packaging/rpm/bt-sync.spec
cp "$TOP"/RPMS/noarch/*.rpm "$TOP"/SRPMS/*.rpm dist/
