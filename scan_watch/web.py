"""Web front end: status, recent documents, settings and a live log.

A single page talks to a small JSON API and receives the watcher's events
as Server-Sent Events. Only the standard library is used.

Settings fixed by environment variables or arguments are shown read-only;
everything else can be changed here, including output folder, Paperless URL
and token. Those let a visitor redirect scans and the token, so whoever wants
them out of reach sets them in .env. The token is never sent to the browser.
"""

import base64
import hmac
import json
import os
import threading
import time
from collections import OrderedDict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .i18n import Text, _, catalog, negotiate, render
from .paperless import cert_fingerprint, format_fingerprint
from .settings import (COLOR_MODES, DEFAULTS, PAPER_SIZES, RESOLUTIONS, SPLIT_MODES, TARGETS, TOKEN_ENV,
                       paperless_token, parse_value, save_settings, uses_paperless)

# Everything not fixed by the environment can be changed here. Whoever wants the
# output folder, Paperless URL or token out of reach of the page sets them in .env.
EDITABLE = ["color", "dpi", "duplex", "paper", "skip_blank", "blank_threshold", "split", "target",
            "paperless_tags", "prefix", "host", "auto_pause", "snmp_community", "out", "paperless_url", "paperless_public_url", "paperless_token"]
CHOICES = {
    "color": [[k, v[0]] for k, v in COLOR_MODES.items()],
    "dpi": [[r, f"{r} dpi"] for r in RESOLUTIONS],
    "paper": [[k, v[0]] for k, v in PAPER_SIZES.items()],
    "split": [[k, v] for k, v in SPLIT_MODES.items()],
    "target": [[k, v] for k, v in TARGETS.items()],
}
# A profile is a named set of these settings, kept per scanner in settings["profiles"]:
# {device: {name: {key: value}}}, device being the serial number (or "host:<address>").
PROFILE_KEYS = ["color", "dpi", "duplex", "paper", "split", "skip_blank", "blank_threshold", "target", "prefix",
                "paperless_tags"]
MAX_PROFILES = 20
MAX_PROFILE_NAME = 40
MAX_DOCS = 30
CSRF_HEADER = "X-Scan-Watch"
OUT_LABEL_ENV = "SCAN_WATCH_OUT_LABEL"  # cross-site forms cannot set custom headers


class NotStored(Exception):
    """The change is in effect, but the settings file could not be written:
    it is lost on restart. Carries the message for the user."""


def not_stored(done, error):
    return NotStored(_("{done} Only until restart: {error}", done=done, error=error))


class EventHub:
    """Keeps recent events and documents and wakes up waiting event streams."""

    def __init__(self, maxlen=300):
        self._cond = threading.Condition()
        self._events = deque(maxlen=maxlen)
        self._next_id = 1
        self.docs = OrderedDict()  # name -> record, oldest first
        self.thumbs = {}
        self.unsaved = {}  # name -> PDF that could not be stored anywhere, offered for download
        self.conflict = None  # (time, message) of the last sign of another program using the scanner
        self.state = "starting"
        self.page = None
        self.fingerprint = None
        self.scanner = None
        self.scanner_icon = None  # (bytes, type)

    def add(self, event, message="", **data):
        with self._cond:
            if event == "conflict":
                self.conflict = (time.time(), message)
            self._track(event, data)
            data.pop("thumb", None)  # served separately, not JSON
            data.pop("icon", None)
            data.pop("pdf", None)
            # The message stays a Text and is rendered per reader (render_event).
            self._events.append({"id": self._next_id, "time": time.time(), "event": event,
                                 "message": message, "data": data})
            self._next_id += 1
            self._cond.notify_all()

    def _track(self, event, data):
        name = data.get("name")
        if event == "state":
            self.state = data["state"]
        elif event == "paper":
            self.page = None
        elif event == "page":
            self.page = data
        elif event == "scanner":
            self.scanner = data["info"]
            self.scanner_icon = (data["icon"], data["icon_type"]) if data.get("icon") else None
        elif event == "untrusted_cert":
            self.fingerprint = data["fingerprint"]
        elif event == "document":
            self.docs[name] = {"name": name, "pages": data["pages"], "time": time.time(), "path": None,
                               "folder_error": False, "paperless": None, "document": None, "error": None}
            self.thumbs[name] = data["thumb"]
            while len(self.docs) > MAX_DOCS:
                old = next(iter(self.docs))
                del self.docs[old]
                self.thumbs.pop(old, None)
                self.unsaved.pop(old, None)
        elif name in self.docs:
            doc = self.docs[name]
            if event == "saved":
                doc["path"] = data["path"]
            elif event == "save_error":
                doc["folder_error"] = True
            elif event == "unsaved":
                self.unsaved[name] = data["pdf"]
            elif event == "uploaded":
                doc["paperless"] = "uploaded"
            elif event == "upload_error":
                doc["paperless"] = "error"
            elif event == "paperless_done":
                doc["paperless"] = "done"
                doc["document"] = data.get("document")
            elif event == "paperless_failed":
                doc["paperless"] = "failed"
                doc["error"] = str(data.get("result") or "")

    def doc_path(self, name):
        with self._cond:
            doc = self.docs.get(name)
            return doc["path"] if doc else None

    def clear_fingerprint(self):
        with self._cond:
            self.fingerprint = None

    def last_id(self):
        with self._cond:
            return self._next_id - 1

    def recent(self, count):
        with self._cond:
            return list(self._events)[-count:]

    def wait(self, last_id, timeout):
        """Events newer than last_id, waiting up to timeout seconds for one."""
        with self._cond:
            self._cond.wait_for(lambda: self._next_id - 1 > last_id, timeout)
            return [e for e in self._events if e["id"] > last_id]

    def snapshot(self):
        with self._cond:
            docs = [dict(d, downloadable=bool(d["path"]), unsaved=d["name"] in self.unsaved, path=None)
                    for d in reversed(self.docs.values())]
            scanner = dict(self.scanner, icon=bool(self.scanner_icon)) if self.scanner else None
            return {"state": self.state, "page": self.page, "docs": docs, "scanner": scanner,
                    "fingerprint": format_fingerprint(self.fingerprint) if self.fingerprint else None}


def render_event(e, lang):
    return dict(e, message=render(e["message"], lang) if e["message"] else "",
                data={k: render(v, lang) if isinstance(v, Text) else v for k, v in e["data"].items()})


ICONS = {"/icon.svg": "image/svg+xml", "/icon.png": "image/png"}


def static(name):
    return resources.files("scan_watch").joinpath("static", name).read_bytes()


def parse_address(text):
    """"8080", ":8080" or "127.0.0.1:8080" -> (host, port)."""
    host, _sep, port = str(text).rpartition(":")
    return host.strip("[]"), int(port)


class WebUI:
    def __init__(self, address, settings, from_env, hub, watcher, password=None, save_error=None, emit=None):
        self.address = parse_address(address)
        self.settings = dict(settings)
        self.from_env = set(from_env)
        self.hub = hub
        self.watcher = watcher
        self.password = password or None
        self._lock = threading.Lock()
        self._last_profile = {}  # device -> name of the profile applied or saved last
        self.save_error = save_error  # why the settings file could not be written, if so
        self.emit = emit or hub.add  # like the watcher's events: terminal/log and this page
        self.server = None

    def start(self):
        handler = type("Handler", (Handler,), {"app": self})
        self.server = ThreadingHTTPServer(self.address, handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def status(self, lang):
        with self._lock:
            s = self.settings
        shown = {k: s[k] for k in DEFAULTS if k not in ("paperless_token", "paperless_tls_pin")}
        return dict(
            self.hub.snapshot(),
            settings=shown,
            token_set=bool(paperless_token(s)),
            pinned=bool(s["paperless_tls_pin"]),
            paused=self.watcher.paused,
            auto_pause=self.watcher.auto_pause,
            out_error=self._out_error(lang),
            settings_error=render(self.save_error, lang) if self.save_error else None,
            paperless=self._paperless(lang),
            conflict=self._conflict(lang),
            out_label=os.environ.get(OUT_LABEL_ENV) or None,
            profiles=self._profile_list(s),
            editable=[k for k in EDITABLE if k not in self.fixed()],
            env=sorted(self.fixed()),
            choices={key: [[value, render(text, lang)] for value, text in options] for key, options in CHOICES.items()},
            log=[render_event(e, lang) for e in self.hub.recent(100)],
            lang=lang,
            last_event=self.hub.last_id(),
        )

    def fixed(self):
        """Settings given by the environment or arguments; the token counts when set in .env."""
        return set(self.from_env) | ({"paperless_token"} if os.environ.get(TOKEN_ENV) else set())

    def change_settings(self, changes):
        """Validate and apply changed settings; returns a note for the user."""
        if not isinstance(changes, dict):
            raise ValueError(_("Invalid request"))
        fixed = self.fixed()
        with self._lock:
            s = dict(self.settings)
            for key, value in changes.items():
                if key in fixed:
                    raise ValueError(_("{key} is set by an environment variable", key=key))
                if key not in EDITABLE or value is None or isinstance(value, (dict, list)):
                    raise ValueError(_("{key} cannot be changed here", key=key))
                try:
                    s[key] = parse_value(key, str(value))
                except ValueError as e:
                    raise ValueError(_("{key}: {error}", key=key, error=e.args[0])) from None
            if not s["host"]:
                raise ValueError(_("Scanner address missing"))
            if not s["out"]:
                raise ValueError(_("Output folder missing"))
            if s["paperless_url"] != self.settings["paperless_url"] and "paperless_tls_pin" not in fixed:
                s["paperless_tls_pin"] = ""  # a pinned certificate belongs to the old server
            error = self._apply(s)
        message = (_("Applied – takes effect from the next stack on.") if self.watcher.state == "scanning"
                   else _("Applied."))
        if error:
            raise not_stored(message, error)
        return message

    def _paperless(self, lang):
        p = self.watcher.paperless_status
        if not p:
            return None
        return {"ok": p["ok"], "time": p["time"], "message": render(p["message"], lang) if p["message"] else None}

    def test_paperless(self):
        """Connection test from the configuration page."""
        if not uses_paperless(self.settings):
            raise ValueError(_("Paperless is not a target (see Target in the settings)"))
        result = self.watcher.check_paperless()
        if not result["ok"]:
            raise ValueError(result["message"] or _("Paperless is not reachable"))
        return _("Paperless is reachable.")

    def _conflict(self, lang):
        if not self.hub.conflict:
            return None
        when, message = self.hub.conflict
        return {"time": when, "message": render(message, lang)}

    def _out_error(self, lang):
        error = self.watcher.out_error
        if not error:
            return None
        # In Docker the output folder is a container path; name the host folder instead.
        label = os.environ.get(OUT_LABEL_ENV)
        if label and isinstance(error, Text) and "folder" in error.args:
            error = Text(error.msgid, **dict(error.args, folder=label))
        return render(error, lang)

    def _device(self):
        """Key the profiles of the current scanner are kept under, None until it is identified."""
        info = self.watcher.scanner_info
        if info is None:
            return None
        return info.get("serial") or f'host:{self.settings["host"]}'

    def _profiles(self, s, device):
        profiles = s.get("profiles")
        mine = profiles.get(device) if isinstance(profiles, dict) else None
        return dict(mine) if isinstance(mine, dict) else {}

    def _active_profile(self, s, device, profiles):
        """Name of the profile the settings match, or None.

        Profiles with the same values all match; the one used last wins, else the first.
        Settings fixed by environment or arguments are not compared: a profile cannot change them.
        """
        matching = [name for name, values in profiles.items()
                    if all(s.get(k) == v for k, v in values.items() if k not in self.from_env)]
        last = self._last_profile.get(device)
        return last if last in matching else (matching[0] if matching else None)

    def _profile_list(self, s):
        device = self._device()
        if device is None:
            return None
        profiles = self._profiles(s, device)
        active = self._active_profile(s, device, profiles)
        return [{"name": name, "active": name == active, "values": {k: v for k, v in values.items() if k in PROFILE_KEYS}}
                for name, values in profiles.items()]

    @staticmethod
    def _profile_name(name):
        name = " ".join(str(name or "").split())
        if not name or len(name) > MAX_PROFILE_NAME or not name.isprintable():
            raise ValueError(_("Profile name: 1 to {max} characters", max=MAX_PROFILE_NAME))
        return name

    def _store_profiles(self, device, mine):
        """Keep the profiles of one scanner; called with the lock held."""
        profiles = dict(self.settings.get("profiles") or {})
        profiles[device] = mine
        self.settings = dict(self.settings, profiles={d: p for d, p in profiles.items() if p})
        # Only stored: the watcher does not use profiles, no need to restart anything.
        return self._save()

    def profile(self, action, name, new_name=None, values=None, overwrite=False):
        """Apply, save the current settings as, edit (values, name) or delete a profile of the current scanner.

        The name is the key. Saving under an existing name needs overwrite=True, so a
        browser that does not know the profile yet cannot replace it unasked.
        """
        name = self._profile_name(name)
        device = self._device()
        if device is None:
            raise ValueError(_("Profiles are available once the scanner has been detected"))
        apply = None
        error = None
        with self._lock:
            mine = self._profiles(self.settings, device)
            if action != "save" and name not in mine:
                raise ValueError(_("Unknown profile “{name}”", name=name))
            if action == "apply":
                apply = mine[name]
                self._last_profile[device] = name
                message = _("Profile “{name}” active.", name=name)
            elif action == "save":
                if name in mine and overwrite is not True:
                    raise ValueError(_("A profile “{name}” already exists", name=name))
                if name not in mine and len(mine) >= MAX_PROFILES:
                    raise ValueError(_("At most {max} profiles per scanner", max=MAX_PROFILES))
                mine[name] = {k: self.settings[k] for k in PROFILE_KEYS}
                self._last_profile[device] = name
                error = self._store_profiles(device, mine)
                message = _("Profile “{name}” saved.", name=name)
            elif action == "update":
                new_name = self._profile_name(new_name or name)
                if new_name != name and new_name in mine:
                    raise ValueError(_("A profile “{name}” already exists", name=new_name))
                if not isinstance(values, dict):
                    raise ValueError(_("Invalid request"))
                updated = dict(mine[name])
                for key, value in values.items():
                    if key not in PROFILE_KEYS or value is None or isinstance(value, (dict, list)):
                        raise ValueError(_("{key} cannot be changed here", key=key))
                    if key in self.from_env:
                        raise ValueError(_("{key} is set by an environment variable", key=key))
                    try:
                        updated[key] = parse_value(key, str(value))
                    except ValueError as e:
                        raise ValueError(_("{key}: {error}", key=key, error=e.args[0])) from None
                # Editing the profile in use changes the current settings too, so it stays selected.
                if self._active_profile(self.settings, device, mine) == name:
                    apply = updated
                if self._last_profile.get(device) == name:
                    self._last_profile[device] = new_name
                mine = {(new_name if n == name else n): (updated if n == name else v) for n, v in mine.items()}
                error = self._store_profiles(device, mine)
                message = (_("Profile “{name}” saved and applied.", name=new_name) if apply
                           else _("Profile “{name}” saved.", name=new_name))
            elif action == "delete":
                del mine[name]
                error = self._store_profiles(device, mine)
                message = _("Profile “{name}” deleted.", name=name)
            else:
                raise ValueError(_("Invalid request"))
        if apply is not None:
            # Settings fixed by environment or arguments stay as they are.
            try:
                self.change_settings({k: v for k, v in apply.items() if k in PROFILE_KEYS and k not in self.from_env})
            except NotStored:
                error = self.save_error
            if action == "update":
                self._last_profile[device] = new_name
        self.emit("profiles", message, name=name)
        if error:
            raise not_stored(message, error)
        return message

    def pause(self, paused):
        if not isinstance(paused, bool):
            raise ValueError(_("Invalid request"))
        self.watcher.pause(paused)
        return _("Paused.") if paused else _("Resumed.")

    def trust(self, fingerprint):
        """Pin the Paperless certificate the visitor has compared."""
        with self._lock:
            if "paperless_tls_pin" in self.from_env:
                raise ValueError(_("The certificate pin is set by an environment variable"))
            url = self.settings["paperless_url"]
            if not url.lower().startswith("https://"):
                raise ValueError(_("No HTTPS address entered for Paperless"))
            try:
                current = cert_fingerprint(url)
            except (OSError, ValueError) as e:
                raise ValueError(_("Could not read the certificate ({error})", error=e)) from None
            if format_fingerprint(current) != str(fingerprint).upper():
                raise ValueError(_("The server now shows a different certificate – reload the page"))
            error = self._apply(dict(self.settings, paperless_tls_pin=current))
        if error:
            raise not_stored(_("Certificate pinned."), error)
        return _("Certificate pinned.")

    def _save(self):
        """Write the settings file (with the lock held); returns the error, if any.
        The log hears about it only when that changes."""
        old, self.save_error = self.save_error, save_settings(self.settings)
        if self.save_error and str(self.save_error) != str(old):
            self.emit("settings_error", _("Note: {message}", message=self.save_error))
        elif old and not self.save_error:
            self.emit("settings_ok", _("Settings file is writable again; changes are saved."))
        return self.save_error

    def check_settings_file(self):
        """Save again (on every stack): notices lost rights, and stores what could not
        be saved before once the file is writable again."""
        with self._lock:
            self._save()

    def _apply(self, s):
        self.settings = s
        error = self._save()
        self.hub.clear_fingerprint()  # reported again if still untrusted
        self.watcher.update(s)
        return error

    def file_path(self, name):
        path = self.hub.doc_path(name)
        return Path(path) if path and Path(path).is_file() else None

    def pdf(self, name):
        """The saved PDF, or one kept in memory because it could not be stored: (bytes, name) or None."""
        path = self.file_path(name)
        if path:
            return path.read_bytes(), path.name
        data = self.hub.unsaved.get(name)
        return (data, name) if data else None


class Handler(BaseHTTPRequestHandler):
    app = None  # set by WebUI.start()
    server_version = "scan-watch"

    def log_message(self, format, *args):
        pass

    def _send(self, status, body=b"", content_type="application/json", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=HTTPStatus.OK):
        self._send(status, json.dumps(data).encode())

    @property
    def lang(self):
        return negotiate(self.headers.get("Accept-Language"))

    def _error(self, text, status):
        self._json({"error": render(text, self.lang)}, status)

    def _authorized(self):
        if not self.app.password:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                password = base64.b64decode(auth[6:]).decode().partition(":")[2]
            except ValueError:
                return False
            return hmac.compare_digest(password.encode(), self.app.password.encode())
        return False

    def _deny(self):
        self._send(HTTPStatus.UNAUTHORIZED, render(_("Login required"), self.lang).encode(), "text/plain; charset=utf-8",
                   [("WWW-Authenticate", 'Basic realm="scan-watch", charset="UTF-8"')])

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/healthz":
            return self._send(HTTPStatus.OK, b"ok", "text/plain")
        if url.path in ICONS:  # no secret in them, and browsers fetch them without asking for a password
            return self._send(HTTPStatus.OK, static(url.path[1:]), ICONS[url.path])
        if not self._authorized():
            return self._deny()
        if url.path == "/":
            return self._send(HTTPStatus.OK, static("index.html"), "text/html; charset=utf-8",
                              [("Content-Security-Policy", "default-src 'self'; img-src 'self'; "
                                "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")])
        if url.path == "/api/status":
            return self._json(self.app.status(self.lang))
        if url.path == "/api/i18n":
            return self._json({"lang": self.lang, "messages": catalog(self.lang)})
        if url.path == "/api/events":
            return self._stream(url)
        if url.path.startswith("/api/thumb/"):
            thumb = self.app.hub.thumbs.get(unquote(url.path[len("/api/thumb/"):]))
            if thumb:
                return self._send(HTTPStatus.OK, thumb, "image/jpeg")
        if url.path == "/api/scanner-icon" and self.app.hub.scanner_icon:
            return self._send(HTTPStatus.OK, *self.app.hub.scanner_icon)
        if url.path.startswith("/api/file/"):
            found = self.app.pdf(unquote(url.path[len("/api/file/"):]))
            if found:
                data, name = found
                return self._send(HTTPStatus.OK, data, "application/pdf",
                                  [("Content-Disposition", f'inline; filename="{name}"')])
        self._error(_("Not found"), HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        if self.headers.get(CSRF_HEADER) != "1":
            return self._error(_("Request rejected"), HTTPStatus.FORBIDDEN)
        actions = {"/api/settings": lambda body: self.app.change_settings(body),
                   "/api/trust": lambda body: self.app.trust(body.get("fingerprint", "")),
                   "/api/pause": lambda body: self.app.pause(body.get("paused")),
                   "/api/paperless-test": lambda body: self.app.test_paperless(),
                   "/api/profile": lambda body: self.app.profile(body.get("action"), body.get("name"),
                                                                 body.get("new_name"), body.get("values"),
                                                                 body.get("overwrite", False))}
        action = actions.get(urlsplit(self.path).path)
        if action is None:
            return self._error(_("Not found"), HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError(_("Invalid request"))
            message = action(body)
        except NotStored as e:
            return self._json({"message": render(e.args[0], self.lang), "stored": False})
        except ValueError as e:  # includes malformed JSON
            return self._error(e, HTTPStatus.BAD_REQUEST)
        self._json({"message": render(message, self.lang), "stored": True})

    def _stream(self, url):
        """Server-Sent Events; the browser resumes via Last-Event-ID after a reconnect."""
        try:
            last = int(self.headers.get("Last-Event-ID") or parse_qs(url.query).get("since", ["0"])[0])
        except ValueError:
            last = 0
        lang = self.lang
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                events = self.app.hub.wait(last, timeout=15)
                for e in events:
                    self.wfile.write(f"id: {e['id']}\ndata: {json.dumps(render_event(e, lang))}\n\n".encode())
                    last = e["id"]
                if not events:
                    self.wfile.write(b": ping\n\n")  # notices closed connections
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
