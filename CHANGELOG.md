# Changelog

## 1.0.2

- New **Delete…** button under each list (and `bt-sync delete`): removes the
  pairing of the selected devices from Linux or from Windows. A backup of the
  Windows registry is made first.
- Fix: the Windows scan failed when an LE device had no IRK, KeyLength or
  AddressType value in the registry.

## 1.0.1

- Settings dialog: the text of the key backup section is never cut, in any
  language, font size or display scaling.

## 1.0.0

First release.

- Sync Bluetooth Classic and LE pairing keys between Linux (BlueZ) and Windows,
  in both directions, from a tray app or the command line.
- Suggests the copy direction when the keys differ (★ = newer key).
- Export/import of the Linux pairing keys (key backup).
- Translations: English, Italian, German, Spanish, French, Brazilian
  Portuguese, Russian, Simplified Chinese, Japanese.
- Packages for Arch (AUR), Debian/Ubuntu/Mint (.deb) and Fedora/Bazzite (.rpm).
