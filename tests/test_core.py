import atexit
import io
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from scan_watch import core
from scan_watch.settings import DEFAULTS


def page(text=True):
    img = Image.new("RGB", (620, 877), "white")  # A4 at 75 dpi
    if text:
        draw = ImageDraw.Draw(img)
        for y in range(100, 700, 20):
            draw.rectangle((80, y, 540, y + 8), fill="black")
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


TEXT, BLANK = page(True), page(False)


class FakeScanner:
    NAME = "eSCL"

    def __init__(self, pages, adf_states=()):
        self._pages = pages
        self._adf = list(adf_states)
        self.cancelled = []

    def start_job(self, s):
        return "job/"

    def pages(self, job):
        yield from self._pages

    def cancel(self, job):
        self.cancelled.append(job)

    def adf_state(self):
        return self._adf.pop(0) if self._adf else ("ScannerAdfEmpty", "Idle")

    def capabilities(self):
        return {"model": "CANON DR-S150", "serial": "JU1", "version": "2.9", "feeder_capacity": 100,
                "admin_url": "http://scanner", "icon_path": "/eSCL/icon.png"}

    def icon(self, path):
        return b"PNG", "image/png"

    def wake_watch(self, settings, clock):
        return None


TEST_OUT = tempfile.mkdtemp(prefix="scan-watch-test-")  # never the real ~/Scans
atexit.register(shutil.rmtree, TEST_OUT, True)


def watcher(**settings):
    s = dict(DEFAULTS, **dict({"host": "scanner", "dpi": 75, "out": TEST_OUT}, **settings))
    events = []
    w = core.Watcher(s, lambda event, message, **data: events.append((event, message, data)))
    return w, s, events


class ScanStackTest(unittest.TestCase):
    def scan(self, pages, **settings):
        w, s, events = watcher(**settings)
        w.scanner = FakeScanner(pages)
        docs, dropped = w.scan_stack(s)
        self.assertEqual(w.scanner.cancelled, ["job/"])
        return docs, dropped, events

    def test_stack_becomes_one_document_without_blank_pages(self):
        docs, dropped, _ = self.scan([TEXT, BLANK, TEXT, BLANK])
        self.assertEqual([len(d) for d in docs], [2])
        self.assertEqual(dropped, 2)

    def test_split_by_sheet_keeps_front_and_back_together(self):
        docs, dropped, _ = self.scan([TEXT, TEXT, TEXT, BLANK, BLANK, BLANK, TEXT, BLANK], split="sheet")
        self.assertEqual([len(d) for d in docs], [2, 1, 1])
        self.assertEqual(dropped, 4)

    def test_simplex_split_by_sheet(self):
        docs, _, _ = self.scan([TEXT, TEXT, BLANK], split="sheet", duplex=False)
        self.assertEqual([len(d) for d in docs], [1, 1])

    def test_blank_detection_can_be_switched_off(self):
        docs, dropped, events = self.scan([TEXT, BLANK], skip_blank=False)
        self.assertEqual([len(d) for d in docs], [2])
        self.assertEqual(dropped, 0)
        self.assertEqual([e[2]["page"] for e in events if e[0] == "page"], [1, 2])


class DeliverTest(unittest.TestCase):
    def test_folder_target_writes_numbered_pdfs(self):
        with tempfile.TemporaryDirectory() as out:
            w, s, events = watcher(out=out, prefix="t")
            w.deliver_all([[TEXT], [TEXT, TEXT]], 0, s)
            files = sorted(p.name for p in Path(out).iterdir())
            self.assertEqual(len(files), 2)
            self.assertTrue(files[0].startswith("t_") and files[0].endswith("_001.pdf"))
            self.assertTrue(all(Path(out, f).read_bytes().startswith(b"%PDF") for f in files))
            self.assertEqual([e[0] for e in events], ["document", "saved", "document", "saved", "info"])

    def test_paperless_unavailable_falls_back_to_folder(self):
        with tempfile.TemporaryDirectory() as out:
            w, s, events = watcher(out=out, target="paperless")
            w.paperless = None
            w.deliver([TEXT], "x.pdf", "1 Seite", s)
            self.assertTrue(Path(out, "x.pdf").exists())
            self.assertEqual([e[0] for e in events], ["document", "upload_error", "saved"])


class FakePaperless:
    def upload(self, pdf, name, tags):
        return "task"

    def watch_task(self, task, on_done):
        pass


class SaveErrorTest(unittest.TestCase):
    """A folder that cannot be written must never cost a scan or stop the watcher."""

    def setUp(self):
        blocker = tempfile.NamedTemporaryFile(delete=False)  # a file where the folder should be
        self.addCleanup(Path(blocker.name).unlink)
        self.out = blocker.name

    def test_folder_error_keeps_the_pdf_for_download(self):
        w, s, events = watcher(out=self.out)
        w.deliver([TEXT], "x.pdf", "1 page", s)
        self.assertEqual([e[0] for e in events], ["document", "save_error", "unsaved"])
        self.assertTrue(events[-1][2]["pdf"].startswith(b"%PDF"))

    def test_folder_error_with_paperless_still_uploads(self):
        w, s, events = watcher(out=self.out, target="both")
        w.paperless = (FakePaperless(), [])
        w.deliver([TEXT], "x.pdf", "1 page", s)
        self.assertEqual([e[0] for e in events], ["document", "save_error", "uploaded"])

    def test_nothing_works(self):
        w, s, events = watcher(out=self.out, target="paperless")
        w.paperless = None
        w.deliver([TEXT], "x.pdf", "1 page", s)
        self.assertEqual([e[0] for e in events], ["document", "upload_error", "save_error", "unsaved"])

    def test_unwritable_folder_is_reported_up_front(self):
        w, s, events = watcher(out=self.out)
        with mock.patch.object(core, "scanner_for"), mock.patch.object(core, "connect_paperless"):
            w._apply(s)
        self.assertIsNotNone(w.out_error)
        self.assertEqual([e[0] for e in events].count("out_error"), 1)
        w._check_out()  # still broken: no second warning on the next stack
        self.assertEqual([e[0] for e in events].count("out_error"), 1)
        w._apply(dict(s, out=TEST_OUT))
        self.assertIsNone(w.out_error)
        self.assertIn("out_ok", [e[0] for e in events])


class RunTest(unittest.TestCase):
    def test_scans_loaded_stack_and_stops(self):
        with tempfile.TemporaryDirectory() as out:
            w, s, events = watcher(out=out)
            fake = FakeScanner([TEXT], adf_states=[("ScannerAdfLoaded", "Idle")])
            saved = threading.Event()

            def on_event(event, message, **data):
                events.append((event, message, data))
                if event == "saved":
                    saved.set()
            w._on_event = on_event
            with mock.patch.object(core, "scanner_for", return_value=fake), \
                    mock.patch.object(core, "SETTLE_DELAY", 0), mock.patch.object(core, "POLL_INTERVAL", 0.01):
                t = threading.Thread(target=w.run)
                t.start()
                self.assertTrue(saved.wait(5))
                w.stop()
                t.join(5)
            self.assertFalse(t.is_alive())
            self.assertEqual(w.state, "stopped")
            self.assertEqual(len(list(Path(out).iterdir())), 1)
            states = [e[2]["state"] for e in events if e[0] == "state"]
            # Ready only after the first answer; after a stack right away.
            self.assertEqual(states, ["connecting", "waiting", "scanning", "waiting", "stopped"])
            scanner = [e for e in events if e[0] == "scanner"]
            self.assertEqual(len(scanner), 1)  # identified once, not again for the next stack
            self.assertEqual(scanner[0][2]["info"]["model"], "CANON DR-S150")
            self.assertNotIn("icon_path", scanner[0][2]["info"])
            self.assertEqual(scanner[0][2]["icon"], b"PNG")

    def run_one_stack(self, fake):
        w, s, events = watcher()
        done = threading.Event()

        def on_event(event, message, **data):
            events.append((event, message, data))
            if event == "conflict":
                done.set()
        w._on_event = on_event
        with mock.patch.object(core, "scanner_for", return_value=fake), \
                mock.patch.object(core, "SETTLE_DELAY", 0), mock.patch.object(core, "POLL_INTERVAL", 0.01):
            t = threading.Thread(target=w.run)
            t.start()
            self.assertTrue(done.wait(5))
            threading.Event().wait(0.1)
            w.stop()
            t.join(5)
        return [e[0] for e in events], [e[2]["state"] for e in events if e[0] == "state"]

    def test_busy_scanner_is_reported_as_possible_second_instance(self):
        fake = FakeScanner([TEXT], adf_states=[("ScannerAdfLoaded", "Idle")])
        fake.start_job = mock.Mock(side_effect=core.ScannerBusy("HTTP 503"))
        names, states = self.run_one_stack(fake)
        self.assertIn("conflict", names)
        self.assertNotIn("scan_error", names)
        self.assertEqual(states[-3:], ["busy_elsewhere", "waiting", "stopped"])

    def test_job_without_pages_is_reported_as_possible_second_instance(self):
        names, _states = self.run_one_stack(FakeScanner([], adf_states=[("ScannerAdfLoaded", "Idle")]))
        self.assertIn("conflict", names)
        self.assertNotIn("document", names)

    def test_odd_page_count_in_duplex_is_reported_but_saved(self):
        names, _states = self.run_one_stack(FakeScanner([TEXT], adf_states=[("ScannerAdfLoaded", "Idle")]))
        self.assertIn("conflict", names)
        self.assertLess(names.index("saved"), names.index("conflict"))  # nothing lost

    def test_paperless_is_retried_when_paper_arrives(self):
        w, s, events = watcher(target="paperless")
        uploaded = threading.Event()

        def on_event(event, message, **data):
            events.append((event, message, data))
            if event == "uploaded":
                uploaded.set()
        w._on_event = on_event
        fake = FakeScanner([TEXT, TEXT], adf_states=[("ScannerAdfEmpty", "Idle")] * 3 + [("ScannerAdfLoaded", "Idle")])
        # Down at start, back when the stack comes.
        connect = mock.Mock(side_effect=[None, (FakePaperless(), [])])
        with mock.patch.object(core, "scanner_for", return_value=fake), mock.patch.object(core, "connect_paperless", connect), \
                mock.patch.object(core, "SETTLE_DELAY", 0), mock.patch.object(core, "POLL_INTERVAL", 0.01):
            t = threading.Thread(target=w.run)
            t.start()
            self.assertTrue(uploaded.wait(5))
            w.stop()
            t.join(5)
        self.assertEqual(connect.call_count, 2)
        self.assertTrue(w.paperless_status["ok"])
        self.assertNotIn("saved", [e[0] for e in events])  # went to Paperless, not to the folder

    def test_pending_settings_are_applied_while_idle(self):
        w, s, events = watcher()
        with mock.patch.object(core, "scanner_for", return_value=FakeScanner([])) as scanner_cls, \
                mock.patch.object(core, "POLL_INTERVAL", 0.01):
            t = threading.Thread(target=w.run)
            t.start()
            w.update(dict(s, host="other"))
            for _ in range(200):
                if w.settings["host"] == "other":
                    break
                threading.Event().wait(0.01)
            w.stop()
            t.join(5)
        self.assertEqual(w.settings["host"], "other")
        scanner_cls.assert_any_call("other")

    def test_pause_stops_polling_the_scanner(self):
        w, s, events = watcher()
        fake = FakeScanner([])
        polls = []
        fake.adf_state = lambda: polls.append(1) or ("ScannerAdfEmpty", "Idle")

        def wait_until(check):
            for _ in range(500):
                if check():
                    return True
                threading.Event().wait(0.01)
            return False
        with mock.patch.object(core, "scanner_for", return_value=fake), mock.patch.object(core, "POLL_INTERVAL", 0.01):
            t = threading.Thread(target=w.run)
            t.start()
            self.assertTrue(wait_until(lambda: polls))
            w.pause()
            self.assertTrue(wait_until(lambda: w.state == "paused"))
            count = len(polls)
            threading.Event().wait(0.1)
            self.assertEqual(len(polls), count)
            w.pause(False)
            self.assertTrue(wait_until(lambda: w.state == "waiting" and len(polls) > count))
            w.pause()
            self.assertTrue(wait_until(lambda: w.state == "paused"))
            w.stop()  # also ends a pause
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual([e[0] for e in events if e[0] in ("paused", "resumed")], ["paused", "resumed", "paused"])

    def test_auto_pause_after_minutes_without_paper(self):
        w, s, events = watcher(auto_pause=1)
        fake = FakeScanner([])

        def wait_until(check):
            for _i in range(500):
                if check():
                    return True
                threading.Event().wait(0.01)
            return False
        with mock.patch.object(core, "scanner_for", return_value=fake), mock.patch.object(core, "POLL_INTERVAL", 0.01), \
                mock.patch.object(core, "MINUTE", 0.2):
            t = threading.Thread(target=w.run)
            t.start()
            self.assertTrue(wait_until(lambda: w.state == "waiting"))
            left, total = w.auto_pause
            self.assertEqual(total, 0.2)
            self.assertLessEqual(left, total)
            self.assertTrue(wait_until(lambda: w.state == "paused"))
            self.assertIsNone(w.auto_pause)
            w.pause(False)  # resuming starts the countdown again
            self.assertTrue(wait_until(lambda: w.state == "waiting"))
            self.assertTrue(wait_until(lambda: w.state == "paused"))
            w.stop()
            t.join(5)
        paused = [e for e in events if e[0] == "paused"]
        self.assertEqual([e[2]["auto"] for e in paused], [True, True])

    def test_without_address_nothing_is_polled_until_one_is_set(self):
        w, s, events = watcher(host="")
        fake = FakeScanner([])
        polls = []
        fake.adf_state = lambda: polls.append(1) or ("ScannerAdfEmpty", "Idle")

        def wait_until(check):
            for _i in range(500):
                if check():
                    return True
                threading.Event().wait(0.01)
            return False
        with mock.patch.object(core, "scanner_for", return_value=fake), mock.patch.object(core, "POLL_INTERVAL", 0.01):
            t = threading.Thread(target=w.run)
            t.start()
            self.assertTrue(wait_until(lambda: w.state == "no_scanner"))
            threading.Event().wait(0.1)
            self.assertEqual(polls, [])
            w.update(dict(s, host="scanner"))
            self.assertTrue(wait_until(lambda: w.state == "waiting" and polls))
            w.stop()
            t.join(5)
        self.assertFalse(t.is_alive())

    def test_no_auto_pause_while_the_scanner_is_unreachable(self):
        w, s, events = watcher(auto_pause=1)
        fake = FakeScanner([])
        reachable = threading.Event()

        def adf_state():
            if not reachable.is_set():
                raise OSError("unreachable")
            return "ScannerAdfEmpty", "Idle"
        fake.adf_state = adf_state

        def wait_until(check):
            for _i in range(500):
                if check():
                    return True
                threading.Event().wait(0.01)
            return False
        with mock.patch.object(core, "scanner_for", return_value=fake), mock.patch.object(core, "POLL_INTERVAL", 0.01), \
                mock.patch.object(core, "MINUTE", 0.2):
            t = threading.Thread(target=w.run)
            t.start()
            self.assertTrue(wait_until(lambda: w.state == "unreachable"))
            self.assertIsNone(w.auto_pause)
            threading.Event().wait(0.4)  # twice the auto pause time
            self.assertEqual(w.state, "unreachable")
            reachable.set()  # the countdown starts when the scanner answers again
            self.assertTrue(wait_until(lambda: w.state == "waiting"))
            self.assertGreater(w.auto_pause[0], 0.1)
            self.assertTrue(wait_until(lambda: w.state == "paused"))
            w.stop()
            t.join(5)

    def run_paused(self, wake_watch, manual=False, resumes=False, **settings):
        """Run until paused, give the driver's wake-up detection some polls, then stop."""
        w, s, events = watcher(auto_pause=0 if manual else 1, **settings)
        fake = FakeScanner([])
        fake.wake_watch = wake_watch

        def wait_until(check):
            for _i in range(500):
                if check():
                    return True
                threading.Event().wait(0.01)
            return False
        with mock.patch.object(core, "scanner_for", return_value=fake), \
                mock.patch.object(core, "POLL_INTERVAL", 0.01), mock.patch.object(core, "MINUTE", 0.1), \
                mock.patch.object(core, "WAKE_INTERVAL", 0.01):
            t = threading.Thread(target=w.run)
            t.start()
            self.assertTrue(wait_until(lambda: w.state == "waiting"))
            if manual:
                w.pause()
            self.assertTrue(wait_until(lambda: w.state == "paused"))
            if resumes:
                self.assertTrue(wait_until(lambda: any(e[0] == "resumed" for e in events)))
            else:
                threading.Event().wait(0.2)
            w.stop()
            t.join(5)
        return events

    def test_auto_pause_ends_when_the_scanner_wakes_up(self):
        polls = []

        class Watch:
            def __init__(self, settings):
                self.community = settings["snmp_community"]

            def poll(self):
                polls.append(self.community)
                return len(polls) >= 3
        events = self.run_paused(lambda settings, clock: Watch(settings), resumes=True, snmp_community="public")
        self.assertEqual(polls, ["public"] * 3)
        resumed = [e for e in events if e[0] == "resumed"]
        self.assertTrue(resumed[0][2]["auto"])
        self.assertIn("woke up", str(resumed[0][1]))

    def test_driver_for_the_model_once_the_scanner_answered(self):
        class ModelDriver(FakeScanner):
            NAME = "Model driver"
        generic, special = FakeScanner([]), ModelDriver([])
        w, s, events = watcher()

        def scanner_for(host, model=None):
            return special if model == "CANON DR-S150" else generic

        def wait_until(check):
            for _i in range(500):
                if check():
                    return True
                threading.Event().wait(0.01)
            return False
        with mock.patch.object(core, "scanner_for", side_effect=scanner_for), \
                mock.patch.object(core, "POLL_INTERVAL", 0.01):
            t = threading.Thread(target=w.run)
            t.start()
            self.assertTrue(wait_until(lambda: w.scanner_info is not None))
            w.stop()
            t.join(5)
        self.assertIs(w.scanner, special)
        self.assertEqual(w.scanner_info["driver"], "Model driver")
        self.assertIn("Driver: Model driver", [str(e[1]) for e in events])

    def test_no_wake_up_detection_after_a_manual_pause(self):
        watch = mock.Mock()
        self.run_paused(watch, manual=True, snmp_community="public")
        watch.assert_not_called()

    def test_wake_up_detection_not_offered_by_the_driver(self):
        events = self.run_paused(lambda settings, clock: None, snmp_community="public")
        warnings = [str(e[1]) for e in events if e[0] == "warning"]
        self.assertTrue(any("not available" in w for w in warnings))
        self.assertNotIn("warning", [e[0] for e in self.run_paused(lambda settings, clock: None)])  # no community

    def test_wake_up_detection_gives_up_when_unavailable(self):
        watch = mock.Mock()
        watch.return_value.poll.side_effect = core.WakeUnavailable("no process panel_service")
        events = self.run_paused(watch, snmp_community="public")
        self.assertEqual(watch.return_value.poll.call_count, 1)
        warnings = [str(e[1]) for e in events if e[0] == "warning"]
        self.assertTrue(any("panel_service" in w for w in warnings))
        self.assertNotIn("resumed", [e[0] for e in events])

    def test_auto_pause_off(self):
        w, s, events = watcher(auto_pause=0)
        with mock.patch.object(core, "scanner_for", return_value=FakeScanner([])), \
                mock.patch.object(core, "POLL_INTERVAL", 0.01), mock.patch.object(core, "MINUTE", 0.01):
            t = threading.Thread(target=w.run)
            t.start()
            threading.Event().wait(0.3)
            self.assertEqual(w.state, "waiting")
            self.assertIsNone(w.auto_pause)
            w.stop()
            t.join(5)


if __name__ == "__main__":
    unittest.main()
