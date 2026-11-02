#!/usr/bin/python3
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
periodically by the root scan service) and copies keys by running
"pkexec bt-sync apply-json".

Usage:  bt-sync-gui          open the window
        bt-sync-gui --tray   start hidden in the system tray
"""
import json
import os
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QProcess, Qt, QTimer, QUrl
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QFrame, QGroupBox,
                             QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                             QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QVBoxLayout,
                             QWidget)

__version__ = "1.0.0"

STATE = Path("/var/lib/bt-sync/state.json")
TRIGGER = Path("/var/lib/bt-sync/refresh.trigger")
_HERE = Path(__file__).resolve().parent
# The backend installed next to this script (e.g. /usr/bin or /usr/local/bin).
# Its path must match the polkit policy, so never look it up in $PATH.
HELPER = str(_HERE / "bt-sync") if (_HERE / "bt-sync").is_file() else "/usr/bin/bt-sync"
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
CFG_DIR = XDG_CONFIG / "bt-sync"
CFG = CFG_DIR / "config.json"
AUTOSTART = XDG_CONFIG / "autostart" / "bt-sync-gui.desktop"
SERVER_NAME = f"bt-sync-gui-{os.getuid()}"
POLL_MS = 10_000  # a plain stat() of the state file every 10 s: negligible cost
KOFI_URL = "https://ko-fi.com/eraser117"
ENABLE_CMD = "sudo systemctl enable --now bt-sync-scan.timer bt-sync-scan.path"

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


# ------------------------------------------------------------------ helpers
def os_name():
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("NAME="):
                return line[5:].strip().strip("\"'") or "Linux"
    except OSError:
        pass
    return "Linux"


LINUX = os_name()


def load_cfg():
    try:
        cfg = json.loads(CFG.read_text())
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("archive", {})    # "adapter|mac" -> {"name", "mac"}
    cfg.setdefault("notified", [])   # signatures of items already notified
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
    bundled = _HERE / "data" / "bt-sync.svg"     # running from the source tree
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


# ------------------------------------------------------------- window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bluetooth Sync")
        self.resize(860, 700)
        self.cfg = load_cfg()
        self.state = None
        self._mtime = 0.0
        self._wait_since = None
        self._proc = None
        self._quitting = False
        self.icon = base_icon()
        self.setWindowIcon(self.icon)
        self._build_ui()
        self._build_tray()
        self._sync_autostart_widgets()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)
        self.poll(force=True)

    # ---- UI
    def _list(self):
        lst = QListWidget()
        lst.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lst.setAlternatingRowColors(True)
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
        title = QLabel("<span style='font-size:15pt; font-weight:600'>Bluetooth devices</span>")
        self.status = QLabel("")
        self.btn_refresh = QPushButton(QIcon.fromTheme("view-refresh"), " Refresh")
        self.btn_refresh.setToolTip("Rescan both systems now")
        self.btn_refresh.clicked.connect(self.request_refresh)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.status)
        top.addWidget(self.btn_refresh)
        root.addLayout(top)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.banner.setStyleSheet("color:#e08a00;")
        self.banner.hide()
        root.addWidget(self.banner)

        mid = QHBoxLayout()
        self.left, self.right = self._list(), self._list()
        mid.addWidget(self._box(LINUX, self.left), 1)
        arrows = QVBoxLayout()
        arrows.addStretch(1)
        self.btn_l2w = QPushButton("→")
        self.btn_w2l = QPushButton("←")
        self.btn_l2w.setToolTip(f"Copy the selected devices from {LINUX} to Windows")
        self.btn_w2l.setToolTip(f"Copy the selected devices from Windows to {LINUX}")
        for b in (self.btn_l2w, self.btn_w2l):
            b.setFixedSize(60, 60)
            b.setStyleSheet("font-size:26px;")
        self.btn_l2w.clicked.connect(lambda: self.sync("linux2win"))
        self.btn_w2l.clicked.connect(lambda: self.sync("win2linux"))
        arrows.addWidget(self.btn_l2w)
        arrows.addSpacing(12)
        arrows.addWidget(self.btn_w2l)
        arrows.addStretch(1)
        mid.addLayout(arrows)
        mid.addWidget(self._box("Windows", self.right), 1)
        root.addLayout(mid, 3)

        legend_row = QHBoxLayout()
        legend = QLabel("✔ in sync     → only on one system     "
                        "⚠ different keys (the arrow overwrites the other side)")
        legend.setStyleSheet("color:gray;")
        self.chk_autostart = QCheckBox("Start in tray at login")
        self.chk_autostart.setToolTip(f"Creates {AUTOSTART}")
        self.chk_autostart.toggled.connect(self.set_autostart)
        legend_row.addWidget(legend)
        legend_row.addStretch(1)
        legend_row.addWidget(self.chk_autostart)
        root.addLayout(legend_row)

        self.archive = self._list()
        arch_box = QGroupBox("Archive: never sync these and don't notify me")
        ab = QHBoxLayout(arch_box)
        ab.addWidget(self.archive, 1)
        col = QVBoxLayout()
        btn_arch = QPushButton("Archive selected ↓")
        btn_rest = QPushButton("↑ Restore selected")
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
        msg = QLabel("Enjoying bt-sync? Buy me a 1UP!")
        msg.setStyleSheet("color:gray;")
        btn_support = QPushButton("Support me ☕")
        btn_support.setToolTip(KOFI_URL)
        btn_support.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_support.clicked.connect(self.open_support)
        support.addWidget(msg)
        support.addWidget(btn_support)
        root.addLayout(support)

    def _build_tray(self):
        self.tray = None
        self.act_autostart = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip("Bluetooth Sync")
        self.menu = QMenu()
        self.menu.addAction("Open Bluetooth Sync").triggered.connect(self.show_window)
        self.menu.addAction(QIcon.fromTheme("view-refresh"), "Refresh now") \
            .triggered.connect(self.request_refresh)
        self.act_autostart = self.menu.addAction("Start in tray at login")
        self.act_autostart.setCheckable(True)
        self.act_autostart.toggled.connect(self.set_autostart)
        self.menu.addSeparator()
        self.menu.addAction("Quit").triggered.connect(self.quit_app)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self.show_window)
        self.tray.show()

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

    def show_hint(self, text):
        if text:
            self.banner.setText(text)
            self.banner.show()
        else:
            self.banner.hide()

    # ---- autostart / support
    def _sync_autostart_widgets(self):
        on = AUTOSTART.exists()
        for w in (self.chk_autostart, self.act_autostart):
            if w is not None:
                w.blockSignals(True)
                w.setChecked(on)
                w.blockSignals(False)
        self.chk_autostart.setEnabled(self.tray is not None)
        if self.tray is None:
            self.chk_autostart.setToolTip("No system tray available on this desktop")

    def set_autostart(self, on):
        try:
            if on:
                AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
                AUTOSTART.write_text(AUTOSTART_ENTRY)
            else:
                AUTOSTART.unlink(missing_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Bluetooth Sync",
                                f"Cannot change the login autostart:\n{e}")
        self._sync_autostart_widgets()

    def open_support(self):
        if not QDesktopServices.openUrl(QUrl(KOFI_URL)) and \
                not QProcess.startDetached("xdg-open", [KOFI_URL])[0]:
            self.status.setText(f"Could not open the browser: {KOFI_URL}")

    # ---- state
    def poll(self, force=False):
        try:
            mtime = STATE.stat().st_mtime
        except OSError:
            if force:
                self.status.setText("No scan available yet")
                self.show_hint("The background scan has not run yet. If this persists, "
                               f"make sure it is enabled:  {ENABLE_CMD}")
            return
        if force or mtime != self._mtime:
            self._mtime = mtime
            st = load_state()
            if st:
                self.state = st
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
            self.tray.setToolTip(f"Bluetooth Sync: {n} device(s) to sync" if n
                                 else "Bluetooth Sync: everything in sync")
            if names and not self.isVisible():
                self.tray.showMessage(
                    "Bluetooth Sync",
                    "To sync: " + ", ".join(names),
                    QSystemTrayIcon.MessageIcon.Information, 10000)

    # ---- lists
    def refresh_lists(self):
        st = self.state
        arch = self.cfg["archive"]
        by_dev = {}
        for it in st["items"]:
            by_dev.setdefault((it["adapter"], it["mac"]), set()).add(it["action"])

        for lst, side, other in ((self.left, st.get("linux", {}), st.get("windows", {})),
                                 (self.right, st.get("windows", {}), st.get("linux", {}))):
            selected = {i.data(ROLE)[0] + "|" + i.data(ROLE)[1] for i in lst.selectedItems()}
            lst.clear()
            for ad, devs in sorted(side.items()):
                for mac, info in sorted(devs.items(), key=lambda kv: kv[1]["name"].lower()):
                    if f"{ad}|{mac}" in arch:
                        continue
                    acts = by_dev.get((ad, mac), set())
                    present_other = mac in other.get(ad, {})
                    if "conflict" in acts:
                        status, mark, color, txt = "conflict", "⚠", AMBER, "different keys"
                    elif not present_other:
                        status, mark, color, txt = "only", "→", BLUE, "only here"
                    elif acts:
                        status, mark, color, txt = "partial", "→", BLUE, "partially synced"
                    else:
                        status, mark, color, txt = "sync", "✔", GREEN, "in sync"
                    row = QListWidgetItem(f"{mark}  {info['name']}\n     {mac} · "
                                          f"{kinds_text(info)} · {txt}")
                    row.setForeground(QBrush(color))
                    row.setData(ROLE, (ad, mac, status, info["name"]))
                    lst.addItem(row)
                    if f"{ad}|{mac}" in selected:
                        row.setSelected(True)

        self.archive.clear()
        for key, v in sorted(arch.items(), key=lambda kv: kv[1]["name"].lower()):
            row = QListWidgetItem(f"{v['name']}\n     {v['mac']}")
            row.setData(ROLE, key)
            self.archive.addItem(row)

        if st.get("error"):
            self.show_hint(f"⚠ Could not read Windows: {st['error']}")
        elif st.get("linux") and st.get("windows") and \
                not set(st["linux"]) & set(st["windows"]):
            self.show_hint("⚠ Linux and Windows use different Bluetooth adapters "
                           "(different adapter MAC addresses): nothing can be synced.")
        else:
            self.show_hint(None)
        self.status.setText("Last scan: " + time.strftime(
            "%H:%M:%S", time.localtime(st.get("time", 0))))

    # ---- archive
    def archive_selected(self):
        for lst in (self.left, self.right):
            for it in lst.selectedItems():
                ad, mac, _status, name = it.data(ROLE)
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
        try:
            with open(TRIGGER, "w") as f:   # watched by bt-sync-scan.path
                f.write(f"{time.time()}\n")
        except OSError as e:
            self.status.setText("Cannot start a scan")
            self.show_hint(f"Cannot request a scan ({e}). Try:  sudo systemd-tmpfiles "
                           f"--create bt-sync.conf  and then:  {ENABLE_CMD}")
            return
        self._wait_since = time.time()
        self.btn_refresh.setEnabled(False)
        self.status.setText("Scanning…")
        QTimer.singleShot(90_000, self._refresh_timeout)

    def _refresh_timeout(self):
        if self._wait_since:
            self._wait_since = None
            self.btn_refresh.setEnabled(True)
            self.status.setText("The scan did not answer")
            self.show_hint("The scan service did not answer. Make sure it is enabled:  "
                           f"{ENABLE_CMD}")

    # ---- sync
    def sync(self, direction):
        lst = self.left if direction == "linux2win" else self.right
        sel = [i.data(ROLE) for i in lst.selectedItems() if i.data(ROLE)[2] != "sync"]
        if not sel:
            QMessageBox.information(self, "Bluetooth Sync",
                                    "Select at least one device that is not in sync yet "
                                    "in the list the arrow starts from.")
            return
        conflicts = [s[3] for s in sel if s[2] == "conflict"]
        if conflicts:
            dest = "Windows" if direction == "linux2win" else LINUX
            ok = QMessageBox.question(
                self, "Overwrite the keys?",
                f"The keys of {', '.join(conflicts)} differ between the two systems.\n"
                f"The ones on {dest} will be overwritten. Continue?")
            if ok != QMessageBox.StandardButton.Yes:
                return
        reqs = [{"adapter": s[0], "mac": s[1], "direction": direction} for s in sel]
        self.run_helper(reqs)

    def set_busy(self, busy):
        for b in (self.btn_l2w, self.btn_w2l):
            b.setEnabled(not busy)
        if busy:
            self.status.setText("Syncing…")

    def run_helper(self, reqs):
        self.set_busy(True)
        p = QProcess(self)
        self._proc = p
        p.finished.connect(self._helper_done)
        p.start("pkexec", [HELPER, "apply-json"])
        if not p.waitForStarted(5000):
            self.set_busy(False)
            QMessageBox.warning(self, "Bluetooth Sync",
                                "Could not start pkexec. Is polkit installed and is a "
                                "polkit authentication agent running?")
            return
        p.write(json.dumps({"requests": reqs}).encode())
        p.closeWriteChannel()

    def _helper_done(self, code, _status):
        p = self._proc
        out = bytes(p.readAllStandardOutput()).decode(errors="replace").strip()
        err = bytes(p.readAllStandardError()).decode(errors="replace").strip()
        self.set_busy(False)
        if code in (126, 127) and not out:
            self.status.setText("Authentication cancelled or not authorized")
            return
        try:
            res = json.loads(out.splitlines()[-1])
            ok, errors = res["ok"], res["errors"]
        except (ValueError, IndexError, KeyError, TypeError):
            QMessageBox.warning(self, "Bluetooth Sync",
                                f"Unexpected answer from {HELPER} (exit code {code}).\n\n"
                                f"{err or out or 'no output'}")
            return
        done = [line for line in ok if not line.startswith("backup")]
        if errors:
            text = ""
            if done:
                text += "Done:\n• " + "\n• ".join(done) + "\n\n"
            text += "Problems:\n• " + "\n• ".join(errors)
            QMessageBox.warning(self, "Bluetooth Sync", text)
        if done:
            self.status.setText("Done: " + "; ".join(done))
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

    win = MainWindow()

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


if __name__ == "__main__":
    sys.exit(main())
