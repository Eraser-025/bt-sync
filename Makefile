# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
#
#   make install                       # installs to /usr (used by the AUR package)
#   sudo make PREFIX=/usr/local install
#   sudo make PREFIX=/usr/local uninstall

PREFIX      ?= /usr
BINDIR      ?= $(PREFIX)/bin
DATADIR     ?= $(PREFIX)/share
SYSCONFDIR  ?= /etc
# systemd and tmpfiles only look in /usr/lib or /etc
UNITDIR     ?= /usr/lib/systemd/system
TMPFILESDIR ?= /usr/lib/tmpfiles.d
DESTDIR     ?=

FIXPATH = sed 's|/usr/bin/|$(BINDIR)/|g'

.PHONY: all install uninstall

all:
	@echo "Nothing to build. Run 'make install' (DESTDIR/PREFIX are honoured)."

install:
	install -Dm755 bt-sync.py          $(DESTDIR)$(BINDIR)/bt-sync
	install -Dm755 bt-sync-gui.py      $(DESTDIR)$(BINDIR)/bt-sync-gui
	install -dm755 $(DESTDIR)$(UNITDIR) $(DESTDIR)$(DATADIR)/polkit-1/actions
	$(FIXPATH) data/bt-sync-scan.service > $(DESTDIR)$(UNITDIR)/bt-sync-scan.service
	$(FIXPATH) data/io.github.eraser025.btsync.policy \
		> $(DESTDIR)$(DATADIR)/polkit-1/actions/io.github.eraser025.btsync.policy
	chmod 644 $(DESTDIR)$(UNITDIR)/bt-sync-scan.service \
		$(DESTDIR)$(DATADIR)/polkit-1/actions/io.github.eraser025.btsync.policy
	install -Dm644 data/bt-sync-scan.timer $(DESTDIR)$(UNITDIR)/bt-sync-scan.timer
	install -Dm644 data/bt-sync-scan.path  $(DESTDIR)$(UNITDIR)/bt-sync-scan.path
	install -Dm644 data/bt-sync.tmpfiles   $(DESTDIR)$(TMPFILESDIR)/bt-sync.conf
	install -Dm644 data/bt-sync.conf       $(DESTDIR)$(SYSCONFDIR)/bt-sync.conf
	install -Dm644 data/bt-sync.desktop    $(DESTDIR)$(DATADIR)/applications/bt-sync.desktop
	install -Dm644 data/bt-sync.svg        $(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/bt-sync.svg
	install -Dm644 README.md               $(DESTDIR)$(DATADIR)/doc/bt-sync/README.md

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/bt-sync $(DESTDIR)$(BINDIR)/bt-sync-gui \
		$(DESTDIR)$(UNITDIR)/bt-sync-scan.service \
		$(DESTDIR)$(UNITDIR)/bt-sync-scan.timer \
		$(DESTDIR)$(UNITDIR)/bt-sync-scan.path \
		$(DESTDIR)$(TMPFILESDIR)/bt-sync.conf \
		$(DESTDIR)$(DATADIR)/polkit-1/actions/io.github.eraser025.btsync.policy \
		$(DESTDIR)$(DATADIR)/applications/bt-sync.desktop \
		$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/bt-sync.svg
	rm -rf $(DESTDIR)$(DATADIR)/doc/bt-sync
	@echo "Kept $(SYSCONFDIR)/bt-sync.conf and /var/lib/bt-sync (backups of the Windows registry)."
