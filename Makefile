# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
#
#   make                                  # compile the translations
#   make DESTDIR=/tmp/pkg install         # what the distro packages do (PREFIX=/usr)
#   sudo make PREFIX=/usr/local install   # manual install (also on Bazzite/Silverblue)
#   sudo make PREFIX=/usr/local uninstall
#   make update-po                        # refresh po/bt-sync.pot and the .po files

VERSION     := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' btsync/__init__.py)
PREFIX      ?= /usr
BINDIR      ?= $(PREFIX)/bin
LIBDIR      ?= $(PREFIX)/lib/bt-sync
DATADIR     ?= $(PREFIX)/share
SYSCONFDIR  ?= /etc
DESTDIR     ?=

# systemd and tmpfiles.d only look in /usr/lib or /etc: when installing
# somewhere else (/usr/local, which is the only writable choice on immutable
# systems such as Bazzite) use /etc.
ifeq ($(PREFIX),/usr)
UNITDIR     ?= /usr/lib/systemd/system
TMPFILESDIR ?= /usr/lib/tmpfiles.d
else
UNITDIR     ?= /etc/systemd/system
TMPFILESDIR ?= /etc/tmpfiles.d
endif
POLKITDIR   ?= $(DATADIR)/polkit-1/actions

PO_FILES    := $(wildcard po/*.po)
MO_FILES    := $(patsubst po/%.po,build/locale/%/LC_MESSAGES/bt-sync.mo,$(PO_FILES))
FIXPATH      = sed 's|/usr/bin/|$(BINDIR)/|g'

.PHONY: all install uninstall pot update-po clean check

all: $(MO_FILES)

build/locale/%/LC_MESSAGES/bt-sync.mo: po/%.po
	@mkdir -p $(dir $@)
	msgfmt --check -o $@ $<

pot:
	xgettext --from-code=UTF-8 -L Python --keyword=_ --keyword=N_ \
		--keyword=ngettext:1,2 --add-comments=TRANSLATORS: \
		--package-name=bt-sync --package-version=$(VERSION) \
		--msgid-bugs-address=https://github.com/Eraser-025/bt-sync/issues \
		-o po/bt-sync.pot btsync/*.py

update-po: pot
	for po in $(PO_FILES); do msgmerge --quiet --update --backup=none $$po po/bt-sync.pot; done

check: all
	python3 -m py_compile btsync/*.py && rm -rf btsync/__pycache__
	desktop-file-validate data/bt-sync.desktop

install: all
	install -Dm755 bt-sync     $(DESTDIR)$(BINDIR)/bt-sync
	install -Dm755 bt-sync-gui $(DESTDIR)$(BINDIR)/bt-sync-gui
	install -Dm644 -t $(DESTDIR)$(LIBDIR)/btsync btsync/*.py
	for mo in $(MO_FILES); do \
		install -Dm644 $$mo $(DESTDIR)$(DATADIR)/$${mo#build/}; done
	install -dm755 $(DESTDIR)$(UNITDIR) $(DESTDIR)$(POLKITDIR)
	$(FIXPATH) data/bt-sync-scan.service > $(DESTDIR)$(UNITDIR)/bt-sync-scan.service
	$(FIXPATH) data/io.github.eraser025.btsync.policy \
		> $(DESTDIR)$(POLKITDIR)/io.github.eraser025.btsync.policy
	chmod 644 $(DESTDIR)$(UNITDIR)/bt-sync-scan.service \
		$(DESTDIR)$(POLKITDIR)/io.github.eraser025.btsync.policy
	install -Dm644 data/bt-sync-scan.timer $(DESTDIR)$(UNITDIR)/bt-sync-scan.timer
	install -Dm644 data/bt-sync-scan.path  $(DESTDIR)$(UNITDIR)/bt-sync-scan.path
	install -Dm644 data/bt-sync.tmpfiles   $(DESTDIR)$(TMPFILESDIR)/bt-sync.conf
	test -e $(DESTDIR)$(SYSCONFDIR)/bt-sync.conf || \
		install -Dm644 data/bt-sync.conf $(DESTDIR)$(SYSCONFDIR)/bt-sync.conf
	install -Dm644 data/bt-sync.desktop    $(DESTDIR)$(DATADIR)/applications/bt-sync.desktop
	install -Dm644 data/bt-sync.svg        $(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/bt-sync.svg
	install -Dm644 README.md               $(DESTDIR)$(DATADIR)/doc/bt-sync/README.md

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/bt-sync $(DESTDIR)$(BINDIR)/bt-sync-gui \
		$(DESTDIR)$(UNITDIR)/bt-sync-scan.service \
		$(DESTDIR)$(UNITDIR)/bt-sync-scan.timer \
		$(DESTDIR)$(UNITDIR)/bt-sync-scan.path \
		$(DESTDIR)$(TMPFILESDIR)/bt-sync.conf \
		$(DESTDIR)$(POLKITDIR)/io.github.eraser025.btsync.policy \
		$(DESTDIR)$(DATADIR)/applications/bt-sync.desktop \
		$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/bt-sync.svg
	rm -f $(DESTDIR)$(DATADIR)/locale/*/LC_MESSAGES/bt-sync.mo
	rm -rf $(DESTDIR)$(LIBDIR) $(DESTDIR)$(DATADIR)/doc/bt-sync
	@echo "Kept $(SYSCONFDIR)/bt-sync.conf and /var/lib/bt-sync (backups of the Windows registry)."

clean:
	rm -rf build btsync/__pycache__
