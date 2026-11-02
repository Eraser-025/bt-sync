# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eraser-025
"""Translations (gettext). The catalogs live in <prefix>/share/locale."""
import gettext
import os
from pathlib import Path

DOMAIN = "bt-sync"

# Language code -> name of the language in that language.
LANGUAGES = {
    "en": "English",
    "it": "Italiano",
    "de": "Deutsch",
    "es": "Español",
    "fr": "Français",
    "pt_BR": "Português (Brasil)",
    "ru": "Русский",
    "zh_CN": "简体中文",
    "ja": "日本語",
}

_PKG = Path(__file__).resolve().parent
_tr = gettext.NullTranslations()
_current = "en"


def locale_dir():
    candidates = (
        _PKG.parent / "build" / "locale",          # source tree, after `make`
        _PKG.parents[2] / "share" / "locale",      # <prefix>/lib/bt-sync/btsync
    )
    for cand in candidates:
        if any(cand.glob(f"*/LC_MESSAGES/{DOMAIN}.mo")):
            return str(cand)
    return None


def _match(item):
    """Map a locale name such as 'it_IT.UTF-8' to one of LANGUAGES."""
    item = item.split(".")[0].split("@")[0]
    if item in LANGUAGES:
        return item
    lang, _sep, territory = item.partition("_")
    if lang == "pt":
        return "pt_BR"
    if lang == "zh":
        return "zh_CN" if territory in ("", "CN", "SG") else None
    return lang if lang in LANGUAGES else None


def system_language():
    """The best available language for the user's locale settings."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for item in (os.environ.get(var) or "").split(":"):
            if item and item not in ("C", "POSIX"):
                code = _match(item)
                if code:
                    return code
    return "en"


def set_language(code=None):
    """Switch language; None (or an unknown code) means the system language."""
    global _tr, _current
    if code not in LANGUAGES:
        code = system_language()
    _current = code
    if code == "en":
        _tr = gettext.NullTranslations()
    else:
        _tr = gettext.translation(DOMAIN, locale_dir(), languages=[code], fallback=True)
    return code


def current():
    return _current


def _(message):
    return _tr.gettext(message)


def ngettext(singular, plural, n):
    return _tr.ngettext(singular, plural, n)


def N_(message):
    """Mark a string for translation without translating it yet."""
    return message


def render(parts):
    """Translate and join a structured message: [(msgid, {args}), ...]."""
    out = []
    for msgid, args in parts:
        text = _(msgid)
        try:
            text = text.format(**args)
        except (KeyError, IndexError, ValueError):   # broken translation
            text = msgid.format(**args)
        out.append(text)
    return " ".join(out)


set_language()
