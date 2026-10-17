"""Translations: English source texts, gettext catalogs in scan_watch/locale/<lang>.po.

Messages are Text objects (built with _()), not finished strings: they keep the
English template and its named arguments and are rendered on output, so every
browser gets its own language.

Logs are always English, so they read the same everywhere and need no support in
other languages. Only an interactive terminal session (cli.main() calls
use_locale()) follows LANG.
tools/update_po.py collects all _() and t() texts and updates the catalogs.
"""

import ast
import functools
import os
import re
from importlib import resources

SOURCE_LANGUAGE = "en"
_terminal_language = SOURCE_LANGUAGE


class Text:
    """A translatable message: English template plus named arguments."""

    __slots__ = ("msgid", "args")

    def __init__(self, msgid, **args):
        self.msgid = msgid
        self.args = args

    def render(self, lang=None):
        lang = lang or server_language()
        args = {k: render(v, lang) for k, v in self.args.items()}
        try:
            return gettext(self.msgid, lang).format(**args)
        except (KeyError, IndexError, ValueError):  # a broken translation must not hide the message
            return self.msgid.format(**args)

    def __str__(self):
        return self.render()

    def __repr__(self):
        return f"Text({self.msgid!r}, {self.args!r})"

    def __eq__(self, other):
        if isinstance(other, Text):
            return (self.msgid, self.args) == (other.msgid, other.args)
        return NotImplemented

    def __hash__(self):
        return hash(self.msgid)


def _(msgid, **args):
    """Mark a text for translation; rendered later in the reader's language."""
    return Text(msgid, **args)


def render(value, lang=None):
    """Text in the given language; other values (numbers, file names, OS errors) unchanged."""
    if isinstance(value, Text):
        return value.render(lang)
    if isinstance(value, BaseException):
        return render(value.args[0], lang) if len(value.args) == 1 else str(value)
    if isinstance(value, float):
        return value  # keeps format specs like {ratio:.2f} working
    return value if isinstance(value, int) else str(value)


def gettext(msgid, lang):
    return catalog(lang).get(msgid) or msgid


@functools.lru_cache(maxsize=None)
def catalog(lang):
    """msgid -> msgstr of a language; empty for English and unknown languages."""
    if lang == SOURCE_LANGUAGE:
        return {}
    try:
        text = resources.files("scan_watch").joinpath("locale", f"{lang}.po").read_text("utf-8")
    except (OSError, ValueError):
        return {}
    return {msgid: msgstr for msgid, msgstr, fuzzy in parse_po(text) if msgid and msgstr and not fuzzy}


def parse_po(text):
    """Yield (msgid, msgstr, fuzzy) of a .po file; plural forms are not used here."""
    entry, field, fuzzy = {}, None, False
    for line in text.splitlines() + [""]:
        line = line.strip()
        if not line:
            if "msgid" in entry:
                yield entry["msgid"], entry.get("msgstr", ""), fuzzy
            entry, field, fuzzy = {}, None, False
        elif line.startswith("#,"):
            fuzzy = "fuzzy" in line
        elif line.startswith("#"):
            continue
        elif line.startswith('"'):
            if field:
                entry[field] += ast.literal_eval(line)
        else:
            field, _sep, value = line.partition(" ")
            entry[field] = ast.literal_eval(value)


@functools.lru_cache(maxsize=None)
def languages():
    """Languages with a catalog, plus English."""
    found = {SOURCE_LANGUAGE}
    try:
        for f in resources.files("scan_watch").joinpath("locale").iterdir():
            if f.name.endswith(".po"):
                found.add(f.name[:-3])
    except OSError:
        pass
    return sorted(found)


def match(tag):
    """'de_DE.UTF-8', 'de-AT' or 'de' -> 'de' if available, else None."""
    lang = re.split(r"[_.@-]", tag.strip().lower(), maxsplit=1)[0]
    return lang if lang in languages() else None


def server_language():
    """Language of terminal output and log: English unless use_locale() was called."""
    return _terminal_language


def locale_language(environ=os.environ):
    """Language of the locale (LC_ALL, LC_MESSAGES, LANG) if available, else English."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = environ.get(name, "")
        if value:
            # C/POSIX and unavailable languages mean English.
            return match(value) or SOURCE_LANGUAGE
    return SOURCE_LANGUAGE


def use_locale():
    """For an interactive terminal session only; output that may end up in a log stays English."""
    global _terminal_language
    _terminal_language = locale_language()


def negotiate(accept_language):
    """Best available language for an Accept-Language header, else English."""
    ranked = []
    for i, part in enumerate((accept_language or "").split(",")):
        tag, _sep, params = part.partition(";")
        q = 1.0
        m = re.search(r"q=([0-9.]+)", params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        if tag.strip() and q > 0:
            ranked.append((-q, i, tag))
    for *_rank, tag in sorted(ranked):
        found = match(tag)
        if found:
            return found
    return SOURCE_LANGUAGE
