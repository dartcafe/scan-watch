import base64
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from scan_watch import web
from scan_watch.settings import DEFAULTS


class FakeWatcher:
    state = "waiting"
    paused = False
    auto_pause = None
    scanner_info = None
    out_error = None
    paperless_status = None

    def check_paperless(self, trust_cert=None):
        return self.paperless_status

    def __init__(self):
        self.updates = []

    def update(self, settings):
        self.updates.append(settings)

    def pause(self, paused=True):
        self.paused = paused


class WebTest(unittest.TestCase):
    password = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        save = mock.patch.object(web, "save_settings", return_value=None)
        self.save = save.start()
        self.addCleanup(save.stop)
        self.settings = dict(DEFAULTS, host="scanner", paperless_token="secret-token", out=self.tmp.name,
                             paperless_url="https://paperless.lan")
        self.hub = web.EventHub()
        self.watcher = FakeWatcher()
        self.ui = web.WebUI("127.0.0.1:0", self.settings, {"target"}, self.hub, self.watcher, self.password)
        self.base = f"http://127.0.0.1:{self.ui.start()}"
        self.addCleanup(self.ui.stop)

    def request(self, path, body=None, headers=None, auth=None):
        headers = dict(headers or {})
        if body is not None:
            headers.setdefault("X-Scan-Watch", "1")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
        req = urllib.request.Request(self.base + path, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers


class ApiTest(WebTest):
    def test_status_never_contains_the_token(self):
        status, body, _ = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertNotIn(b"secret-token", body)
        data = json.loads(body)
        self.assertTrue(data["token_set"])
        self.assertNotIn("target", data["editable"])  # fixed in these tests (from the environment)
        self.assertIn("target", data["env"])

    def test_page_is_served(self):
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>scan-watch</title>", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_change_settings(self):
        status, body, _ = self.request("/api/settings", {"dpi": 200, "duplex": False, "blank_threshold": "1,5"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.watcher.updates[-1]["dpi"], 200)
        self.assertIs(self.watcher.updates[-1]["duplex"], False)
        self.assertEqual(self.watcher.updates[-1]["blank_threshold"], 1.5)
        self.save.assert_called_once()

    def test_out_url_and_token_are_editable_unless_set_in_the_environment(self):
        self.ui.settings["paperless_tls_pin"] = "ab" * 32
        status, body, _h = self.request("/api/settings", {"out": "/srv/scans", "paperless_url": "https://other.lan",
                                                          "paperless_token": "new-token"})
        self.assertEqual(status, 200, body)
        update = self.watcher.updates[-1]
        self.assertEqual((update["out"], update["paperless_url"], update["paperless_token"]),
                         ("/srv/scans", "https://other.lan", "new-token"))
        self.assertEqual(update["paperless_tls_pin"], "")  # the pin belonged to the old server
        self.assertNotIn(b"new-token", self.request("/api/status")[1])
        with mock.patch.dict("os.environ", {"SCAN_WATCH_PAPERLESS_TOKEN": "from-env"}):
            self.assertEqual(self.request("/api/settings", {"paperless_token": "x"})[0], 400)
            self.assertNotIn("paperless_token", json.loads(self.request("/api/status")[1])["editable"])
        self.ui.from_env = {"out", "paperless_url"}
        for body in ({"out": "/etc"}, {"paperless_url": "https://evil"}):
            self.assertEqual(self.request("/api/settings", body)[0], 400, body)

    def test_public_paperless_url_for_links(self):
        status, body, _h = self.request("/api/settings", {"paperless_public_url": "https://docs.lan"})
        self.assertEqual(status, 200, body)
        settings = json.loads(self.request("/api/status")[1])["settings"]
        self.assertEqual((settings["paperless_url"], settings["paperless_public_url"]),
                         ("https://paperless.lan", "https://docs.lan"))  # scan-watch keeps using its own

    def test_rejected_settings(self):
        for body in ({"dpi": 600}, {"prefix": "../x"}, {"host": "evil/x?"}, {"target": "folder"},
                     {"host": ""}, {"out": ""}, {"paperless_url": "ftp://x"}, {"color": None}):
            status, _, _ = self.request("/api/settings", body)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.watcher.updates, [])

    def test_pause_and_resume(self):
        status, _, _ = self.request("/api/pause", {"paused": True})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(self.request("/api/status")[1])["paused"])
        self.request("/api/pause", {"paused": False})
        self.assertFalse(self.watcher.paused)
        self.assertEqual(self.request("/api/pause", {"paused": "ja"})[0], 400)

    def test_scanner_info_and_icon(self):
        self.assertEqual(self.request("/api/scanner-icon")[0], 404)
        self.hub.add("scanner", "Scanner erkannt", info={"model": "CANON DR-S150"}, icon=b"PNG", icon_type="image/png")
        data = json.loads(self.request("/api/status")[1])
        self.assertEqual(data["scanner"], {"model": "CANON DR-S150", "icon": True})
        status, body, headers = self.request("/api/scanner-icon")
        self.assertEqual((status, body, headers["Content-Type"]), (200, b"PNG", "image/png"))

    def test_language_follows_the_browser(self):
        self.hub.add("info", web._("New settings applied."))
        for lang, error, log, choice in (("de-DE,de;q=0.9", "Scanner-Adresse fehlt", "Neue Einstellungen übernommen.",
                                          "Farbe"),
                                         ("en-US,en;q=0.9", "Scanner address missing", "New settings applied.",
                                          "Color")):
            headers = {"Accept-Language": lang}
            status, body, _h = self.request("/api/settings", {"host": ""}, headers=headers)
            self.assertEqual((status, json.loads(body)["error"]), (400, error))
            data = json.loads(self.request("/api/status", headers=headers)[1])
            self.assertEqual(data["log"][-1]["message"], log)
            self.assertIn(["color", choice], data["choices"]["color"])
        i18n = json.loads(self.request("/api/i18n", headers={"Accept-Language": "de"})[1])
        self.assertEqual(i18n["lang"], "de")
        self.assertEqual(i18n["messages"]["Save"], "Speichern")

    def profile(self, action, name, **extra):
        status, body, _h = self.request("/api/profile", dict(extra, action=action, name=name))
        return status, json.loads(body)

    def profiles(self):
        return json.loads(self.request("/api/status")[1])["profiles"]

    def test_profiles_need_a_detected_scanner(self):
        self.assertIsNone(self.profiles())
        self.assertEqual(self.profile("save", "Stapel")[0], 400)

    def test_save_apply_and_delete_profiles(self):
        self.watcher.scanner_info = {"serial": "JU1"}
        self.assertEqual(self.profiles(), [])
        self.assertEqual(self.profile("save", "  Stapel  ")[0], 200)  # whitespace is trimmed
        self.request("/api/settings", {"split": "sheet", "paperless_tags": "Beleg"})
        self.assertEqual(self.profile("save", "Einzelblätter")[0], 200)
        self.assertEqual([(p["name"], p["active"]) for p in self.profiles()], [("Stapel", False), ("Einzelblätter", True)])
        self.assertEqual(self.profiles()[1]["values"]["paperless_tags"], "Beleg")

        self.assertEqual(self.profile("apply", "Stapel")[0], 200)
        self.assertEqual(self.watcher.updates[-1]["split"], "stack")
        self.assertEqual(self.watcher.updates[-1]["paperless_tags"], "")
        self.assertEqual([p["active"] for p in self.profiles()], [True, False])
        saved = self.save.call_args[0][0]["profiles"]
        self.assertEqual(list(saved["JU1"]), ["Stapel", "Einzelblätter"])
        self.assertNotIn("host", saved["JU1"]["Stapel"])  # only scan settings, not the device address

        self.assertEqual(self.profile("delete", "Stapel")[0], 200)
        self.assertEqual([p["name"] for p in self.profiles()], ["Einzelblätter"])
        self.assertEqual(self.profile("apply", "Stapel")[0], 400)

    def test_overwriting_a_profile_needs_confirmation(self):
        self.watcher.scanner_info = {"serial": "A"}
        self.profile("save", "Stapel")
        self.request("/api/settings", {"dpi": 150})
        status, body = self.profile("save", "Stapel")
        self.assertEqual(status, 400)
        self.assertEqual(self.profiles()[0]["values"]["dpi"], 300)
        self.assertEqual(self.profile("save", " Stapel ", overwrite=True)[0], 200)
        self.assertEqual([(p["name"], p["values"]["dpi"]) for p in self.profiles()], [("Stapel", 150)])
        self.assertEqual(self.profile("save", "stapel")[0], 200)  # case matters: another profile
        self.assertEqual(len(self.profiles()), 2)

    def test_only_one_of_identical_profiles_is_active(self):
        self.watcher.scanner_info = {"serial": "A"}
        self.profile("save", "Ordner")
        self.profile("save", "Ordner 2")  # same values
        self.assertEqual([p["active"] for p in self.profiles()], [False, True])  # the one saved last
        self.profile("apply", "Ordner")
        self.assertEqual([p["active"] for p in self.profiles()], [True, False])
        self.request("/api/settings", {"dpi": 150})
        self.assertEqual([p["active"] for p in self.profiles()], [False, False])
        self.request("/api/settings", {"dpi": 300})  # matches both again: still the one used last
        self.assertEqual([p["active"] for p in self.profiles()], [True, False])

    def test_edit_profile_without_touching_the_current_settings(self):
        self.watcher.scanner_info = {"serial": "A"}
        self.profile("save", "Stapel")
        self.profile("save", "Einzeln")
        self.request("/api/settings", {"dpi": 150})  # now neither profile is in use
        updates = len(self.watcher.updates)
        status, body = self.profile("update", "Einzeln", new_name="Belege", values={"split": "sheet", "dpi": "200"})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.watcher.updates), updates)  # current settings unchanged
        profiles = {p["name"]: p["values"] for p in self.profiles()}
        self.assertEqual(list(profiles), ["Stapel", "Belege"])  # renamed in place
        self.assertEqual((profiles["Belege"]["split"], profiles["Belege"]["dpi"]), ("sheet", 200))

    def test_editing_the_profile_in_use_applies_it(self):
        self.watcher.scanner_info = {"serial": "A"}
        self.profile("save", "Stapel")
        status, body = self.profile("update", "Stapel", values={"color": "gray"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.watcher.updates[-1]["color"], "gray")
        self.assertEqual([p["active"] for p in self.profiles()], [True])

    def test_invalid_profile_edits(self):
        self.watcher.scanner_info = {"serial": "A"}
        self.profile("save", "Stapel")
        self.profile("save", "Einzeln")
        for extra in ({"new_name": "Stapel"}, {"values": {"dpi": 600}}, {"values": {"host": "evil"}},
                      {"values": {"out": "/etc"}}, {"values": "x"}, {"new_name": "  "}):
            extra.setdefault("values", {})
            status, _b = self.profile("update", "Einzeln", **extra)
            self.assertEqual(status, 400, extra)
        self.assertEqual(self.profile("update", "Gibt es nicht", values={})[0], 400)

    def test_profiles_belong_to_one_scanner(self):
        self.watcher.scanner_info = {"serial": "A"}
        self.profile("save", "Stapel")
        self.watcher.scanner_info = {"serial": "B"}
        self.assertEqual(self.profiles(), [])
        self.watcher.scanner_info = {}  # no serial number: keyed by address
        self.profile("save", "Ohne Seriennummer")
        self.assertIn("host:scanner", self.save.call_args[0][0]["profiles"])

    def test_profile_leaves_fixed_settings_alone(self):
        self.watcher.scanner_info = {"serial": "A"}
        self.profile("save", "Stapel")
        self.ui.settings["profiles"]["A"]["Stapel"]["target"] = "paperless"  # "target" is fixed in these tests
        self.assertEqual(self.profile("apply", "Stapel")[0], 200)
        self.assertEqual(self.watcher.updates[-1]["target"], "folder")
        self.assertEqual([p["active"] for p in self.profiles()], [True])  # the fixed value does not count
        self.assertEqual(self.profile("update", "Stapel", values={"target": "both"})[0], 400)
        self.assertEqual(self.profile("update", "Stapel", values={"dpi": "200"})[0], 200)

    def test_invalid_profile_requests(self):
        self.watcher.scanner_info = {"serial": "A"}
        for action, name in (("save", ""), ("save", "x" * 41), ("save", "a\x00b"), ("rename", "x"), ("delete", "nope")):
            self.assertEqual(self.profile(action, name)[0], 400, (action, name))

    def test_unsaved_pdf_can_be_downloaded(self):
        self.hub.add("document", name="x.pdf", pages=1, thumb=b"")
        self.hub.add("save_error", "error", name="x.pdf")
        self.hub.add("unsaved", "unsaved", name="x.pdf", pdf=b"%PDF-1.4 test")
        doc = json.loads(self.request("/api/status")[1])["docs"][0]
        self.assertEqual((doc["unsaved"], doc["folder_error"], doc["downloadable"]), (True, True, False))
        self.assertNotIn("pdf", json.loads(self.request("/api/status")[1])["log"][-1]["data"])
        status, body, _h = self.request("/api/file/x.pdf")
        self.assertEqual((status, body), (200, b"%PDF-1.4 test"))

    def test_unwritable_settings_file_is_reported(self):
        self.save.return_value = web._("Settings not saved ({error})", error="Permission denied")
        self.watcher.scanner_info = {"serial": "A"}
        headers = {"Accept-Language": "de"}
        status, body, _h = self.request("/api/settings", {"dpi": 200}, headers=headers)
        answer = json.loads(body)
        self.assertEqual((status, answer["stored"]), (200, False))
        self.assertIn("nur bis zum Neustart", answer["message"])
        self.assertEqual(self.watcher.updates[-1]["dpi"], 200)  # in effect anyway
        for action in ("save", "apply"):
            status, body, _h = self.request("/api/profile", {"action": action, "name": "Stapel"}, headers=headers)
            self.assertEqual((status, json.loads(body)["stored"]), (200, False), action)
        self.assertEqual([p["name"] for p in json.loads(self.request("/api/status")[1])["profiles"]], ["Stapel"])
        data = json.loads(self.request("/api/status", headers=headers)[1])
        self.assertIn("Permission denied", data["settings_error"])
        self.save.return_value = None  # writable again: the notice goes away with the next save
        self.assertTrue(json.loads(self.request("/api/settings", {"dpi": 300})[1])["stored"])
        self.assertIsNone(json.loads(self.request("/api/status")[1])["settings_error"])

    def test_settings_file_is_checked_on_every_stack(self):
        self.ui.check_settings_file()
        self.assertIsNone(self.ui.save_error)
        self.save.return_value = web._("Settings not saved ({error})", error="Permission denied")
        self.ui.check_settings_file()
        self.ui.check_settings_file()  # reported once, not on every stack
        names = [e["event"] for e in self.hub.recent(50)]
        self.assertEqual(names.count("settings_error"), 1)
        self.save.return_value = None
        self.ui.check_settings_file()  # writable again: what was not saved is saved now
        self.assertIsNone(self.ui.save_error)
        self.assertIn("settings_ok", [e["event"] for e in self.hub.recent(50)])

    def test_paperless_connection_test(self):
        self.ui.settings["target"] = "paperless"
        self.watcher.paperless_status = {"ok": True, "time": 1.0, "message": None}
        status, body, _h = self.request("/api/paperless-test", {})
        self.assertEqual((status, json.loads(body)["message"]), (200, "Paperless is reachable."))
        self.assertEqual(json.loads(self.request("/api/status")[1])["paperless"]["ok"], True)
        self.watcher.paperless_status = {"ok": False, "time": 2.0, "message": web._("Paperless: {error}", error="timed out")}
        status, body, _h = self.request("/api/paperless-test", {}, headers={"Accept-Language": "de"})
        self.assertEqual((status, json.loads(body)["error"]), (400, "Paperless: timed out"))
        self.ui.settings["target"] = "folder"
        self.assertEqual(self.request("/api/paperless-test", {})[0], 400)

    def test_startup_save_error_is_shown(self):
        ui = web.WebUI("127.0.0.1:0", self.settings, set(), self.hub, self.watcher,
                       save_error=web._("Settings not saved ({error})", error="Permission denied"))
        self.assertIn("Permission denied", ui.status("en")["settings_error"])

    def test_post_without_csrf_header_is_rejected(self):
        status, _, _ = self.request("/api/settings", {"dpi": 200}, headers={"X-Scan-Watch": "0"})
        self.assertEqual(status, 403)
        self.assertEqual(self.watcher.updates, [])

    def test_malformed_json_is_rejected(self):
        req = urllib.request.Request(self.base + "/api/settings", data=b"{nope",
                                     headers={"X-Scan-Watch": "1", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_trust_requires_the_current_fingerprint(self):
        with mock.patch.object(web, "cert_fingerprint", return_value="ab" * 32):
            status, _, _ = self.request("/api/trust", {"fingerprint": "CD:" * 31 + "CD"})
            self.assertEqual(status, 400)
            status, _, _ = self.request("/api/trust", {"fingerprint": "AB:" * 31 + "AB"})
            self.assertEqual(status, 200)
        self.assertEqual(self.watcher.updates[-1]["paperless_tls_pin"], "ab" * 32)

    def test_documents_thumbnails_and_files(self):
        pdf = Path(self.tmp.name, "scan_1.pdf")
        pdf.write_bytes(b"%PDF-1.4 test")
        self.hub.add("document", name="scan_1.pdf", pages=2, thumb=b"\xff\xd8jpeg")
        self.hub.add("saved", "Gespeichert", name="scan_1.pdf", path=str(pdf), pages=2)
        self.hub.add("paperless_done", "ok", name="scan_1.pdf", document=42)

        doc = json.loads(self.request("/api/status")[1])["docs"][0]
        self.assertEqual((doc["name"], doc["pages"], doc["paperless"], doc["document"]), ("scan_1.pdf", 2, "done", 42))
        self.assertTrue(doc["downloadable"])
        self.assertIsNone(doc["path"])  # server paths stay on the server
        self.assertEqual(self.request("/api/thumb/scan_1.pdf")[1], b"\xff\xd8jpeg")
        self.assertEqual(self.request("/api/file/scan_1.pdf")[1], b"%PDF-1.4 test")
        self.assertEqual(self.request("/api/file/..%2F..%2Fetc%2Fpasswd")[0], 404)

    def test_event_stream_resumes_after_given_id(self):
        self.hub.add("info", "eins")
        self.hub.add("info", "zwei")
        with urllib.request.urlopen(self.base + "/api/events?since=1", timeout=5) as resp:
            self.assertEqual(resp.headers["Content-Type"], "text/event-stream")
            lines = [resp.readline() for _ in range(3)]
        self.assertEqual(lines[0], b"id: 2\n")
        self.assertEqual(json.loads(lines[1][len(b"data: "):])["message"], "zwei")

    def test_docs_are_limited(self):
        for i in range(web.MAX_DOCS + 5):
            self.hub.add("document", name=f"d{i}.pdf", pages=1, thumb=b"x")
        self.assertEqual(len(self.hub.docs), web.MAX_DOCS)
        self.assertEqual(len(self.hub.thumbs), web.MAX_DOCS)
        self.assertNotIn("d0.pdf", self.hub.thumbs)


class AuthTest(WebTest):
    password = "geheim"

    def test_password_is_required(self):
        self.assertEqual(self.request("/api/status")[0], 401)
        self.assertEqual(self.request("/api/status", auth="x:falsch")[0], 401)
        self.assertEqual(self.request("/api/status", auth="x:geheim")[0], 200)
        self.assertEqual(self.request("/api/settings", {"dpi": 200})[0], 401)

    def test_health_check_and_icon_need_no_password(self):
        self.assertEqual(self.request("/healthz")[0], 200)
        status, body, headers = self.request("/icon.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", body)
        status, body, headers = self.request("/icon.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
