"""Scan settings: defaults, the JSON file they are remembered in, and
overrides from SCAN_WATCH_* environment variables (e.g. a Docker .env file)."""

import json
import os
import re
from pathlib import Path

from .i18n import _

ENV_PREFIX = "SCAN_WATCH_"
TOKEN_ENV = ENV_PREFIX + "PAPERLESS_TOKEN"
CONFIG_FILE = Path(os.environ.get(ENV_PREFIX + "CONFIG") or Path.home() / ".config" / "scan-watch.json")

# Sizes in 1/300 inch, the unit eSCL uses for scan regions.
PAPER_SIZES = {
    "a4": ("A4", 2480, 3508),
    "a5": ("A5", 1748, 2480),
    "letter": ("Letter", 2550, 3300),
    "legal": (_("Legal / max. length"), 2550, 4200),
}
COLOR_MODES = {"color": (_("Color"), "RGB24"), "gray": (_("Grayscale"), "Grayscale8")}
RESOLUTIONS = [100, 150, 200, 240, 300]
TARGETS = {"folder": _("Folder"), "paperless": "Paperless", "both": _("Folder + Paperless")}
SPLIT_MODES = {"stack": _("one document per stack"), "sheet": _("one document per sheet")}
CHOICES = {"color": COLOR_MODES, "dpi": RESOLUTIONS, "paper": PAPER_SIZES,
           "target": TARGETS, "split": SPLIT_MODES}

DEFAULTS = {
    "host": "",
    "target": "folder",
    "out": str(Path.home() / "Scans"),
    "paperless_url": "",
    # Address of Paperless for links in the browser, when it differs from the one
    # scan-watch uses (e.g. http://paperless:8000 inside a Docker network). Empty: the same.
    "paperless_public_url": "",
    "paperless_token": "",
    "paperless_tags": "",
    "paperless_tls_pin": "",
    "color": "color",
    "dpi": 300,
    "duplex": True,
    "paper": "a4",
    "skip_blank": True,
    "blank_threshold": 1.0,
    "split": "stack",
    "prefix": "scan",
    "auto_pause": 30,  # minutes without paper until the watcher pauses itself, 0 = never
    # SNMP v1 community of the scanner: an automatic pause ends when it wakes up
    # (Canon DR-S150 only, see drivers/canon_dr_s150.py). Empty: off.
    "snmp_community": "",
}

TRUE_WORDS = ("1", "true", "yes", "on", "ja")
FALSE_WORDS = ("0", "false", "no", "off", "nein")


# Values that end up in file names and URLs must not be able to escape them.
PREFIX_RE = re.compile(r"[\w.-]+")
HOST_RE = re.compile(r"[\w.-]+(:\d+)?|\[[0-9a-fA-F:]+\](:\d+)?")
COMMUNITY_RE = re.compile(r"[!-~]{1,64}")  # printable ASCII without spaces


def parse_value(key, raw):
    """Convert text (an environment variable, a form field) to the type of the setting."""
    default = DEFAULTS[key]
    raw = raw.strip()
    if isinstance(default, bool):
        if raw.lower() not in TRUE_WORDS + FALSE_WORDS:
            raise ValueError(_("“{value}” is neither true nor false", value=raw))
        value = raw.lower() in TRUE_WORDS
    elif isinstance(default, int):
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(_("“{value}” is not a whole number", value=raw)) from None
    elif isinstance(default, float):
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            raise ValueError(_("“{value}” is not a number", value=raw)) from None
    elif key == "paperless_tls_pin":
        # Accept the fingerprint as printed (AB:CD:…) or as plain hex.
        value = raw.replace(":", "").lower()
    else:
        value = raw
    if key in CHOICES and value not in CHOICES[key]:
        options = ", ".join(str(c) for c in CHOICES[key])
        raise ValueError(_("“{value}” is not one of {options}", value=raw, options=options))
    if key == "prefix" and not PREFIX_RE.fullmatch(value):
        raise ValueError(_("“{value}”: only letters, digits, dot, underscore and hyphen are allowed", value=raw))
    if key in ("paperless_url", "paperless_public_url") and value and not value.lower().startswith(("http://", "https://")):
        raise ValueError(_("“{value}” is not an http:// or https:// address", value=raw))
    if key == "auto_pause" and value < 0:
        raise ValueError(_("“{value}” must not be negative", value=raw))
    if key == "host" and value and not HOST_RE.fullmatch(value):
        raise ValueError(_("“{value}” is not a valid scanner address", value=raw))
    if key == "snmp_community" and value and not COMMUNITY_RE.fullmatch(value):
        raise ValueError(_("“{value}”: at most 64 characters, no spaces", value=raw))
    return value


def env_overrides(environ=os.environ):
    """Settings given as SCAN_WATCH_<NAME> variables; empty variables are ignored."""
    found = {}
    for key in DEFAULTS:
        if key == "paperless_token":
            continue  # read on use via paperless_token(), never stored
        raw = environ.get(ENV_PREFIX + key.upper(), "")
        if raw.strip():
            try:
                found[key] = parse_value(key, raw)
            except ValueError as e:
                raise ValueError(_("{name}: {error}", name=ENV_PREFIX + key.upper(), error=e.args[0])) from None
    return found


def load_settings():
    settings = dict(DEFAULTS)
    try:
        settings.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    settings.pop("env_defaults", None)  # left by an earlier version
    return settings


def save_settings(settings):
    """Store the settings; returns an error message or None."""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        # The file may hold the Paperless API token, so keep it private.
        fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        return _("Settings not saved ({error})", error=e)
    return None


def paperless_token(s):
    return os.environ.get(TOKEN_ENV) or s["paperless_token"]


def uses_paperless(s):
    return s["target"] in ("paperless", "both")


def uses_folder(s):
    return s["target"] in ("folder", "both")


def tag_names(s):
    return [t.strip() for t in s["paperless_tags"].split(",") if t.strip()]
