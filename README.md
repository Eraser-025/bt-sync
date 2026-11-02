# bt-sync

**Keep your Bluetooth devices paired on both Linux and Windows in a dual-boot setup.**

When you pair a Bluetooth device, the device and the computer agree on a secret
pairing key. Linux and Windows share the same Bluetooth adapter but keep their
own copy of that key, so pairing on one system breaks the pairing on the other.
You end up re-pairing your headphones, mouse or keyboard every time you switch
OS.

bt-sync reads the pairing keys of both systems (BlueZ on Linux and the
`SYSTEM` registry hive on Windows), shows the differences and copies the keys
in the direction you choose. After that, the device works on both systems
without pairing it again.

- Tray app (PyQt6) that tells you when something is out of sync
- Supports Bluetooth Classic (BR/EDR) and Bluetooth Low Energy (LE) devices
- Copies keys in both directions: Windows → Linux and Linux → Windows
- A background scan mounts Windows **read-only**; the Windows partition is
  only mounted read-write when you ask to copy keys *to* Windows
- Backs up the Windows registry before every write
- No hardcoded partitions or devices: the Windows partition and the Bluetooth
  adapters are detected automatically

## Requirements

- Arch Linux or an Arch-based distribution (CachyOS, EndeavourOS, Manjaro, …)
  with systemd and BlueZ
- Windows 10/11 on an NTFS partition of the same machine, **not encrypted with
  BitLocker**
- Packages: `python`, `python-pyqt6`, `hivex`, `bluez`, `polkit`,
  `xdg-utils`, `util-linux`; `ntfs-3g` is optional (the kernel `ntfs3` driver is
  used first)
- A polkit authentication agent (every major desktop environment ships one)

## Installation

### From the AUR

```sh
paru -S bt-sync        # or: yay -S bt-sync
```

Then enable the background scan:

```sh
sudo systemctl enable --now bt-sync-scan.timer bt-sync-scan.path
```

### Manual installation

```sh
git clone https://github.com/Eraser-025/bt-sync.git
cd bt-sync
sudo make PREFIX=/usr/local install
sudo systemd-tmpfiles --create bt-sync.conf
sudo systemctl daemon-reload
sudo systemctl enable --now bt-sync-scan.timer bt-sync-scan.path
```

Uninstall with `sudo make PREFIX=/usr/local uninstall`.

## Usage

### Recommended workflow

The pairing key that counts is the one from the system where you paired the
device **last**: copy *from* that system.

1. Pair the device on Linux.
2. Boot Windows and pair the device there too (this invalidates the Linux key).
3. Shut down Windows (see [Fast Startup](#fast-startup-and-hibernation)) and
   boot Linux.
4. Open **Bluetooth Sync**, select the device in the *Windows* list and press
   **←** to copy the Windows key to Linux.

Copying in the other direction (**→**, Linux to Windows) works as well. It is
handy when you pair a new device on Linux and want it on Windows without
pairing it again there.

### The GUI

Start **Bluetooth Sync** from the application menu, or run `bt-sync-gui`
(`bt-sync-gui --tray` starts it hidden in the system tray). Tick **Start in
tray at login** to start it automatically. The tray icon shows an orange dot
and a notification when a device needs syncing.

| Mark | Meaning |
|------|---------|
| ✔ | In sync on both systems |
| → | Paired on one system only (or only partly synced) |
| ⚠ | Paired on both systems with **different** keys: the arrow overwrites the other side |

Devices you never want to sync can be moved to the **Archive**. bt-sync then
ignores them and stops notifying you about them.

Copying keys requires your administrator password (polkit). When keys are
copied to Linux, `bluetooth.service` is restarted, so connected devices drop
for a moment.

### Command line

```sh
sudo bt-sync status   # show the differences
sudo bt-sync apply    # interactive sync in the terminal
sudo bt-sync scan     # rescan now (what the timer does)
```

## Configuration

`/etc/bt-sync.conf`:

```ini
[bt-sync]
# Try this if an LE device connects after syncing but loses the pairing
# after a while: some Windows versions store the IRK byte-reversed.
reverse_irk = false
# Number of Windows registry backups kept in /var/lib/bt-sync/backups
keep_backups = 5
```

## Warnings — please read

> [!WARNING]
> bt-sync writes to the Windows registry when you copy keys to Windows. A
> backup of the `SYSTEM` hive is saved in `/var/lib/bt-sync/backups` before
> every write, but **use it at your own risk** and keep your own backups of
> important data.

### Fast Startup and hibernation

If Windows was hibernated or *Fast Startup* is enabled, the Windows partition
is left in a "dirty" state. Linux can then only mount it read-only, and
writing to it could corrupt it. bt-sync detects this and refuses to write to
Windows. To fix it, open an administrator prompt in Windows, run

```bat
powercfg /h off
```

and always use **Shut down** (not *Restart* into Linux) before booting Linux.
Reading Windows keys still works when the partition is read-only.

### BitLocker

bt-sync cannot read a BitLocker-encrypted Windows partition (Windows 11 often
enables *Device encryption* by default). It tells you when it finds one.

### Same adapter

Keys are matched by the MAC address of the Bluetooth adapter. If your adapter
reports a different address on Linux and Windows, bt-sync has nothing to
match.

### Permissions and what runs as root

- `bt-sync-scan.service` runs as **root** every 30 minutes (and when you press
  *Refresh*). It mounts the Windows partition read-only under `/run/bt-sync`,
  reads the keys and unmounts it, unless you had already mounted it yourself.
- It writes `/var/lib/bt-sync/state.json`, which is **world-readable** and
  contains device names and MAC addresses, but **never the keys**.
- `/var/lib/bt-sync/refresh.trigger` is **world-writable** so that the GUI can
  request a rescan: any local user can trigger a scan, nothing else.
- The GUI runs as your user. Copying keys runs `bt-sync apply-json` as root
  through `pkexec` (polkit action `io.github.eraser025.btsync.apply`, admin
  password required). The root helper only accepts *which* device to copy and
  in *which* direction; it always re-reads the keys itself.
- Registry backups in `/var/lib/bt-sync/backups` are readable by root only.
  They contain secrets from your Windows installation, so do not share them.

## Troubleshooting

- **"No scan available yet" / "The scan did not answer"**: enable the units with
  `sudo systemctl enable --now bt-sync-scan.timer bt-sync-scan.path`, then
  check `journalctl -u bt-sync-scan.service`.
- **"Windows is mounted read-only"**: see [Fast Startup](#fast-startup-and-hibernation).
- **An LE device pairs but disconnects after a while**: set
  `reverse_irk = true` in `/etc/bt-sync.conf`, then sync the device again.
- **Restore a registry backup** (from Linux, with Windows shut down): mount the
  Windows partition read-write and copy
  `/var/lib/bt-sync/backups/SYSTEM.<date>` over
  `Windows/System32/config/SYSTEM`.

## License

Copyright (C) 2026 Eraser-025

bt-sync is free software, released under the
[GNU General Public License v3.0 or later](LICENSE) (`GPL-3.0-or-later`).

## Support

If bt-sync saved you some re-pairing, you can buy me a 1UP ☕

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/eraser117)

Ko-fi accepts PayPal and cards: <https://ko-fi.com/eraser117>
