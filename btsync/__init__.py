# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
"""bt-sync: keep Bluetooth pairing keys in sync between Linux and Windows."""

__version__ = "1.0.3"

PROJECT_URL = "https://github.com/Eraser-025/bt-sync"
KOFI_URL = "https://ko-fi.com/eraser117"


def os_name():
    """Name of this Linux distribution, from /etc/os-release."""
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("NAME="):
                    return line[5:].strip().strip("\"'") or "Linux"
    except OSError:
        pass
    return "Linux"
