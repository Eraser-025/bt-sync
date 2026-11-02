# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. See the LICENSE file for details.
"""
bt-sync - keep Bluetooth pairing keys in sync between Linux (BlueZ) and
Windows on a dual-boot machine. This is the privileged backend (runs as root).

commands:
  setup         enable the background scan (bt-sync-scan.timer/.path).
  scan          mount Windows read-only, compare the keys and write
                /var/lib/bt-sync/state.json (device names and differences
                only, never the keys). Run periodically by bt-sync-scan.timer.
  status        print the differences.
  apply         interactive sync from the terminal.
  export-keys   print a backup of the Linux pairing keys (JSON) on stdout.
  import-keys   restore a backup read from stdin into Linux.
  apply-json    used by bt-sync-gui through pkexec (requests on stdin).
"""
import argparse
import configparser
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, os_name
from . import i18n
from .i18n import N_, _

CONFIG = Path("/etc/bt-sync.conf")
STATE_DIR = Path("/var/lib/bt-sync")
STATE = STATE_DIR / "state.json"
IGNORED = STATE_DIR / "ignored.json"      # used by the terminal mode only
BACKUPS = STATE_DIR / "backups"
RUN_DIR = Path("/run/bt-sync")
LOCK = RUN_DIR / "lock"
MOUNTPOINT = RUN_DIR / "win"
BT_DIR = Path("/var/lib/bluetooth")
HIVE_PATH = "Windows/System32/config/SYSTEM"
KEYFILE_FORMAT = "bt-sync-keys"
NEWER_THRESHOLD = 120       # seconds: closer timestamps give no suggestion

REG_BINARY, REG_DWORD, REG_QWORD = 3, 4, 11
HEX12 = re.compile(r"^[0-9a-fA-F]{12}$")
HEX32 = re.compile(r"^[0-9A-F]{32}$")
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")

FAST_STARTUP_HINT = N_(
    "This usually means Windows was hibernated or Fast Startup is enabled: in "
    "Windows run 'powercfg /h off' from an administrator prompt, then use "
    "Shut down (not Restart) before booting Linux.")

LINUX = os_name()


class BtSyncError(Exception):
    """An expected failure, with a translatable message for the user.

    The message is a list of (msgid, args) parts, so that it can be stored
    untranslated in the state file and translated later by the GUI."""

    def __init__(self, msgid, **args):
        super().__init__(msgid)
        self.parts = [(msgid, args)]

    def add(self, msgid, **args):
        self.parts.append((msgid, args))
        return self

    def __str__(self):
        return i18n.render(self.parts)

    def to_json(self):
        return [{"id": m, "args": a} for m, a in self.parts]


def error_json(e):
    if isinstance(e, BtSyncError):
        return e.to_json()
    return [{"id": N_("unexpected error: {error}"), "args": {"error": repr(e)}}]


def warn(text):
    print(_("bt-sync: warning: {message}").format(message=text), file=sys.stderr)


# ------------------------------------------------------------------ helpers
def load_config():
    cfg = {"reverse_irk": False, "keep_backups": 5}
    cp = configparser.ConfigParser()
    try:
        cp.read(CONFIG)
        if cp.has_section("bt-sync"):
            s = cp["bt-sync"]
            cfg["reverse_irk"] = s.getboolean("reverse_irk", fallback=False)
            cfg["keep_backups"] = max(1, s.getint("keep_backups", fallback=5))
    except (configparser.Error, ValueError) as e:
        warn(_("ignoring invalid {path}: {error}").format(path=CONFIG, error=e))
    return cfg


CFG = load_config()


def fmt_mac(raw):
    raw = raw.upper()
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


def raw_mac(mac):
    return mac.replace(":", "").lower()


def rev_hex(h):
    return bytes.fromhex(h)[::-1].hex().upper()


def clean_name(name, fallback):
    """Device names come from the devices themselves: no control characters
    (a newline would corrupt the BlueZ info file)."""
    name = "".join(c if c.isprintable() else " " for c in str(name)).strip()
    return name[:248] or fallback


def filetime_to_unix(ft):
    return (ft - 116444736000000000) / 1e7 if ft else None


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def require_root():
    if os.geteuid() != 0:
        raise BtSyncError(N_("this command must be run as root (try: sudo bt-sync ...)."))


def write_json_atomic(path, obj, mode=0o644):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o755)
    # The backups are copies of the Windows SYSTEM hive: root only.
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    os.chmod(BACKUPS, 0o700)
    for f in BACKUPS.iterdir():
        os.chmod(f, 0o600)


@contextlib.contextmanager
def exclusive_lock(timeout=120):
    """Only one bt-sync operation at a time (they share the mount point)."""
    RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(LOCK, "w") as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise BtSyncError(N_("another bt-sync operation is still running; "
                                         "try again in a minute.")) from None
                time.sleep(0.5)
        yield


def ci_find(base, rel):
    """Resolve a path case-insensitively."""
    cur = Path(base)
    for part in rel.split("/"):
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        match = next((n for n in names if n.lower() == part.lower()), None)
        if match is None:
            return None
        cur = cur / match
    return cur


def is_rw(mp):
    r = run(["findmnt", "-no", "OPTIONS", str(mp)])
    return "rw" in r.stdout.strip().split(",")


# ------------------------------------------------------ mounting Windows
def _flatten(devs):
    for d in devs:
        yield d
        yield from _flatten(d.get("children") or [])


class WinMount:
    """Find the Windows NTFS partition, mount it if needed, unmount at the end."""

    def __init__(self, rw=False):
        self.rw = rw
        self.root = None
        self.hive = None
        self.writable = False
        self._ours = False

    def __enter__(self):
        RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        MOUNTPOINT.mkdir(exist_ok=True)
        if os.path.ismount(MOUNTPOINT):     # left over by an interrupted run
            self._umount()
        r = run(["lsblk", "-J", "-p", "-o", "PATH,FSTYPE,MOUNTPOINT"])
        if r.returncode != 0:
            raise BtSyncError(N_("lsblk failed: {error}"), error=r.stderr.strip())
        devs = list(_flatten(json.loads(r.stdout).get("blockdevices") or []))
        parts = [d for d in devs if (d.get("fstype") or "") in ("ntfs", "ntfs3")]
        mount_errors = []
        for d in parts:
            mp = d.get("mountpoint")
            if mp:
                root, ours = Path(mp), False
            else:
                root, ours = self._mount(d["path"], mount_errors), True
                if root is None:
                    continue
            hive = ci_find(root, HIVE_PATH)
            if hive:
                self.root, self.hive, self._ours = root, hive, ours
                self.writable = is_rw(root)
                return self
            if ours:
                self._umount()

        err = BtSyncError(N_("Windows partition not found: no NTFS partition "
                             "contains {path}."), path=HIVE_PATH)
        if any((d.get("fstype") or "").lower() == "bitlocker" for d in devs):
            err.add(N_("A BitLocker-encrypted partition was detected: bt-sync cannot "
                       "read it. Turn off BitLocker (or device encryption) on the "
                       "Windows system drive."))
        elif not parts:
            err.add(N_("No NTFS partitions were detected at all."))
        if mount_errors:
            err.add(N_("Some NTFS partitions could not be mounted ({errors})."),
                    errors="; ".join(mount_errors))
            err.add(FAST_STARTUP_HINT)
        raise err

    def _mount(self, dev, errors):
        last = ""
        for mode in (["rw", "ro"] if self.rw else ["ro"]):
            for fs in ("ntfs3", "ntfs-3g"):
                r = run(["mount", "-t", fs, "-o", mode, dev, str(MOUNTPOINT)])
                if r.returncode == 0:
                    return MOUNTPOINT
                last = r.stderr.strip() or f"mount -t {fs}: exit {r.returncode}"
        errors.append(f"{dev}: {last}")
        return None

    def _umount(self):
        run(["sync"])
        if run(["umount", str(MOUNTPOINT)]).returncode != 0:
            run(["umount", "-l", str(MOUNTPOINT)])

    def __exit__(self, *exc):
        if self._ours:
            self._umount()
        return False


# ------------------------------------------------------- Windows registry
class WinReg:
    def __init__(self, hive_path, write=False):
        try:
            import hivex  # late import: only needed here
        except ImportError:
            raise BtSyncError(N_("the Python bindings of hivex are missing: install "
                                 "'hivex' (Arch) or 'python3-hivex' (Debian, Ubuntu, "
                                 "Fedora).")) from None
        try:
            self.h = hivex.Hivex(str(hive_path), write=write)
        except Exception as e:
            raise BtSyncError(N_("cannot open the Windows registry {path}: {error}"),
                              path=str(hive_path), error=str(e)) from None
        root = self.h.root()
        sel = self._child(root, "Select")
        cur_v = self._val(sel, "Current") if sel else None
        if not cur_v:
            raise BtSyncError(N_("unexpected Windows registry layout "
                                 "(SYSTEM\\Select\\Current is missing)."))
        cur = int.from_bytes(cur_v[1][:4], "little")
        path = [f"ControlSet{cur:03d}", "Services", "BTHPORT", "Parameters"]
        node = root
        for i, part in enumerate(path):
            node = self._child(node, part)
            if node is None:
                raise BtSyncError(N_("Windows registry key not found: {key}. Has "
                                     "Bluetooth ever been used on this Windows install?"),
                                  key="SYSTEM\\" + "\\".join(path[:i + 1]))
        self.keys = self._child(node, "Keys")
        self.devices = self._child(node, "Devices")
        if self.keys is None:
            raise BtSyncError(N_("no Bluetooth pairing keys in the Windows registry "
                                 "(pair at least one device in Windows first)."))

    def _child(self, node, name):
        c = self.h.node_get_child(node, name)
        return c if c else None   # hivex returns None/0 when the child is missing

    def _val(self, node, name):
        v = self.h.node_get_value(node, name)
        return self.h.value_value(v) if v else None

    def _time(self, node):
        """Last write time of a registry key (Unix time), if available."""
        try:
            return filetime_to_unix(self.h.node_timestamp(node))
        except Exception:
            return None

    def _name(self, rawmac):
        if self.devices is None:
            return None
        dn = self._child(self.devices, rawmac)
        if dn is None:
            return None
        v = self._val(dn, "Name")
        return v[1].rstrip(b"\0").decode("utf-8", "replace") if v else None

    def read_all(self):
        res = {}
        for ad in self.h.node_children(self.keys):
            adname = self.h.node_name(ad)
            if not HEX12.match(adname):
                continue
            devs = res.setdefault(fmt_mac(adname), {})

            def dev(rawname):
                mac = fmt_mac(rawname)
                return devs.setdefault(mac, {
                    "name": clean_name(self._name(rawname.lower()) or mac, mac),
                    "classic": None, "le": None, "times": {}})

            # BR/EDR keys are values of the adapter key: the best timestamp we
            # have is the last time any of them changed.
            ad_time = self._time(ad)
            for v in self.h.node_values(ad):
                name = self.h.value_key(v)
                if HEX12.match(name):
                    data = self.h.value_value(v)[1]
                    if len(data) == 16:
                        d = dev(name)
                        d["classic"] = data.hex().upper()
                        d["times"]["classic"] = ad_time
            for ch in self.h.node_children(ad):       # BLE keys: one subkey each
                name = self.h.node_name(ch)
                if not HEX12.match(name):
                    continue
                ltk = self._val(ch, "LTK")
                if not ltk or len(ltk[1]) != 16:
                    continue
                kl = self._val(ch, "KeyLength")
                ediv = self._val(ch, "EDIV")
                erand = self._val(ch, "ERand")
                irk = self._val(ch, "IRK")
                at = self._val(ch, "AddressType")
                first = int(name[:2], 16)
                if at is not None:
                    atype = "static" if int.from_bytes(at[1][:4], "little") else "public"
                else:
                    atype = "static" if (first & 0xC0) == 0xC0 else "public"
                irk_hex = irk[1].hex().upper() if irk and len(irk[1]) == 16 else None
                if irk_hex and CFG["reverse_irk"]:
                    irk_hex = rev_hex(irk_hex)
                d = dev(name)
                d["le"] = {
                    "ltk": ltk[1].hex().upper(),
                    "key_length": int.from_bytes(kl[1][:4], "little") if kl else 16,
                    "ediv": int.from_bytes(ediv[1][:4], "little") if ediv else 0,
                    "erand": int.from_bytes(erand[1][:8], "little") if erand else 0,
                    "irk": irk_hex,
                    "addr_type": atype,
                }
                d["times"]["le"] = self._time(ch)
        return res

    # ---- writing
    def _adapter(self, adapter):
        ad = self._child(self.keys, raw_mac(adapter))
        if ad is None:
            raise BtSyncError(N_("Bluetooth adapter {adapter} is not in the Windows "
                                 "registry."), adapter=adapter)
        return ad

    def write_classic(self, adapter, mac, key_hex):
        ad = self._adapter(adapter)
        self.h.node_set_value(ad, {"key": raw_mac(mac), "t": REG_BINARY,
                                   "value": bytes.fromhex(key_hex)})

    def write_le(self, adapter, mac, le):
        ad = self._adapter(adapter)
        node = self._child(ad, raw_mac(mac)) or self.h.node_add_child(ad, raw_mac(mac))
        sv = self.h.node_set_value
        sv(node, {"key": "LTK", "t": REG_BINARY, "value": bytes.fromhex(le["ltk"])})
        sv(node, {"key": "KeyLength", "t": REG_DWORD,
                  "value": int(le["key_length"]).to_bytes(4, "little")})
        sv(node, {"key": "ERand", "t": REG_QWORD,
                  "value": int(le["erand"]).to_bytes(8, "little")})
        sv(node, {"key": "EDIV", "t": REG_DWORD,
                  "value": int(le["ediv"]).to_bytes(4, "little")})
        sv(node, {"key": "AddressType", "t": REG_DWORD,
                  "value": (1 if le["addr_type"] == "static" else 0).to_bytes(4, "little")})
        if le.get("irk"):
            irk = rev_hex(le["irk"]) if CFG["reverse_irk"] else le["irk"]
            sv(node, {"key": "IRK", "t": REG_BINARY, "value": bytes.fromhex(irk)})

    def commit(self):
        try:
            self.h.commit(None)
        except Exception as e:
            raise BtSyncError(N_("writing the Windows registry failed: {error}"),
                              error=str(e)) from None


# ----------------------------------------------------------------- BlueZ
def _cp():
    cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
    cp.optionxform = str
    return cp


def _read_info(name, info):
    cp = _cp()
    cp.read(info)
    mtime = info.stat().st_mtime
    entry = {"name": clean_name(cp.get("General", "Name", fallback=name), name),
             "classic": None, "le": None, "times": {"classic": mtime, "le": mtime}}
    if cp.has_section("LinkKey"):
        entry["classic"] = cp.get("LinkKey", "Key", fallback="").upper() or None
    if cp.has_section("LongTermKey"):
        s = cp["LongTermKey"]
        entry["le"] = {
            "ltk": s.get("Key", "").upper(),
            "key_length": int(s.get("EncSize", 16)),
            "ediv": int(s.get("EDiv", 0)),
            "erand": int(s.get("Rand", 0)),
            "irk": cp.get("IdentityResolvingKey", "Key", fallback="").upper() or None,
            "addr_type": cp.get("General", "AddressType", fallback="public"),
        }
    return entry


def linux_adapters():
    if not BT_DIR.exists():
        return []
    return sorted(a.name for a in BT_DIR.iterdir() if a.is_dir() and MAC_RE.match(a.name))


def linux_read():
    res = {}
    for adname in linux_adapters():
        ad = BT_DIR / adname
        devs = res.setdefault(adname, {})
        for d in ad.iterdir():
            info = d / "info"
            if not (d.is_dir() and MAC_RE.match(d.name) and info.exists()):
                continue
            try:
                entry = _read_info(d.name, info)
            except (configparser.Error, ValueError, OSError) as e:
                warn(_("skipping unreadable {path}: {error}").format(path=info, error=e))
                continue
            if entry["classic"] or entry["le"]:
                devs[d.name] = entry
    return res


def linux_write(adapter, mac, name, kind, data):
    d = BT_DIR / adapter / mac
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    info = d / "info"
    cp = _cp()
    if info.exists():
        cp.read(info)
    if not cp.has_section("General"):
        cp.add_section("General")
    g = cp["General"]
    if not g.get("Name"):
        g["Name"] = clean_name(name, mac)
    g["Trusted"] = "true"
    g.setdefault("Blocked", "false")
    tech = g.get("SupportedTechnologies", "")
    want = "BR/EDR;" if kind == "classic" else "LE;"
    if want not in tech:
        g["SupportedTechnologies"] = tech + want
    if kind == "classic":
        old = cp["LinkKey"] if cp.has_section("LinkKey") else {}
        cp["LinkKey"] = {"Key": data, "Type": old.get("Type", "4"),
                         "PINLength": old.get("PINLength", "0")}
    else:
        g["AddressType"] = data["addr_type"]
        secure_conn = data["ediv"] == 0 and data["erand"] == 0
        cp["LongTermKey"] = {
            "Key": data["ltk"],
            "Authenticated": "2" if secure_conn else "0",
            "EncSize": str(data["key_length"]),
            "EDiv": str(data["ediv"]),
            "Rand": str(data["erand"]),
        }
        if data.get("irk"):
            cp["IdentityResolvingKey"] = {"Key": data["irk"]}
    tmp = info.with_name("info.tmp")
    with open(tmp, "w") as f:
        cp.write(f, space_around_delimiters=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, info)


def write_linux(entries, errors):
    """Write [(adapter, mac, name, kind, data)] into BlueZ, which must be
    stopped meanwhile because it rewrites the info files when it stops."""
    if not entries:
        return
    r = run(["systemctl", "stop", "bluetooth.service"])
    if r.returncode != 0:
        raise BtSyncError(N_("could not stop bluetooth.service: {error}"),
                          error=r.stderr.strip())
    try:
        for e in entries:
            linux_write(*e)
    finally:
        r = run(["systemctl", "start", "bluetooth.service"])
        if r.returncode != 0:
            errors.append(_("could not restart bluetooth.service, run 'sudo systemctl "
                            "start bluetooth': {error}").format(error=r.stderr.strip()))


# ------------------------------------------------------------- comparison
def _sig(kind, v):
    return v if kind == "classic" else (v["ltk"], v["ediv"], v["erand"])


def load_ignored():
    try:
        return set(json.loads(IGNORED.read_text()))
    except (OSError, ValueError):
        return set()


def newer_side(wt, lt):
    if wt is None or lt is None or abs(wt - lt) < NEWER_THRESHOLD:
        return None
    return "windows" if wt > lt else "linux"


def diff(win, lin, use_ignored=False):
    ignored = load_ignored() if use_ignored else set()
    items = []
    for ad in sorted(set(win) & set(lin)):
        for mac in sorted(set(win[ad]) | set(lin[ad])):
            w, l = win[ad].get(mac), lin[ad].get(mac)
            for kind in ("classic", "le"):
                wk = w and w[kind]
                lk = l and l[kind]
                if wk and not lk:
                    action = "win2linux"
                elif lk and not wk:
                    action = "linux2win"
                elif wk and lk and _sig(kind, wk) != _sig(kind, lk):
                    action = "conflict"
                else:
                    continue
                if f"{ad}|{mac}|{kind}" in ignored:
                    continue
                wt = w["times"].get(kind) if wk else None
                lt = l["times"].get(kind) if lk else None
                items.append({"adapter": ad, "mac": mac, "kind": kind, "action": action,
                              "name": (w or l)["name"], "win": wk, "linux": lk,
                              "win_time": wt, "linux_time": lt,
                              "newer": newer_side(wt, lt) if action == "conflict" else None})
    return items


# ----------------------------------------------------------------- state
STATE_ITEM_KEYS = ("adapter", "mac", "kind", "action", "name",
                   "win_time", "linux_time", "newer")


def summarize(devs):
    """Summary WITHOUT keys: the state file is world-readable."""
    return {ad: {mac: {"name": d["name"], "classic": bool(d["classic"]), "le": bool(d["le"])}
                 for mac, d in ds.items()} for ad, ds in devs.items()}


def save_state(win, lin, items, error=None):
    ensure_dirs()
    write_json_atomic(STATE, {
        "version": 2, "time": time.time(), "error": error,
        "linux_name": LINUX,
        "linux": summarize(lin), "windows": summarize(win),
        "items": [{k: i[k] for k in STATE_ITEM_KEYS} for i in items],
    })


def refresh_state():
    """Rescan both systems and rewrite the state file. Never raises."""
    lin = linux_read()
    try:
        with WinMount(rw=False) as wm:
            win = WinReg(wm.hive).read_all()
    except Exception as e:  # the scan service must not fail loudly
        save_state({}, lin, [], error=error_json(e))
        print(_("bt-sync: scan failed: {error}").format(
            error=e if isinstance(e, BtSyncError) else repr(e)), file=sys.stderr)
        return False
    save_state(win, lin, diff(win, lin))
    return True


# ------------------------------------------------------------------ sync
def kind_label(kind):
    return "Classic" if kind == "classic" else "LE"


def do_sync(wm, to_linux, to_win, errors):
    """Copy the keys. Returns (things done, backup path or None)."""
    log, bak = [], None
    write_linux([(it["adapter"], it["mac"], it["name"], it["kind"], it["win"])
                 for it in to_linux], errors)
    log += [_("imported into {system}: {device} ({kind})").format(
        system=LINUX, device=it["name"], kind=kind_label(it["kind"])) for it in to_linux]
    if to_win:
        ensure_dirs()
        bak = BACKUPS / f"SYSTEM.{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copyfile(wm.hive, bak)
        os.chmod(bak, 0o600)
        for old in sorted(BACKUPS.glob("SYSTEM.*"))[:-CFG["keep_backups"]]:
            old.unlink()
        wreg = WinReg(wm.hive, write=True)
        for it in to_win:
            if it["kind"] == "classic":
                wreg.write_classic(it["adapter"], it["mac"], it["linux"])
            else:
                wreg.write_le(it["adapter"], it["mac"], it["linux"])
        wreg.commit()
        del wreg
        log += [_("exported to Windows: {device} ({kind})").format(
            device=it["name"], kind=kind_label(it["kind"])) for it in to_win]
    return log, (str(bak) if bak else None)


def readonly_msg(wm):
    if wm._ours:
        text = _("Windows is mounted read-only.")
    else:
        text = _("Windows is mounted read-only (it is already mounted at {path}).") \
            .format(path=wm.root)
    return text + " " + _(FAST_STARTUP_HINT)


def cmd_setup(_args):
    """Create the state directory and enable the background scan (the GUI
    offers this when the units are disabled, e.g. after a Fedora/Bazzite or
    AUR install, where packages do not enable services)."""
    require_root()
    run(["systemd-tmpfiles", "--create", "bt-sync.conf"])
    r = run(["systemctl", "enable", "--now", "bt-sync-scan.timer", "bt-sync-scan.path"])
    if r.returncode != 0:
        raise BtSyncError(N_("could not enable the background scan: {error}"),
                          error=r.stderr.strip())
    run(["systemctl", "start", "--no-block", "bt-sync-scan.service"])
    print(_("Background scan enabled."))
    return 0


def cmd_scan(_args):
    require_root()
    try:
        with exclusive_lock(timeout=30):
            ok = refresh_state()
    except BtSyncError:
        print(_("bt-sync: another operation is running, scan skipped."))
        return 0
    print(_("Scan completed.") if ok else _("Scan completed with errors."))
    return 0


def _parse_requests(raw):
    try:
        reqs = json.loads(raw)["requests"]
    except (ValueError, KeyError, TypeError):
        raise BtSyncError(N_("invalid request from the GUI.")) from None
    if not isinstance(reqs, list):
        raise BtSyncError(N_("invalid request from the GUI."))
    for r in reqs:
        if not (isinstance(r, dict)
                and MAC_RE.match(str(r.get("adapter")))
                and MAC_RE.match(str(r.get("mac")))
                and r.get("direction") in ("win2linux", "linux2win")):
            raise BtSyncError(N_("invalid request from the GUI."))
    return reqs


def cmd_apply_json(_args):
    """Called by the GUI through pkexec. Only (adapter, mac, direction) are
    accepted: the keys are always re-read here as root, never taken from
    the caller. Always prints one JSON line."""
    result = {"ok": [], "errors": [], "backup": None}
    try:
        require_root()
        reqs = _parse_requests(sys.stdin.read())
        with exclusive_lock():
            try:
                with WinMount(rw=True) as wm:
                    win = WinReg(wm.hive).read_all()
                    items = diff(win, linux_read())
                    to_linux, to_win = [], []
                    for r in reqs:
                        found = False
                        for it in items:
                            if (it["adapter"], it["mac"]) != (r["adapter"], r["mac"]):
                                continue
                            if r["direction"] == "win2linux" and \
                                    it["action"] in ("win2linux", "conflict"):
                                to_linux.append(it)
                                found = True
                            elif r["direction"] == "linux2win" and \
                                    it["action"] in ("linux2win", "conflict"):
                                found = True
                                if wm.writable:
                                    to_win.append(it)
                                else:
                                    result["errors"].append(f"{it['name']}: {readonly_msg(wm)}")
                        if not found:
                            result["errors"].append(_(
                                "{device}: nothing to copy in that direction (already "
                                "in sync?).").format(device=r["mac"]))
                    result["ok"], result["backup"] = do_sync(wm, to_linux, to_win,
                                                             result["errors"])
            finally:
                refresh_state()
    except BtSyncError as e:
        result["errors"].append(str(e))
    except Exception as e:
        result["errors"].append(i18n.render([(N_("unexpected error: {error}"),
                                              {"error": repr(e)})]))
    print(json.dumps(result))
    return 0


# ------------------------------------------------- key backup (export/import)
def cmd_export_keys(_args):
    require_root()
    lin = linux_read()
    out = {"format": KEYFILE_FORMAT, "version": 1,
           "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "system": LINUX,
           "adapters": {ad: {mac: {k: d[k] for k in ("name", "classic", "le")}
                             for mac, d in devs.items()}
                        for ad, devs in lin.items() if devs}}
    json.dump(out, sys.stdout, indent=1)
    print()
    if sys.stdout.isatty():
        warn(_("the output contains secret pairing keys: keep it private."))
    return 0


def _int_in(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def validate_keyfile(obj):
    """Strictly validate a key backup; returns {adapter: {mac: entry}}."""
    bad = BtSyncError(N_("this is not a valid bt-sync key backup file."))
    if not (isinstance(obj, dict) and obj.get("format") == KEYFILE_FORMAT
            and obj.get("version") == 1 and isinstance(obj.get("adapters"), dict)):
        raise bad
    out = {}
    for ad, devs in obj["adapters"].items():
        if not (MAC_RE.match(str(ad)) and isinstance(devs, dict)):
            raise bad
        for mac, d in devs.items():
            if not (MAC_RE.match(str(mac)) and isinstance(d, dict)):
                raise bad
            classic, le = d.get("classic"), d.get("le")
            if classic is not None and not (isinstance(classic, str) and HEX32.match(classic)):
                raise bad
            if le is not None:
                if not (isinstance(le, dict)
                        and isinstance(le.get("ltk"), str) and HEX32.match(le["ltk"])
                        and _int_in(le.get("key_length"), 7, 16)
                        and _int_in(le.get("ediv"), 0, 0xFFFF)
                        and _int_in(le.get("erand"), 0, 0xFFFFFFFFFFFFFFFF)
                        and le.get("addr_type") in ("public", "static")
                        and (le.get("irk") is None
                             or (isinstance(le["irk"], str) and HEX32.match(le["irk"])))):
                    raise bad
                le = {k: le[k] for k in ("ltk", "key_length", "ediv", "erand",
                                         "irk", "addr_type")}
            if classic or le:
                out.setdefault(ad, {})[mac] = {
                    "name": clean_name(d.get("name") or mac, mac),
                    "classic": classic, "le": le}
    return out


def import_plan(keys, lin, adapters):
    plan = {"new": [], "same": [], "different": [], "missing_adapters": []}
    for ad, devs in sorted(keys.items()):
        if ad not in adapters:
            plan["missing_adapters"].append(ad)
            continue
        for mac, d in sorted(devs.items()):
            for kind in ("classic", "le"):
                v = d[kind]
                if not v:
                    continue
                cur = lin.get(ad, {}).get(mac, {}).get(kind)
                group = "new" if not cur else \
                    "same" if _sig(kind, cur) == _sig(kind, v) else "different"
                plan[group].append({"adapter": ad, "mac": mac, "name": d["name"],
                                    "kind": kind, "data": v})
    return plan


def cmd_import_keys(args):
    as_json = args.json
    result = {"new": [], "same": [], "different": [], "missing_adapters": [],
              "ok": [], "errors": []}
    try:
        require_root()
        raw = sys.stdin.read(4 * 1024 * 1024)
        try:
            obj = json.loads(raw)
        except ValueError:
            raise BtSyncError(N_("this is not a valid bt-sync key backup file.")) from None
        keys = validate_keyfile(obj)
        with exclusive_lock():
            plan = import_plan(keys, linux_read(), set(linux_adapters()))
            for g in ("new", "same", "different"):
                result[g] = [f"{p['name']} ({kind_label(p['kind'])})" for p in plan[g]]
            result["missing_adapters"] = plan["missing_adapters"]
            if not args.dry_run:
                todo = plan["new"] + (plan["different"] if args.overwrite else [])
                write_linux([(p["adapter"], p["mac"], p["name"], p["kind"], p["data"])
                             for p in todo], result["errors"])
                result["ok"] = [_("imported into {system}: {device} ({kind})").format(
                    system=LINUX, device=p["name"], kind=kind_label(p["kind"]))
                    for p in todo]
                if todo:
                    refresh_state()
    except BtSyncError as e:
        if not as_json:
            raise
        result["errors"].append(str(e))

    if as_json:
        print(json.dumps(result))
        return 0
    if result["missing_adapters"]:
        print(_("Adapters not present on this computer (skipped): {list}").format(
            list=", ".join(result["missing_adapters"])))
    print(_("Already present: {n}").format(n=len(result["same"])))
    for g, label in (("new", _("New")), ("different", _("Different from this computer"))):
        for line in result[g]:
            print(f"  {label}: {line}")
    if args.dry_run:
        print(_("Dry run: nothing was changed."))
    elif result["different"] and not args.overwrite:
        print(_("Keys that differ were kept; use --overwrite to replace them."))
    for line in result["ok"]:
        print("  " + line)
    for line in result["errors"]:
        print(_("error: {message}").format(message=line), file=sys.stderr)
    return 1 if result["errors"] else 0


# ----------------------------------------------------- terminal (CLI) mode
def label(action):
    return {
        "win2linux": _("on Windows, missing on {system}"),
        "linux2win": _("on {system}, missing on Windows"),
        "conflict": _("DIFFERENT keys on the two systems"),
    }[action].format(system=LINUX)


def newer_text(item):
    if item.get("newer") == "windows":
        return _("the Windows key is newer")
    if item.get("newer") == "linux":
        return _("the {system} key is newer").format(system=LINUX)
    return ""


def cmd_status(_args):
    require_root()
    with exclusive_lock():
        with WinMount(rw=False) as wm:
            win = WinReg(wm.hive).read_all()
    items = diff(win, linux_read(), use_ignored=True)
    if not items:
        print(_("Everything is in sync."))
    for i in items:
        hint = newer_text(i)
        print(f"{i['action']:10} {kind_label(i['kind']):7} {i['mac']}  {i['name']}"
              + (f"  ({hint})" if hint else ""))
    return 0


def _ask(item, can_write_win):
    print(f"\n* {item['name']}  [{item['mac']}]  ({kind_label(item['kind'])})")
    print("  " + label(item["action"]))
    if newer_text(item):
        print("  " + newer_text(item))
    opts = {"n": _("skip"), "i": _("ignore from now on")}
    if item["action"] == "win2linux":
        opts = {"y": _("import into {system}").format(system=LINUX), **opts}
    elif item["action"] == "linux2win":
        if can_write_win:
            opts = {"y": _("export to Windows"), **opts}
        else:
            print("  " + _("(Windows is mounted read-only: cannot export)"))
    else:
        opts = {"w": f"Windows -> {LINUX}", **opts}
        if can_write_win:
            opts = {"l": f"{LINUX} -> Windows", **opts}
    while True:
        try:
            a = input("  " + " / ".join(f"[{k}] {v}" for k, v in opts.items()) + " > ")
        except EOFError:
            raise KeyboardInterrupt from None
        a = a.strip().lower()
        if a in opts:
            return a


def cmd_apply(_args):
    require_root()
    errors = []
    with exclusive_lock():
        with WinMount(rw=True) as wm:
            win = WinReg(wm.hive).read_all()
            items = diff(win, linux_read(), use_ignored=True)
            if not items:
                print(_("Everything is in sync."))
                return 0
            if not wm.writable:
                print(readonly_msg(wm))
            to_linux, to_win, ignore = [], [], []
            for it in items:
                a = _ask(it, wm.writable)
                if a == "i":
                    ignore.append(f"{it['adapter']}|{it['mac']}|{it['kind']}")
                elif a == "w" or (a == "y" and it["action"] == "win2linux"):
                    to_linux.append(it)
                elif a in ("y", "l"):
                    to_win.append(it)
            if ignore:
                ensure_dirs()
                write_json_atomic(IGNORED, sorted(load_ignored() | set(ignore)))
            log, bak = do_sync(wm, to_linux, to_win, errors)
            if bak:
                print("  " + _("backup of the Windows registry saved to {path}").format(path=bak))
            for line in log:
                print("  " + line)
            for line in errors:
                print("  " + _("error: {message}").format(message=line), file=sys.stderr)
        refresh_state()
    return 1 if errors else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="bt-sync", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--lang", help=argparse.SUPPRESS)   # language for the GUI helper
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")
    for name, fn, hlp in (
            ("setup", cmd_setup, "enable the background scan (systemd timer)"),
            ("scan", cmd_scan, "rescan both systems and update the state file"),
            ("status", cmd_status, "print the differences"),
            ("apply", cmd_apply, "interactive sync"),
            ("export-keys", cmd_export_keys, "print a backup of the Linux pairing keys"),
            ("import-keys", cmd_import_keys, "restore a key backup read from stdin"),
            ("apply-json", cmd_apply_json, "backend for the GUI (reads JSON on stdin)")):
        sp = sub.add_parser(name, help=hlp)
        sp.set_defaults(fn=fn)
        if name == "import-keys":
            sp.add_argument("--dry-run", action="store_true",
                            help="only show what would be imported")
            sp.add_argument("--overwrite", action="store_true",
                            help="also replace keys that differ")
            sp.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.lang:
        i18n.set_language(args.lang)
    try:
        return args.fn(args)
    except BtSyncError as e:
        print(_("bt-sync: error: {message}").format(message=e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n" + _("Aborted, nothing was changed."), file=sys.stderr)
        return 130
