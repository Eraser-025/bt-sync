# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. See the LICENSE file for details.
"""
bt-sync-gui - system tray app to keep Bluetooth pairing keys in sync between
Linux and Windows on a dual-boot machine.

Runs as a normal user. It reads /var/lib/bt-sync/state.json (written
periodically by the root scan service) and runs "pkexec bt-sync ..." to copy,
export or import keys.

Usage:  bt-sync-gui          open the window
        bt-sync-gui --tray   start hidden in the system tray
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from PyQt6.QtCore import QLibraryInfo, QProcess, Qt, QTimer, QTranslator, QUrl
    from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPainter, QPixmap
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                                 QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QFrame, QGroupBox, QHBoxLayout, QLabel, QLayout, QListWidget,
                                 QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                                 QPushButton, QSystemTrayIcon, QToolButton, QVBoxLayout,
                                 QWidget)
    QT6 = True
except ImportError:   # e.g. Ubuntu 22.04 / Linux Mint 21 have no PyQt6
    from PyQt5.QtCore import QLibraryInfo, QProcess, Qt, QTimer, QTranslator, QUrl
    from PyQt5.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPainter, QPixmap
    from PyQt5.QtNetwork import QLocalServer, QLocalSocket
    from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                                 QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QFrame, QGroupBox, QHBoxLayout, QLabel, QLayout, QListWidget,
                                 QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                                 QPushButton, QSystemTrayIcon, QToolButton, QVBoxLayout,
                                 QWidget)
    QT6 = False

from . import KOFI_URL, PROJECT_URL, __version__, i18n, os_name
from .i18n import LANGUAGES, _, ngettext

# BT_SYNC_STATE lets developers point the GUI at a fake state file.
STATE = Path(os.environ.get("BT_SYNC_STATE") or "/var/lib/bt-sync/state.json")
TRIGGER = Path("/var/lib/bt-sync/refresh.trigger")
_PKG = Path(__file__).resolve().parent


def _find_helper():
    # The root backend: <prefix>/bin/bt-sync next to the installed package,
    # or the launcher in the source tree. Its path must match the polkit
    # policy, so it is never looked up in $PATH.
    for cand in (_PKG.parents[2] / "bin" / "bt-sync", _PKG.parent / "bt-sync"):
        if cand.is_file():
            return str(cand)
    return "/usr/bin/bt-sync"


HELPER = _find_helper()
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
CFG_DIR = XDG_CONFIG / "bt-sync"
CFG = CFG_DIR / "config.json"
AUTOSTART = XDG_CONFIG / "autostart" / "bt-sync-gui.desktop"
SERVER_NAME = f"bt-sync-gui-{os.getuid()}"
POLL_MS = 10_000  # a plain stat() of the state file every 10 s: negligible cost
ENABLE_CMD = "sudo systemctl enable --now bt-sync-scan.timer bt-sync-scan.path"
KEYFILE_FORMAT = "bt-sync-keys"
MAX_KEYFILE = 4 * 1024 * 1024

AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=Bluetooth Sync
Comment=Tray icon for bt-sync
Exec=bt-sync-gui --tray
Icon=bt-sync
Terminal=false
X-GNOME-Autostart-enabled=true
"""

GREEN, AMBER, BLUE = QColor("#2e9e5b"), QColor("#e08a00"), QColor("#3b82f6")
ROLE = Qt.ItemDataRole.UserRole
ARROW_STYLE = "font-size:26px;"
ARROW_SUGGESTED = ("QPushButton { font-size:26px; border:2px solid #3b82f6; "
                   "border-radius:6px; }")


# ------------------------------------------------------------------ helpers
def load_cfg():
    try:
        cfg = json.loads(CFG.read_text())
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("archive", {})    # "adapter|mac" -> {"name", "mac"}
    cfg.setdefault("notified", [])   # signatures of items already notified
    cfg.setdefault("notify", True)
    cfg.setdefault("language", None)  # None = system language
    return cfg


def save_cfg(cfg):
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CFG.with_name(CFG.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=1))
        os.replace(tmp, CFG)
    except OSError as e:
        print(f"bt-sync-gui: cannot save {CFG}: {e}", file=sys.stderr)


def load_state():
    try:
        st = json.loads(STATE.read_text())
        return st if isinstance(st, dict) and "items" in st else None
    except (OSError, ValueError):
        return None


def state_error(st):
    err = st.get("error")
    if not err:
        return None
    if isinstance(err, str):                      # state file of bt-sync < 1.0
        return err
    return i18n.render([(p.get("id", ""), p.get("args") or {}) for p in err])


def scan_units_enabled():
    """Is the background scan enabled? (systemctl works as a normal user)"""
    try:
        r = subprocess.run(["systemctl", "is-enabled", "bt-sync-scan.timer"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return True     # cannot tell: do not nag
    return r.stdout.strip() in ("enabled", "enabled-runtime", "static", "indirect", "alias")


def qt_translations_dir():
    if QT6:
        return QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    return QLibraryInfo.location(QLibraryInfo.TranslationsPath)


def base_icon():
    # custom icon: ~/.config/bt-sync/icon.svg (or icon.png), if present
    for ext in ("svg", "png"):
        custom = CFG_DIR / f"icon.{ext}"
        if custom.exists():
            ic = QIcon(str(custom))
            if not ic.isNull():
                return ic
    for name in ("bt-sync", "bluetooth", "preferences-system-bluetooth", "bluetooth-active"):
        ic = QIcon.fromTheme(name)
        if not ic.isNull():
            return ic
    bundled = _PKG.parent / "data" / "bt-sync.svg"     # running from the source tree
    if bundled.exists():
        return QIcon(str(bundled))
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#2d7ff9"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.end()
    return QIcon(pm)


def badge_icon(ic):
    """Icon with an orange dot in the top right corner (= something to sync)."""
    pm = ic.pixmap(64, 64)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#ff8c00"))
    p.setPen(QColor("white"))
    p.drawEllipse(36, 0, 27, 27)
    p.end()
    return QIcon(pm)


def kinds_text(info):
    k = [n for n, on in (("Classic", info["classic"]), ("LE", info["le"])) if on]
    return " + ".join(k)


def fmt_time(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"


def bullets(lines):
    return "\n".join("• " + line for line in lines)


# ------------------------------------------------------------ settings
class SettingsDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(_("Settings"))
        lay = QVBoxLayout(self)
        # The dialog always takes the size its content needs: wrapped labels
        # (which Qt does not account for when a window is resized) can never
        # be cut, whatever the language, font or scaling.
        lay.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        general = QGroupBox(_("General"))
        form = QFormLayout(general)
        self.lang = QComboBox()
        self.lang.addItem(_("System default ({language})").format(
            language=LANGUAGES[i18n.system_language()]), "")
        for code, name in LANGUAGES.items():
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(win.cfg.get("language") or "")))
        self.lang.currentIndexChanged.connect(self._language_changed)
        form.addRow(_("Language:"), self.lang)
        self.chk_autostart = QCheckBox(_("Start in tray at login"))
        self.chk_autostart.setChecked(AUTOSTART.exists())
        if win.tray is None:
            self.chk_autostart.setEnabled(False)
            self.chk_autostart.setToolTip(_("No system tray available on this desktop"))
        self.chk_autostart.toggled.connect(win.set_autostart)
        self.chk_notify = QCheckBox(_("Show desktop notifications"))
        self.chk_notify.setChecked(bool(win.cfg["notify"]))
        self.chk_notify.toggled.connect(win.set_notify)
        form.addRow(self.chk_autostart)
        form.addRow(self.chk_notify)
        lay.addWidget(general)

        keys = QGroupBox(_("Pairing keys backup"))
        kl = QVBoxLayout(keys)
        info = QLabel(_("Save the pairing keys of this Linux system to a file and "
                        "restore them later, for example after reinstalling Linux "
                        "or on another Linux system installed on this computer."))
        info.setWordWrap(True)
        info.setMinimumWidth(440)
        kl.addWidget(info)
        row = QHBoxLayout()
        btn_export = QPushButton(QIcon.fromTheme("document-export"), _("Export keys…"))
        btn_import = QPushButton(QIcon.fromTheme("document-import"), _("Import keys…"))
        btn_export.clicked.connect(win.export_keys)
        btn_import.clicked.connect(win.import_keys)
        row.addWidget(btn_export)
        row.addWidget(btn_import)
        row.addStretch(1)
        kl.addLayout(row)
        note = QLabel(_("The file contains secret keys: keep it private."))
        note.setStyleSheet("color:gray;")
        note.setWordWrap(True)
        note.setMinimumWidth(440)
        kl.addWidget(note)
        lay.addWidget(keys)

        about = QLabel(f"bt-sync {__version__} · <a href='{PROJECT_URL}'>GitHub</a> · "
                       "GPL-3.0-or-later")
        about.setOpenExternalLinks(True)
        about.setStyleSheet("color:gray;")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(about)
        bottom.addStretch(1)
        bottom.addWidget(bb)
        lay.addLayout(bottom)

    def _language_changed(self):
        self.win.cfg["language"] = self.lang.currentData() or None
        save_cfg(self.win.cfg)
        self.accept()
        self.win.apply_language()
        QTimer.singleShot(0, self.win.open_settings)   # reopen, translated


# ------------------------------------------------------------- window
class MainWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.cfg = load_cfg()
        self.state = None
        self._mtime = 0.0
        self._wait_since = None
        self._proc = None
        self._quitting = False
        self.qt_translator = None
        self.linux = os_name()
        self.scan_enabled = scan_units_enabled()
        self.icon = base_icon()
        self.setWindowIcon(self.icon)
        self.setWindowTitle("Bluetooth Sync")
        self.resize(900, 720)
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.icon, self)
            self.tray.activated.connect(self._tray_activated)
            self.tray.messageClicked.connect(self.show_window)
        self.apply_language()
        if self.tray:
            self.tray.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)

    # ---- language
    def apply_language(self):
        code = i18n.set_language(self.cfg.get("language"))
        if self.qt_translator:
            self.app.removeTranslator(self.qt_translator)
        self.qt_translator = QTranslator(self)
        if code != "en" and self.qt_translator.load(f"qtbase_{code}", qt_translations_dir()):
            self.app.installTranslator(self.qt_translator)
        self._build_ui()
        self._build_tray_menu()
        self.poll(force=True)

    # ---- UI
    def _list(self):
        lst = QListWidget()
        lst.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lst.setAlternatingRowColors(True)
        # long translations wrap instead of needing a horizontal scrollbar
        lst.setWordWrap(True)
        lst.setResizeMode(QListWidget.ResizeMode.Adjust)
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return lst

    @staticmethod
    def _box(title, widget):
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        lay.addWidget(widget)
        return box

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        title = QLabel("<span style='font-size:15pt; font-weight:600'>%s</span>"
                       % _("Bluetooth devices"))
        self.status = QLabel("")
        self.btn_refresh = QPushButton(QIcon.fromTheme("view-refresh"), " " + _("Refresh"))
        self.btn_refresh.setToolTip(_("Rescan both systems now"))
        self.btn_refresh.clicked.connect(self.request_refresh)
        self.btn_settings = QToolButton()
        self.btn_settings.setText("⚙")      # same gear on every icon theme
        self.btn_settings.setStyleSheet("font-size:20px; padding:0 4px;")
        self.btn_settings.setToolTip(_("Settings"))
        self.btn_settings.setAutoRaise(True)
        self.btn_settings.clicked.connect(self.open_settings)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.status)
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_settings)
        root.addLayout(top)

        hint_row = QHBoxLayout()
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.banner.setStyleSheet("color:#e08a00;")
        self.banner.hide()
        self.btn_setup = QPushButton(_("Enable now"))
        self.btn_setup.clicked.connect(self.setup_scan)
        self.btn_setup.hide()
        hint_row.addWidget(self.banner, 1)
        hint_row.addWidget(self.btn_setup)
        root.addLayout(hint_row)

        mid = QHBoxLayout()
        self.left, self.right = self._list(), self._list()
        self.left.itemSelectionChanged.connect(lambda: self._selection_changed(self.left))
        self.right.itemSelectionChanged.connect(lambda: self._selection_changed(self.right))
        self.box_left = self._box(self.linux, self.left)
        mid.addWidget(self.box_left, 1)
        arrows = QVBoxLayout()
        arrows.addStretch(1)
        self.btn_l2w = QPushButton("→")
        self.btn_w2l = QPushButton("←")
        self.cap_l2w, self.cap_w2l = QLabel(), QLabel()
        for b, cap in ((self.btn_l2w, self.cap_l2w), (self.btn_w2l, self.cap_w2l)):
            b.setFixedSize(64, 56)
            b.setStyleSheet(ARROW_STYLE)
            cap.setFixedWidth(96)
            cap.setWordWrap(True)
            cap.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            cap.setStyleSheet("color:gray; font-size:9pt;")
        self.btn_l2w.clicked.connect(lambda: self.sync("linux2win"))
        self.btn_w2l.clicked.connect(lambda: self.sync("win2linux"))
        for w in (self.btn_l2w, self.cap_l2w):
            arrows.addWidget(w, 0, Qt.AlignmentFlag.AlignHCenter)
        arrows.addSpacing(18)
        for w in (self.btn_w2l, self.cap_w2l):
            arrows.addWidget(w, 0, Qt.AlignmentFlag.AlignHCenter)
        arrows.addStretch(1)
        mid.addLayout(arrows)
        mid.addWidget(self._box("Windows", self.right), 1)
        root.addLayout(mid, 3)

        self.sel_hint = QLabel()
        self.sel_hint.setWordWrap(True)
        self.sel_hint.hide()
        root.addWidget(self.sel_hint)

        legend = QLabel(_("✔ in sync     → only on one system     "
                          "⚠ different keys (★ = newer key)"))
        legend.setStyleSheet("color:gray;")
        root.addWidget(legend)

        self.archive = self._list()
        arch_box = QGroupBox(_("Archive: never sync these and don't notify me"))
        ab = QHBoxLayout(arch_box)
        ab.addWidget(self.archive, 1)
        col = QVBoxLayout()
        btn_arch = QPushButton(_("Archive selected ↓"))
        btn_rest = QPushButton(_("↑ Restore selected"))
        btn_arch.clicked.connect(self.archive_selected)
        btn_rest.clicked.connect(self.restore_selected)
        col.addStretch(1)
        col.addWidget(btn_arch)
        col.addWidget(btn_rest)
        col.addStretch(1)
        ab.addLayout(col)
        root.addWidget(arch_box, 1)

        # ---- support (discreet footer)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(line)
        support = QHBoxLayout()
        support.addStretch(1)
        msg = QLabel(_("Enjoying bt-sync? Buy me a 1UP!"))
        msg.setStyleSheet("color:gray;")
        btn_support = QPushButton(_("Support me ☕"))
        btn_support.setToolTip(KOFI_URL)
        btn_support.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_support.clicked.connect(self.open_support)
        support.addWidget(msg)
        support.addWidget(btn_support)
        root.addLayout(support)
        self._update_captions()
        self._selection_changed(None)

    def _update_captions(self):
        self.box_left.setTitle(self.linux)
        self.cap_l2w.setText(_("to Windows"))
        self.cap_w2l.setText(_("to {system}").format(system=self.linux))
        self.btn_l2w.setToolTip(_("Copy the selected devices from {system} to Windows")
                                .format(system=self.linux))
        self.btn_w2l.setToolTip(_("Copy the selected devices from Windows to {system}")
                                .format(system=self.linux))

    def _build_tray_menu(self):
        if not self.tray:
            return
        menu = QMenu(self)
        menu.addAction(_("Open Bluetooth Sync")).triggered.connect(self.show_window)
        menu.addAction(QIcon.fromTheme("view-refresh"), _("Refresh now")) \
            .triggered.connect(self.request_refresh)
        menu.addAction(QIcon.fromTheme("configure"), _("Settings…")) \
            .triggered.connect(self.open_settings)
        menu.addSeparator()
        menu.addAction(QIcon.fromTheme("application-exit"), _("Quit")) \
            .triggered.connect(self.quit_app)
        self.tray.setContextMenu(menu)
        self.menu = menu   # keep a reference

    # ---- window / tray
    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.show_window()

    def quit_app(self):
        self._quitting = True
        QApplication.quit()

    def closeEvent(self, ev):
        if self.tray and not self._quitting:
            ev.ignore()
            self.hide()
        else:
            ev.accept()

    def show_hint(self, text, setup=False):
        """Orange message under the title; setup=True adds the 'Enable now' button."""
        self.banner.setText(text or "")
        self.banner.setVisible(bool(text))
        self.btn_setup.setVisible(bool(text) and setup)

    def show_setup_hint(self):
        self.show_hint(_("The background scan is not enabled, so the device lists are "
                         "not updated."), setup=True)

    def setup_scan(self):
        self.run_helper(["setup"], b"", self._setup_done)

    def _setup_done(self, out, err, code):
        if code != 0:
            self._unexpected(out, err, code)
            return
        self.scan_enabled = True
        self.show_hint(None)
        self.status.setText(_("Background scan enabled."))
        self._wait_since = time.time()
        self.btn_refresh.setEnabled(False)
        QTimer.singleShot(90_000, self._refresh_timeout)

    # ---- settings
    def open_settings(self):
        self.show_window()
        SettingsDialog(self).exec()

    def set_autostart(self, on):
        try:
            if on:
                AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
                AUTOSTART.write_text(AUTOSTART_ENTRY)
            else:
                AUTOSTART.unlink(missing_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Bluetooth Sync",
                                _("Cannot change the login autostart:") + f"\n{e}")

    def set_notify(self, on):
        self.cfg["notify"] = bool(on)
        save_cfg(self.cfg)

    def open_support(self):
        if QDesktopServices.openUrl(QUrl(KOFI_URL)):
            return
        r = QProcess.startDetached("xdg-open", [KOFI_URL])
        if not (r[0] if isinstance(r, tuple) else r):
            self.status.setText(_("Could not open the browser: {url}").format(url=KOFI_URL))

    # ---- state
    def poll(self, force=False):
        try:
            mtime = STATE.stat().st_mtime
        except OSError:
            if force:
                self.status.setText(_("No scan available yet"))
                if self.scan_enabled:
                    self.show_hint(_("The background scan has not run yet. If this "
                                     "persists, make sure it is enabled:") + "  " + ENABLE_CMD)
                else:
                    self.show_setup_hint()
            return
        if force or mtime != self._mtime:
            self._mtime = mtime
            st = load_state()
            if st:
                self.state = st
                if st.get("linux_name") and st["linux_name"] != self.linux:
                    self.linux = st["linux_name"]
                    self._update_captions()
                self.refresh_lists()
                self.check_notifications()
                if self._wait_since and st.get("time", 0) >= self._wait_since - 1:
                    self._wait_since = None
                    self.btn_refresh.setEnabled(True)

    def active_items(self):
        arch = self.cfg["archive"]
        return [i for i in self.state["items"] if f"{i['adapter']}|{i['mac']}" not in arch]

    def check_notifications(self):
        items = self.active_items()
        cur = {f"{i['adapter']}|{i['mac']}|{i['kind']}|{i['action']}": i for i in items}
        notified = set(self.cfg["notified"])
        new = [k for k in cur if k not in notified]
        self.cfg["notified"] = sorted((notified & set(cur)) | set(new))
        save_cfg(self.cfg)
        names = sorted({cur[k]["name"] for k in new})
        if self.tray:
            self.tray.setIcon(badge_icon(self.icon) if cur else self.icon)
            n = len({(i["adapter"], i["mac"]) for i in items})
            self.tray.setToolTip(
                "Bluetooth Sync: " + (ngettext("{n} device to sync", "{n} devices to sync",
                                               n).format(n=n) if n
                                      else _("everything in sync")))
            if names and self.cfg["notify"] and not self.isVisible():
                self.tray.showMessage(
                    "Bluetooth Sync",
                    _("To sync: {devices}").format(devices=", ".join(names)),
                    QSystemTrayIcon.MessageIcon.Information, 10000)

    # ---- lists
    def refresh_lists(self):
        st = self.state
        arch = self.cfg["archive"]
        by_dev = {}
        for it in st["items"]:
            by_dev.setdefault((it["adapter"], it["mac"]), []).append(it)

        for lst, here, side, other in ((self.left, "linux", st.get("linux", {}),
                                        st.get("windows", {})),
                                       (self.right, "windows", st.get("windows", {}),
                                        st.get("linux", {}))):
            selected = {i.data(ROLE)[0] + "|" + i.data(ROLE)[1] for i in lst.selectedItems()}
            lst.blockSignals(True)
            lst.clear()
            for ad, devs in sorted(side.items()):
                for mac, info in sorted(devs.items(), key=lambda kv: kv[1]["name"].lower()):
                    if f"{ad}|{mac}" in arch:
                        continue
                    items = by_dev.get((ad, mac), [])
                    acts = {i["action"] for i in items}
                    present_other = mac in other.get(ad, {})
                    newer, tip = None, ""
                    if "conflict" in acts:
                        conf = [i for i in items if i["action"] == "conflict"]
                        sides = {i.get("newer") for i in conf}
                        newer = sides.pop() if len(sides) == 1 else None
                        status, mark, color = "conflict", "⚠", AMBER
                        txt = _("different keys")
                        if newer == here:
                            txt += " · ★ " + _("newer key")
                        elif newer:
                            txt += " · " + _("older key")
                        tip = "\n".join(
                            _("{kind} key changed on Windows: {wtime}, on {system}: "
                              "{ltime}").format(kind="Classic" if i["kind"] == "classic"
                                                else "LE",
                                                wtime=fmt_time(i.get("win_time")),
                                                system=self.linux,
                                                ltime=fmt_time(i.get("linux_time")))
                            for i in conf)
                        if not newer:
                            tip += "\n" + _("Cannot tell which key is newer: copy from "
                                            "the system where you paired the device last.")
                    elif not present_other:
                        status, mark, color, txt = "only", "→", BLUE, _("only here")
                    elif acts:
                        status, mark, color, txt = "partial", "→", BLUE, _("partially synced")
                    else:
                        status, mark, color, txt = "sync", "✔", GREEN, _("in sync")
                    row = QListWidgetItem(f"{mark}  {info['name']}\n     {mac} · "
                                          f"{kinds_text(info)} · {txt}")
                    row.setForeground(QBrush(color))
                    if tip:
                        row.setToolTip(tip)
                    row.setData(ROLE, (ad, mac, status, info["name"], newer))
                    lst.addItem(row)
                    if f"{ad}|{mac}" in selected:
                        row.setSelected(True)
            lst.blockSignals(False)

        self.archive.clear()
        for key, v in sorted(arch.items(), key=lambda kv: kv[1]["name"].lower()):
            row = QListWidgetItem(f"{v['name']}\n     {v['mac']}")
            row.setData(ROLE, key)
            self.archive.addItem(row)

        err = state_error(st)
        if not self.scan_enabled:
            self.show_setup_hint()
        elif err:
            self.show_hint("⚠ " + _("Could not read Windows:") + " " + err)
        elif st.get("linux") and st.get("windows") and \
                not set(st["linux"]) & set(st["windows"]):
            self.show_hint("⚠ " + _("Linux and Windows use different Bluetooth adapters "
                                    "(different adapter addresses): nothing can be "
                                    "synced."))
        else:
            self.show_hint(None)
        self.status.setText(_("Last scan: {time}").format(time=time.strftime(
            "%H:%M:%S", time.localtime(st.get("time", 0)))))
        self._selection_changed(None)

    def _selection_changed(self, lst):
        """Enable the arrow of the list with a selection and suggest a direction."""
        if lst is not None:
            other = self.right if lst is self.left else self.left
            if lst.selectedItems() and other.selectedItems():
                other.blockSignals(True)
                other.clearSelection()
                other.blockSignals(False)
        busy = self._proc is not None
        hint, good = None, False
        for b, src, here in ((self.btn_l2w, self.left, "linux"),
                             (self.btn_w2l, self.right, "windows")):
            sel = [i.data(ROLE) for i in src.selectedItems() if i.data(ROLE)[2] != "sync"]
            b.setEnabled(bool(sel) and not busy)
            conf = [s for s in sel if s[2] == "conflict"]
            older = [s[3] for s in conf if s[4] and s[4] != here]
            suggested = bool(conf) and not older and all(s[4] == here for s in conf)
            b.setStyleSheet(ARROW_SUGGESTED if suggested and not busy else ARROW_STYLE)
            if older:
                there = "Windows" if here == "linux" else self.linux
                hint = "⚠ " + _("{devices}: the key on {system} is newer. You probably "
                                "want to copy in the other direction.").format(
                    devices=", ".join(older), system=there)
            elif suggested:
                hint, good = "✔ " + _("The selected key is the newer one: copying it is "
                                      "probably right."), True
        self.sel_hint.setText(hint or "")
        self.sel_hint.setStyleSheet(f"color:{'#2e9e5b' if good else '#e08a00'};")
        self.sel_hint.setVisible(bool(hint))

    # ---- archive
    def archive_selected(self):
        for lst in (self.left, self.right):
            for it in lst.selectedItems():
                ad, mac, _status, name, _newer = it.data(ROLE)
                self.cfg["archive"][f"{ad}|{mac}"] = {"name": name, "mac": mac}
        save_cfg(self.cfg)
        self.refresh_lists()
        self.check_notifications()

    def restore_selected(self):
        for it in self.archive.selectedItems():
            self.cfg["archive"].pop(it.data(ROLE), None)
        save_cfg(self.cfg)
        self.refresh_lists()
        self.check_notifications()

    # ---- forced refresh
    def request_refresh(self):
        if not self.scan_enabled:
            self.scan_enabled = scan_units_enabled()   # maybe enabled from a terminal
            if not self.scan_enabled:
                self.show_setup_hint()
                return
        try:
            with open(TRIGGER, "w") as f:   # watched by bt-sync-scan.path
                f.write(f"{time.time()}\n")
        except OSError as e:
            self.status.setText(_("Cannot start a scan"))
            self.show_hint(_("Cannot request a scan ({error}). Try:").format(error=e)
                           + "  sudo systemd-tmpfiles --create bt-sync.conf  &&  "
                           + ENABLE_CMD)
            return
        self._wait_since = time.time()
        self.btn_refresh.setEnabled(False)
        self.status.setText(_("Scanning…"))
        QTimer.singleShot(90_000, self._refresh_timeout)

    def _refresh_timeout(self):
        if self._wait_since:
            self._wait_since = None
            self.btn_refresh.setEnabled(True)
            self.status.setText(_("The scan did not answer"))
            self.scan_enabled = scan_units_enabled()
            if not self.scan_enabled:
                self.show_setup_hint()
            else:
                self.show_hint(_("The scan service did not answer. Make sure it is "
                                 "enabled:") + "  " + ENABLE_CMD)

    # ---- running the root helper
    def run_helper(self, args, stdin, callback):
        """Run 'pkexec bt-sync <args>'; callback(stdout, stderr, exit code)."""
        if self._proc is not None:
            return
        p = QProcess(self)
        self._proc = p
        self.set_busy(True)
        p.finished.connect(lambda code, _st: self._helper_finished(p, code, callback))
        p.start("pkexec", [HELPER, "--lang", i18n.current()] + args)
        if not p.waitForStarted(5000):
            self._proc = None
            self.set_busy(False)
            QMessageBox.warning(self, "Bluetooth Sync",
                                _("Could not start pkexec. Is polkit installed and is a "
                                  "polkit authentication agent running?"))
            return
        p.write(stdin)
        p.closeWriteChannel()

    def _helper_finished(self, p, code, callback):
        out = bytes(p.readAllStandardOutput()).decode(errors="replace").strip()
        err = bytes(p.readAllStandardError()).decode(errors="replace").strip()
        self._proc = None
        self.set_busy(False)
        if code in (126, 127) and not out:
            self.status.setText(_("Authentication cancelled or not authorized"))
            return
        callback(out, err, code)

    def _unexpected(self, out, err, code):
        QMessageBox.warning(self, "Bluetooth Sync",
                            _("Unexpected answer from {helper} (exit code {code}).")
                            .format(helper=HELPER, code=code)
                            + "\n\n" + (err or out or "-"))

    def _last_json(self, out, err, code, keys):
        try:
            res = json.loads(out.splitlines()[-1])
            if all(k in res for k in keys):
                return res
        except (ValueError, IndexError, TypeError):
            pass
        self._unexpected(out, err, code)
        return None

    def set_busy(self, busy):
        if busy:
            self.status.setText(_("Working…"))
        self._selection_changed(None)

    # ---- sync
    def sync(self, direction):
        lst = self.left if direction == "linux2win" else self.right
        sel = [i.data(ROLE) for i in lst.selectedItems() if i.data(ROLE)[2] != "sync"]
        if not sel:
            return
        conflicts = [s for s in sel if s[2] == "conflict"]
        if conflicts:
            dest = "Windows" if direction == "linux2win" else self.linux
            dest_side = "windows" if direction == "linux2win" else "linux"
            text = _("The keys of {devices} differ between the two systems.\n"
                     "The ones on {system} will be overwritten.").format(
                devices=", ".join(s[3] for s in conflicts), system=dest)
            newer_lost = [s[3] for s in conflicts if s[4] == dest_side]
            if newer_lost:
                text += "\n\n⚠ " + _("Careful: for {devices} the key you are about to "
                                     "overwrite is the newer one.").format(
                    devices=", ".join(newer_lost))
            ok = QMessageBox.question(self, _("Overwrite the keys?"),
                                      text + "\n\n" + _("Continue?"))
            if ok != QMessageBox.StandardButton.Yes:
                return
        reqs = [{"adapter": s[0], "mac": s[1], "direction": direction} for s in sel]
        self.run_helper(["apply-json"], json.dumps({"requests": reqs}).encode(),
                        self._sync_done)

    def _sync_done(self, out, err, code):
        res = self._last_json(out, err, code, ("ok", "errors"))
        if res is None:
            return
        if res["errors"]:
            text = ""
            if res["ok"]:
                text += _("Done:") + "\n" + bullets(res["ok"]) + "\n\n"
            text += _("Problems:") + "\n" + bullets(res["errors"])
            QMessageBox.warning(self, "Bluetooth Sync", text)
        if res["ok"]:
            self.status.setText(_("Done:") + " " + "; ".join(res["ok"]))
        self.poll(force=True)

    # ---- key backup
    def export_keys(self):
        default = Path.home() / f"bt-sync-keys-{time.strftime('%Y%m%d')}.json"
        path = QFileDialog.getSaveFileName(self, _("Export pairing keys"), str(default),
                                           _("bt-sync key backup") + " (*.json)")[0]
        if path:
            self.run_helper(["export-keys"], b"",
                            lambda out, err, code: self._export_done(path, out, err, code))

    def _export_done(self, path, out, err, code):
        try:
            obj = json.loads(out)
            if obj.get("format") != KEYFILE_FORMAT:
                raise ValueError
        except (ValueError, AttributeError):
            self._unexpected(out, err, code)
            return
        n = sum(bool(d.get("classic")) + bool(d.get("le"))
                for devs in obj["adapters"].values() for d in devs.values())
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(out + "\n")
        except OSError as e:
            QMessageBox.warning(self, "Bluetooth Sync",
                                _("Cannot write {path}: {error}").format(path=path, error=e))
            return
        QMessageBox.information(
            self, _("Export pairing keys"),
            ngettext("Exported {n} pairing key to:", "Exported {n} pairing keys to:", n)
            .format(n=n) + f"\n{path}\n\n"
            + _("The file contains secret keys: keep it private."))

    def import_keys(self):
        path = QFileDialog.getOpenFileName(self, _("Import pairing keys"), str(Path.home()),
                                           _("bt-sync key backup") + " (*.json)")[0]
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            obj = json.loads(data) if len(data) <= MAX_KEYFILE else None
            if not isinstance(obj, dict) or obj.get("format") != KEYFILE_FORMAT:
                raise ValueError
        except (OSError, ValueError):
            QMessageBox.warning(self, _("Import pairing keys"),
                                _("This is not a valid bt-sync key backup file."))
            return
        self.run_helper(["import-keys", "--json", "--dry-run"], data,
                        lambda out, err, code: self._import_planned(data, out, err, code))

    def _import_planned(self, data, out, err, code):
        res = self._last_json(out, err, code, ("new", "same", "different", "errors"))
        if res is None:
            return
        title = _("Import pairing keys")
        if res["errors"]:
            QMessageBox.warning(self, title, bullets(res["errors"]))
            return
        notes = ""
        if res["missing_adapters"]:
            notes = "\n\n" + _("Skipped: these Bluetooth adapters are not on this "
                               "computer: {list}").format(
                list=", ".join(res["missing_adapters"]))
        new, different = res["new"], res["different"]
        if not new and not different:
            QMessageBox.information(self, title, ngettext(
                "Nothing to import: the {n} key in the file is already present.",
                "Nothing to import: the {n} keys in the file are already present.",
                len(res["same"])).format(n=len(res["same"])) + notes)
            return
        text = ""
        if new:
            text += ngettext("{n} new key will be imported:", "{n} new keys will be "
                             "imported:", len(new)).format(n=len(new)) + "\n" + bullets(new)
        if different:
            text += ("\n\n" if text else "") + ngettext(
                "{n} key is different from the one on this computer:",
                "{n} keys are different from the ones on this computer:",
                len(different)).format(n=len(different)) + "\n" + bullets(different)
        text += notes + "\n\n" + _("Bluetooth will restart for a moment.")
        box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self)
        b_new = box.addButton(_("Import new keys only"),
                              QMessageBox.ButtonRole.AcceptRole) if new else None
        b_all = box.addButton(_("Import and overwrite"),
                              QMessageBox.ButtonRole.DestructiveRole) if different else None
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None or clicked not in (b_new, b_all):
            return
        args = ["import-keys", "--json"] + (["--overwrite"] if clicked is b_all else [])
        self.run_helper(args, data, self._import_done)

    def _import_done(self, out, err, code):
        res = self._last_json(out, err, code, ("ok", "errors"))
        if res is None:
            return
        title = _("Import pairing keys")
        if res["errors"]:
            text = (_("Done:") + "\n" + bullets(res["ok"]) + "\n\n") if res["ok"] else ""
            QMessageBox.warning(self, title, text + _("Problems:") + "\n"
                                + bullets(res["errors"]))
        else:
            QMessageBox.information(self, title, _("Done:") + "\n" + bullets(res["ok"]))
        self.poll(force=True)


# ------------------------------------------------------------------- main
def main():
    if "--version" in sys.argv:
        print(f"bt-sync-gui {__version__}")
        return 0
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__.strip())
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName("bt-sync")
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("bt-sync")
    app.setQuitOnLastWindowClosed(False)
    tray_only = "--tray" in sys.argv

    # single instance: a second launch asks the first one to show itself
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        if not tray_only:
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)

    win = MainWindow(app)

    def on_connection():
        conn = server.nextPendingConnection()

        def got():
            if bytes(conn.readAll()) == b"show":
                win.show_window()
        conn.readyRead.connect(got)

    server.newConnection.connect(on_connection)
    if not (tray_only and win.tray):
        win.show_window()
    return app.exec()
