#!/usr/bin/python3
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
  scan        mount Windows read-only, compare the keys and write
              /var/lib/bt-sync/state.json (device names and differences only,
              never the keys). Run periodically by bt-sync-scan.timer.
  status      print the differences.
  apply       interactive sync from the terminal.
  apply-json  used by bt-sync-gui through pkexec (requests on stdin).
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

__version__ = "1.0.0"

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

REG_BINARY, REG_DWORD, REG_QWORD = 3, 4, 11
HEX12 = re.compile(r"^[0-9a-fA-F]{12}$")
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")

FAST_STARTUP_HINT = (
    "This usually means Windows was hibernated or Fast Startup is enabled: in "
    "Windows run 'powercfg /h off' from an administrator prompt, then use "
    "Shut down (not Restart) before booting Linux.")


class BtSyncError(Exception):
    """An expected failure, with a message meant for the user."""


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
        print(f"bt-sync: warning: ignoring invalid {CONFIG}: {e}", file=sys.stderr)
    return cfg


CFG = load_config()


def os_name():
    """Pretty name of this Linux distribution (from /etc/os-release)."""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("NAME="):
                return line[5:].strip().strip("\"'") or "Linux"
    except OSError:
        pass
    return "Linux"


LINUX = os_name()


def fmt_mac(raw):
    raw = raw.upper()
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


def raw_mac(mac):
    return mac.replace(":", "").lower()


def rev_hex(h):
    return bytes.fromhex(h)[::-1].hex().upper()


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def require_root():
    if os.geteuid() != 0:
        raise BtSyncError("this command must be run as root (try: sudo bt-sync ...).")


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
                    raise BtSyncError("another bt-sync operation is still running; "
                                      "try again in a minute.") from None
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
            raise BtSyncError(f"lsblk failed: {r.stderr.strip()}")
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

        msg = "Windows partition not found: no NTFS partition contains " + HIVE_PATH + "."
        if any((d.get("fstype") or "").lower() == "bitlocker" for d in devs):
            msg += (" A BitLocker-encrypted partition was detected: bt-sync cannot read "
                    "it. Turn off BitLocker (or device encryption) on the Windows "
                    "system drive.")
        elif not parts:
            msg += " No NTFS partitions were detected at all."
        if mount_errors:
            msg += " Some NTFS partitions could not be mounted (" + \
                   "; ".join(mount_errors) + "). " + FAST_STARTUP_HINT
        raise BtSyncError(msg)

    def _mount(self, dev, errors):
        last = ""
        for mode in (["rw", "ro"] if self.rw else ["ro"]):
            for fs in ("ntfs3", "ntfs-3g"):
                r = run(["mount", "-t", fs, "-o", mode, dev, str(MOUNTPOINT)])
                if r.returncode == 0:
                    return MOUNTPOINT
                last = r.stderr.strip() or f"mount -t {fs} failed"
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
            raise BtSyncError("the Python hivex bindings are missing "
                              "(install the 'hivex' package).") from None
        try:
            self.h = hivex.Hivex(str(hive_path), write=write)
        except Exception as e:
            raise BtSyncError(f"cannot open the Windows registry {hive_path}: {e}") from None
        root = self.h.root()
        sel = self._child(root, "Select")
        cur_v = self._val(sel, "Current") if sel else None
        if not cur_v:
            raise BtSyncError("unexpected Windows registry layout "
                              "(SYSTEM\\Select\\Current is missing).")
        cur = int.from_bytes(cur_v[1][:4], "little")
        path = [f"ControlSet{cur:03d}", "Services", "BTHPORT", "Parameters"]
        node = root
        for i, part in enumerate(path):
            node = self._child(node, part)
            if node is None:
                raise BtSyncError("Windows registry key not found: SYSTEM\\" +
                                  "\\".join(path[:i + 1]) +
                                  ". Has Bluetooth ever been used on this Windows install?")
        self.keys = self._child(node, "Keys")
        self.devices = self._child(node, "Devices")
        if self.keys is None:
            raise BtSyncError("no Bluetooth pairing keys in the Windows registry "
                              "(pair at least one device in Windows first).")

    def _child(self, node, name):
        c = self.h.node_get_child(node, name)
        return c if c else None   # hivex returns None/0 when the child is missing

    def _val(self, node, name):
        v = self.h.node_get_value(node, name)
        return self.h.value_value(v) if v else None

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
                    "name": self._name(rawname.lower()) or mac,
                    "classic": None, "le": None})

            for v in self.h.node_values(ad):          # BR/EDR (classic) keys
                name = self.h.value_key(v)
                if HEX12.match(name):
                    data = self.h.value_value(v)[1]
                    if len(data) == 16:
                        dev(name)["classic"] = data.hex().upper()
            for ch in self.h.node_children(ad):       # BLE keys
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
                dev(name)["le"] = {
                    "ltk": ltk[1].hex().upper(),
                    "key_length": int.from_bytes(kl[1][:4], "little") if kl else 16,
                    "ediv": int.from_bytes(ediv[1][:4], "little") if ediv else 0,
                    "erand": int.from_bytes(erand[1][:8], "little") if erand else 0,
                    "irk": irk_hex,
                    "addr_type": atype,
                }
        return res

    # ---- writing
    def _adapter(self, adapter):
        ad = self._child(self.keys, raw_mac(adapter))
        if ad is None:
            raise BtSyncError(f"Bluetooth adapter {adapter} is not in the Windows registry.")
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
            raise BtSyncError(f"writing the Windows registry failed: {e}") from None


# ----------------------------------------------------------------- BlueZ
def _cp():
    cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
    cp.optionxform = str
    return cp


def _read_info(name, info):
    cp = _cp()
    cp.read(info)
    entry = {"name": cp.get("General", "Name", fallback=name),
             "classic": None, "le": None}
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


def linux_read():
    res = {}
    if not BT_DIR.exists():
        return res
    for ad in BT_DIR.iterdir():
        if not (ad.is_dir() and MAC_RE.match(ad.name)):
            continue
        devs = res.setdefault(ad.name, {})
        for d in ad.iterdir():
            info = d / "info"
            if not (d.is_dir() and MAC_RE.match(d.name) and info.exists()):
                continue
            try:
                entry = _read_info(d.name, info)
            except (configparser.Error, ValueError, OSError) as e:
                print(f"bt-sync: warning: skipping unreadable {info}: {e}", file=sys.stderr)
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
        g["Name"] = name
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


# ------------------------------------------------------------- comparison
def _sig(kind, v):
    return v if kind == "classic" else (v["ltk"], v["ediv"], v["erand"])


def load_ignored():
    try:
        return set(json.loads(IGNORED.read_text()))
    except (OSError, ValueError):
        return set()


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
                items.append({"adapter": ad, "mac": mac, "kind": kind, "action": action,
                              "name": (w or l)["name"], "win": wk, "linux": lk})
    return items


# ----------------------------------------------------------------- state
def summarize(devs):
    """Summary WITHOUT keys: the state file is world-readable."""
    return {ad: {mac: {"name": d["name"], "classic": bool(d["classic"]), "le": bool(d["le"])}
                 for mac, d in ds.items()} for ad, ds in devs.items()}


def save_state(win, lin, items, error=None):
    ensure_dirs()
    write_json_atomic(STATE, {
        "time": time.time(), "error": error,
        "linux": summarize(lin), "windows": summarize(win),
        "items": [{k: i[k] for k in ("adapter", "mac", "kind", "action", "name")}
                  for i in items],
    })


def refresh_state():
    """Rescan both systems and rewrite the state file. Never raises."""
    lin = linux_read()
    try:
        with WinMount(rw=False) as wm:
            win = WinReg(wm.hive).read_all()
    except Exception as e:  # the scan service must not fail loudly
        msg = str(e) if isinstance(e, BtSyncError) else f"unexpected error: {e!r}"
        save_state({}, lin, [], error=msg)
        print(f"bt-sync: scan failed: {msg}", file=sys.stderr)
        return False
    save_state(win, lin, diff(win, lin))
    return True


# ------------------------------------------------------------------ sync
def do_sync(wm, to_linux, to_win, errors):
    """Copy the keys. Returns the list of things done; problems go to errors."""
    log = []
    if to_linux:
        # BlueZ rewrites the info files when it stops: stop it first.
        r = run(["systemctl", "stop", "bluetooth.service"])
        if r.returncode != 0:
            raise BtSyncError(f"could not stop bluetooth.service: {r.stderr.strip()}")
        try:
            for it in to_linux:
                linux_write(it["adapter"], it["mac"], it["name"], it["kind"], it["win"])
                log.append(f"imported into {LINUX}: {it['name']} ({it['kind']})")
        finally:
            r = run(["systemctl", "start", "bluetooth.service"])
            if r.returncode != 0:
                errors.append("could not restart bluetooth.service, run "
                              f"'sudo systemctl start bluetooth': {r.stderr.strip()}")
    if to_win:
        ensure_dirs()
        bak = BACKUPS / f"SYSTEM.{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copyfile(wm.hive, bak)
        os.chmod(bak, 0o600)
        for old in sorted(BACKUPS.glob("SYSTEM.*"))[:-CFG["keep_backups"]]:
            old.unlink()
        log.append(f"backup of the Windows registry saved to {bak}")
        wreg = WinReg(wm.hive, write=True)
        for it in to_win:
            if it["kind"] == "classic":
                wreg.write_classic(it["adapter"], it["mac"], it["linux"])
            else:
                wreg.write_le(it["adapter"], it["mac"], it["linux"])
        wreg.commit()
        del wreg
        log += [f"exported to Windows: {it['name']} ({it['kind']})" for it in to_win]
    return log


def readonly_msg(wm):
    where = "" if wm._ours else f" (it is already mounted read-only at {wm.root})"
    return f"Windows is mounted read-only{where}. " + FAST_STARTUP_HINT


def cmd_scan(_):
    require_root()
    try:
        with exclusive_lock(timeout=30):
            ok = refresh_state()
    except BtSyncError:
        print("bt-sync: another operation is running, scan skipped.")
        return 0
    print("Scan completed." if ok else "Scan completed with errors (see state.json).")
    return 0


def _parse_requests(raw):
    try:
        reqs = json.loads(raw)["requests"]
    except (ValueError, KeyError, TypeError):
        raise BtSyncError("invalid request from the GUI (malformed JSON).") from None
    if not isinstance(reqs, list):
        raise BtSyncError("invalid request from the GUI.")
    for r in reqs:
        if not (isinstance(r, dict)
                and MAC_RE.match(str(r.get("adapter")))
                and MAC_RE.match(str(r.get("mac")))
                and r.get("direction") in ("win2linux", "linux2win")):
            raise BtSyncError(f"invalid request from the GUI: {r!r}")
    return reqs


def cmd_apply_json(_):
    """Called by the GUI through pkexec. Only (adapter, mac, direction) are
    accepted: the keys are always re-read here as root, never taken from
    the caller. Always prints one JSON line: {"ok": [...], "errors": [...]}."""
    result = {"ok": [], "errors": []}
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
                            result["errors"].append(
                                f"{r['mac']}: nothing to copy in that direction "
                                "(already in sync?).")
                    result["ok"] = do_sync(wm, to_linux, to_win, result["errors"])
            finally:
                refresh_state()
    except BtSyncError as e:
        result["errors"].append(str(e))
    except Exception as e:
        result["errors"].append(f"unexpected error: {e!r}")
    print(json.dumps(result))
    return 0


# ----------------------------------------------------- terminal (CLI) mode
LABEL = {
    "win2linux": f"on Windows, missing on {LINUX}",
    "linux2win": f"on {LINUX}, missing on Windows",
    "conflict": "DIFFERENT keys on the two systems",
}


def cmd_status(_):
    require_root()
    with exclusive_lock():
        with WinMount(rw=False) as wm:
            win = WinReg(wm.hive).read_all()
    items = diff(win, linux_read(), use_ignored=True)
    if not items:
        print("Everything is in sync.")
    for i in items:
        print(f"{i['action']:10} {i['kind']:7} {i['mac']}  {i['name']}")
    return 0


def _ask(item, can_write_win):
    print(f"\n* {item['name']}  [{item['mac']}]  ({item['kind'].upper()})")
    print(f"  {LABEL[item['action']]}")
    opts = {"n": "skip", "i": "ignore from now on"}
    if item["action"] == "win2linux":
        opts = {"y": f"import into {LINUX}", **opts}
    elif item["action"] == "linux2win":
        if can_write_win:
            opts = {"y": "export to Windows", **opts}
        else:
            print("  (Windows is mounted read-only: cannot export)")
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


def cmd_apply(_):
    require_root()
    with exclusive_lock():
        with WinMount(rw=True) as wm:
            win = WinReg(wm.hive).read_all()
            items = diff(win, linux_read(), use_ignored=True)
            if not items:
                print("Everything is in sync.")
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
            errors = []
            for line in do_sync(wm, to_linux, to_win, errors):
                print("  " + line)
            for line in errors:
                print("  error: " + line, file=sys.stderr)
        refresh_state()
    return 1 if errors else 0


def main():
    p = argparse.ArgumentParser(prog="bt-sync", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")
    for name, fn, hlp in (
            ("scan", cmd_scan, "rescan both systems and update the state file"),
            ("status", cmd_status, "print the differences"),
            ("apply", cmd_apply, "interactive sync"),
            ("apply-json", cmd_apply_json, "backend for the GUI (reads JSON on stdin)")):
        sub.add_parser(name, help=hlp).set_defaults(fn=fn)
    args = p.parse_args()
    try:
        return args.fn(args)
    except BtSyncError as e:
        print(f"bt-sync: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted, nothing was changed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
