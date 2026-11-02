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
