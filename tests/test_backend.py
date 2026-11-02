# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
"""Unit tests for the parts of bt-sync that do not need root, a Windows
partition or Bluetooth hardware.  Run with:  python3 -m unittest -v"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btsync import backend as m  # noqa: E402
from btsync import i18n  # noqa: E402

AD = "AA:BB:CC:DD:EE:FF"
DEV = "11:22:33:44:55:66"
KEY = "0123456789ABCDEF0123456789ABCDEF"
LE = {"ltk": "F" * 32, "key_length": 16, "ediv": 1, "erand": 2, "irk": None,
      "addr_type": "static"}


class _Result:
    returncode, stdout, stderr = 0, "", ""


class FakeBluez(unittest.TestCase):
    """Point the backend at a temporary /var/lib/bluetooth and state dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = {k: getattr(m, k) for k in
                      ("BT_DIR", "STATE_DIR", "STATE", "BACKUPS", "RUN_DIR", "LOCK",
                       "run", "require_root", "refresh_state")}
        m.BT_DIR = self.tmp / "bluetooth"
        m.STATE_DIR = self.tmp / "state"
        m.STATE = m.STATE_DIR / "state.json"
        m.BACKUPS = m.STATE_DIR / "backups"
        m.RUN_DIR = self.tmp / "run"
        m.LOCK = m.RUN_DIR / "lock"
        m.run = lambda cmd, **kw: _Result()          # no systemctl
        m.require_root = lambda: None
        m.refresh_state = lambda: True
        (m.BT_DIR / AD).mkdir(parents=True)
        i18n.set_language("en")

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(m, k, v)

    def info(self, mac=DEV):
        return (m.BT_DIR / AD / mac / "info").read_text()


class TestBluez(FakeBluez):
    def test_roundtrip_classic_and_le(self):
        m.linux_write(AD, DEV, "Headphones", "classic", KEY)
        m.linux_write(AD, "C1:00:00:00:00:01", "Mouse", "le", LE)
        lin = m.linux_read()
        self.assertEqual(lin[AD][DEV]["classic"], KEY)
        self.assertEqual(lin[AD]["C1:00:00:00:00:01"]["le"]["ltk"], "F" * 32)
        self.assertEqual(lin[AD]["C1:00:00:00:00:01"]["le"]["addr_type"], "static")

    def test_existing_link_key_type_is_kept(self):
        d = m.BT_DIR / AD / DEV
        d.mkdir()
        (d / "info").write_text("[General]\nName=Old\n\n[LinkKey]\nKey=00\nType=5\nPINLength=4\n")
        m.linux_write(AD, DEV, "New", "classic", KEY)
        text = self.info()
        self.assertIn("Type=5", text)
        self.assertIn("PINLength=4", text)
        self.assertIn("Name=Old", text)

    def test_device_name_cannot_inject_lines(self):
        m.linux_write(AD, DEV, "Evil\n[LinkKey]\nKey=00", "classic", KEY)
        lines = self.info().splitlines()
        self.assertEqual(lines.count("[LinkKey]"), 1)       # only the real section
        self.assertIn("Name=Evil [LinkKey] Key=00", lines)  # the name stays on one line

    def test_unreadable_info_is_skipped(self):
        d = m.BT_DIR / AD / DEV
        d.mkdir()
        (d / "info").write_text("garbage without a section header")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.linux_read(), {AD: {}})


class TestDiff(unittest.TestCase):
    def entry(self, key, t):
        return {DEV: {"name": "H", "classic": key, "le": None, "times": {"classic": t}}}

    def test_conflict_suggests_newer_side(self):
        items = m.diff({AD: self.entry("A" * 32, 5000.0)}, {AD: self.entry("B" * 32, 1000.0)})
        self.assertEqual([(i["action"], i["newer"]) for i in items], [("conflict", "windows")])

    def test_close_timestamps_give_no_suggestion(self):
        items = m.diff({AD: self.entry("A" * 32, 1000.0)}, {AD: self.entry("B" * 32, 1030.0)})
        self.assertIsNone(items[0]["newer"])

    def test_one_sided_and_equal(self):
        self.assertEqual(m.diff({AD: self.entry(KEY, 1.0)}, {AD: {}})[0]["action"], "win2linux")
        self.assertEqual(m.diff({AD: self.entry(KEY, 1.0)}, {AD: self.entry(KEY, 9.0)}), [])


class TestKeyBackup(FakeBluez):
    def _import(self, data, dry_run=False, overwrite=False):
        args = type("A", (), {"dry_run": dry_run, "overwrite": overwrite, "json": True})
        sys.stdin = io.StringIO(json.dumps(data))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                m.cmd_import_keys(args)
        finally:
            sys.stdin = sys.__stdin__
        return json.loads(out.getvalue().splitlines()[-1])

    def _export(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.cmd_export_keys(None)
        return json.loads(out.getvalue())

    def test_export_import_cycle(self):
        m.linux_write(AD, DEV, "Headphones", "classic", KEY)
        backup = self._export()
        self.assertEqual(backup["format"], "bt-sync-keys")
        backup["adapters"][AD][DEV]["classic"] = "E" * 32
        backup["adapters"][AD]["C1:00:00:00:00:01"] = {"name": "Mouse", "classic": None, "le": LE}
        backup["adapters"]["00:00:00:00:00:01"] = {DEV: {"name": "x", "classic": KEY, "le": None}}

        plan = self._import(backup, dry_run=True)
        self.assertEqual(plan["new"], ["Mouse (LE)"])
        self.assertEqual(plan["different"], ["Headphones (Classic)"])
        self.assertEqual(plan["missing_adapters"], ["00:00:00:00:00:01"])
        self.assertIn("Key=" + KEY, self.info())               # dry run: unchanged

        self._import(backup)                                     # new keys only
        self.assertIn("Key=" + KEY, self.info())
        self.assertTrue((m.BT_DIR / AD / "C1:00:00:00:00:01" / "info").exists())

        self._import(backup, overwrite=True)
        self.assertIn("Key=" + "E" * 32, self.info())

    def test_invalid_files_are_rejected(self):
        bad_le = dict(LE, key_length=True)
        for data in ({"format": "other"},
                     {"format": "bt-sync-keys", "version": 1,
                      "adapters": {AD: {DEV: {"classic": "not hex"}}}},
                     {"format": "bt-sync-keys", "version": 1,
                      "adapters": {AD: {DEV: {"le": bad_le}}}},
                     {"format": "bt-sync-keys", "version": 1,
                      "adapters": {"../../etc": {DEV: {"classic": KEY}}}}):
            res = self._import(data, dry_run=True)
            self.assertEqual(res["errors"], ["this is not a valid bt-sync key backup file."])


class FakeHivex:
    """Tiny in-memory stand-in for hivex.Hivex, with the same quirks: a
    missing child gives None, a missing value raises RuntimeError."""
    store = {}

    def __init__(self, path, write=False):
        self.path = path
        self.n = FakeHivex.store[path]

    def root(self):
        return 1

    def node_get_child(self, node, name):
        return next((c for c in self.n[node]["children"]
                     if self.n[c]["name"].lower() == name.lower()), None)

    def node_children(self, node):
        return list(self.n[node]["children"])

    def node_name(self, node):
        return self.n[node]["name"]

    def node_timestamp(self, node):
        return 116444736000000000 + 10_000_000 * 1000   # 1000 s after the epoch

    def node_values(self, node):
        return [(node, k) for k in self.n[node]["values"]]

    def node_get_value(self, node, key):
        if key not in self.n[node]["values"]:
            raise RuntimeError("Success")
        return (node, key)

    def value_key(self, v):
        return v[1]

    def value_value(self, v):
        return self.n[v[0]]["values"][v[1]]

    def node_set_value(self, node, val):
        self.n[node]["values"][val["key"]] = (val["t"], val["value"])

    def node_set_values(self, node, vals):
        self.n[node]["values"] = {v["key"]: (v["t"], v["value"]) for v in vals}

    def node_add_child(self, node, name):
        new = max(self.n) + 1
        self.n[new] = {"name": name, "children": [], "values": {}}
        self.n[node]["children"].append(new)
        return new

    def node_delete_child(self, node):
        for n in self.n.values():
            if node in n["children"]:
                n["children"].remove(node)

    def commit(self, _path):
        pass


def fake_hive(path):
    """SYSTEM hive with one adapter: two Classic keys, one LE key without the
    optional values (KeyLength, IRK, AddressType) and a CentralIRK value."""
    h = {1: {"name": "ROOT", "children": [], "values": {}}}
    FakeHivex.store[path] = h
    x = FakeHivex(path)
    sel = x.node_add_child(1, "Select")
    x.node_set_value(sel, {"key": "Current", "t": 4, "value": (1).to_bytes(4, "little")})
    n = 1
    for part in ("ControlSet001", "Services", "BTHPORT", "Parameters"):
        n = x.node_add_child(n, part)
    keys = x.node_add_child(n, "Keys")
    devs = x.node_add_child(n, "Devices")
    ad = x.node_add_child(keys, "aabbccddeeff")
    for k, v in (("112233445566", "11"), ("223344556677", "22"), ("CentralIRK", "99")):
        x.node_set_value(ad, {"key": k, "t": 3, "value": bytes.fromhex(v * 16)})
    le = x.node_add_child(ad, "c1aabbccdd01")
    x.node_set_value(le, {"key": "LTK", "t": 3, "value": bytes.fromhex("33" * 16)})
    d = x.node_add_child(devs, "112233445566")
    x.node_set_value(d, {"key": "Name", "t": 3, "value": b"Headphones\0"})
    return path


class TestWindowsRegistry(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get("hivex")
        sys.modules["hivex"] = type(sys)("hivex")
        sys.modules["hivex"].Hivex = FakeHivex
        self.hive = fake_hive("SYSTEM")

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("hivex", None)
        else:
            sys.modules["hivex"] = self.saved

    def test_read_with_missing_optional_values(self):
        devs = m.WinReg(self.hive).read_all()[AD]
        self.assertEqual(devs[DEV]["name"], "Headphones")
        le = devs["C1:AA:BB:CC:DD:01"]["le"]
        self.assertEqual((le["key_length"], le["irk"], le["addr_type"]), (16, None, "static"))
        self.assertEqual(devs[DEV]["times"]["classic"], 1000.0)

    def test_delete_keeps_other_values(self):
        w = m.WinReg(self.hive, write=True)
        self.assertTrue(w.delete_device(AD, DEV))
        self.assertTrue(w.delete_device(AD, "C1:AA:BB:CC:DD:01"))
        self.assertFalse(w.delete_device(AD, "99:99:99:99:99:99"))
        devs = m.WinReg(self.hive).read_all()[AD]
        self.assertEqual(sorted(devs), ["22:33:44:55:66:77"])
        h = FakeHivex(self.hive)
        ad = h.node_get_child(h.node_get_child(h.node_get_child(h.node_get_child(
            h.node_get_child(h.node_get_child(1, "ControlSet001"), "Services"), "BTHPORT"),
            "Parameters"), "Keys"), "aabbccddeeff")
        self.assertEqual(h.value_value((ad, "CentralIRK"))[1], bytes.fromhex("99" * 16))

    def test_write_roundtrip(self):
        le = {"ltk": "AB" * 16, "key_length": 16, "ediv": 4660, "erand": 2 ** 63 + 5,
              "irk": "CD" * 16, "addr_type": "static"}
        w = m.WinReg(self.hive, write=True)
        w.write_classic(AD, "33:44:55:66:77:88", "EE" * 16)
        w.write_le(AD, "C1:00:00:00:00:02", le)
        devs = m.WinReg(self.hive).read_all()[AD]
        self.assertEqual(devs["33:44:55:66:77:88"]["classic"], "EE" * 16)
        self.assertEqual(devs["C1:00:00:00:00:02"]["le"], le)


class TestDeleteLinux(FakeBluez):
    def test_delete_removes_device_and_cache(self):
        m.linux_write(AD, DEV, "Headphones", "classic", KEY)
        (m.BT_DIR / AD / "cache").mkdir()
        (m.BT_DIR / AD / "cache" / DEV).write_text("cache")
        errors = []
        log, _bak = m.delete_pairings(
            [{"adapter": AD, "mac": DEV, "side": "linux"},
             {"adapter": AD, "mac": "00:00:00:00:00:01", "side": "linux"}], errors)
        self.assertEqual(len(log), 1)
        self.assertEqual(len(errors), 1)
        self.assertFalse((m.BT_DIR / AD / DEV).exists())
        self.assertFalse((m.BT_DIR / AD / "cache" / DEV).exists())

    def test_delete_requests_are_validated(self):
        for raw in ('{"requests": [{"adapter": "%s", "mac": "../../etc", "side": "linux"}]}' % AD,
                    '{"requests": [{"adapter": "%s", "mac": "%s", "side": "both"}]}' % (AD, DEV)):
            with self.assertRaises(m.BtSyncError):
                m._parse_delete_requests(raw)


class TestI18n(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_locale_matching(self):
        self.assertEqual(i18n._match("it_IT.UTF-8"), "it")
        self.assertEqual(i18n._match("pt_PT.UTF-8"), "pt_BR")
        self.assertEqual(i18n._match("zh_CN.UTF-8"), "zh_CN")
        self.assertIsNone(i18n._match("zh_TW.UTF-8"))
        self.assertIsNone(i18n._match("xx_YY"))

    def test_structured_errors_render(self):
        e = m.BtSyncError(m.N_("lsblk failed: {error}"), error="boom")
        self.assertEqual(str(e), "lsblk failed: boom")
        self.assertEqual(e.to_json(), [{"id": "lsblk failed: {error}", "args": {"error": "boom"}}])

    @unittest.skipUnless(i18n.locale_dir(), "translations not compiled (run make)")
    def test_translation_is_used(self):
        i18n.set_language("it")
        self.assertEqual(i18n._("Settings"), "Impostazioni")


if __name__ == "__main__":
    unittest.main()
