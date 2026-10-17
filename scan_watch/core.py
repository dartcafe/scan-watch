"""The scan loop: wait for paper, scan the stack, deliver the PDFs.

Nothing in here prints. Everything worth showing is passed to an
on_event(event, message, **data) callback, so a terminal, a log or a web
page can present it as it likes.
"""

import io
import os
import threading
import time
import urllib.error
from datetime import datetime
from pathlib import Path

import img2pdf
from PIL import Image

from .blank import content_score
from .drivers import ScannerBusy, WakeUnavailable, scanner_for
from .i18n import Text, _
from .paperless import Paperless, PaperlessError, cert_fingerprint
from .settings import paperless_token, tag_names, uses_folder, uses_paperless

POLL_INTERVAL = 1.0  # seconds between status checks
SETTLE_DELAY = 2.0   # wait after paper is detected so the stack can be aligned
MINUTE = 60.0        # unit of the auto_pause setting, shorter in tests
WAKE_INTERVAL = 2.0  # seconds between SNMP samples while auto-paused


class Stopped(Exception):
    """Raised inside the loop once Watcher.stop() was called."""


def connect_paperless(s, emit, trust_cert=None):
    """Return a ready Paperless client with resolved tag IDs, or None.

    For an untrusted (e.g. self-signed) certificate trust_cert(settings,
    fingerprint) may decide to pin it; without that callback the fingerprint
    is only reported, so it can be pinned via SCAN_WATCH_PAPERLESS_TLS_PIN.
    """
    if not s["paperless_url"] or not paperless_token(s):
        emit("paperless_error", _("Paperless: URL or API token missing."))
        return None
    client = Paperless(s["paperless_url"], paperless_token(s), s["paperless_tls_pin"],
                       on_tag_created=lambda name: emit("info", _("Paperless: tag “{name}” created.", name=name)))
    try:
        client.check()
        tags = client.tag_ids(tag_names(s))
    except PaperlessError as e:
        emit("paperless_error", _("Paperless: {error}", error=e))
        if not e.untrusted_cert or s["paperless_tls_pin"]:
            return None
        try:
            fp = cert_fingerprint(s["paperless_url"])
        except (OSError, ValueError) as e:
            emit("paperless_error", _("Could not read the certificate ({error}).", error=e))
            return None
        if trust_cert is None:
            emit("untrusted_cert", _("To trust this certificate, check the fingerprint and set "
                                     "SCAN_WATCH_PAPERLESS_TLS_PIN={fingerprint}.", fingerprint=fp), fingerprint=fp)
            return None
        if not trust_cert(s, fp):
            return None
        s["paperless_tls_pin"] = fp
        return connect_paperless(s, emit, trust_cert)
    emit("info", _("Paperless: connection to {url} ok.", url=s["paperless_url"]))
    return client, tags


def build_pdf(pages, s):
    layout = img2pdf.get_fixed_dpi_layout_fun((s["dpi"], s["dpi"]))
    return img2pdf.convert(pages, layout_fun=layout)


def thumbnail(jpeg, size=(240, 340)):
    """Small JPEG preview of a page."""
    img = Image.open(io.BytesIO(jpeg))
    img.thumbnail(size)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=75)
    return buf.getvalue()


def check_out_dir(s):
    """None if PDFs can be written to the output folder, else the error."""
    out = Path(s["out"]).expanduser()
    probe = out / f".scan-watch-{os.getpid()}.probe"
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        return e
    return None


def write_pdf(pdf, name, s):
    out_dir = Path(s["out"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    tmp = target.with_suffix(".pdf.part")
    tmp.write_bytes(pdf)
    tmp.rename(target)
    return target


class Watcher:
    """Waits for paper in the feeder and turns every stack into PDFs.

    run() blocks until stop() is called (from another thread or a signal
    handler); a stack being scanned is finished first. Settings passed to
    update() take effect as soon as the scanner is idle. pause() stops polling
    the scanner after the current stack, so it can go to standby; that also
    happens by itself after auto_pause minutes of waiting for paper. If the
    scanner's driver can tell that it woke up (see drivers/), an automatic pause
    also ends by itself then.

    state is one of: starting, connecting (until the scanner first answers),
    waiting, scanning, feeder_blocked, busy_elsewhere (another program scans
    the stack), unreachable, paused, no_scanner (no address set yet), stopped.
    """

    def __init__(self, settings, on_event, trust_cert=None):
        self.settings = dict(settings)
        self.state = "starting"
        self.scanner = None
        self.scanner_info = None
        self._reachable = False  # whether the last poll got an answer
        self.out_error = None  # why the output folder cannot be written, if so
        self.paperless = None  # (client, tag ids) while connected
        # Last connection check while Paperless is a target: {"ok", "message", "time"}
        self.paperless_status = None
        self._paperless_lock = threading.Lock()
        self._on_event = on_event
        self._trust_cert = trust_cert
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._auto_paused = False
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._pending = None
        self._idle_since = time.monotonic()

    def emit(self, event, message="", **data):
        self._on_event(event, message, **data)

    def update(self, settings):
        with self._lock:
            self._pending = dict(settings)

    def stop(self):
        self._stop.set()
        self._wake.set()

    @property
    def paused(self):
        return self._paused.is_set()

    def pause(self, paused=True, auto=False):
        if paused == self.paused:
            return
        if paused:
            self._auto_paused = auto
            self._paused.set()
            if auto:
                message = _("No paper for {minutes} min – paused automatically, the scanner can go to standby.",
                            minutes=self.settings["auto_pause"])
            elif self.state == "scanning":
                message = _("Pausing after the current stack.")
            else:
                message = _("Paused – the scanner is no longer polled and can go to standby.")
            self.emit("paused", message, auto=auto)
        else:
            self._paused.clear()
            self._idle_since = time.monotonic()
            self.emit("resumed", _("The scanner woke up (paper inserted?) – resumed.") if auto else _("Resumed."),
                      auto=auto)
        self._wake.set()

    @property
    def auto_pause(self):
        """(seconds left, total seconds) until the automatic pause, or None.

        Relative, not a clock time: the browser's clock may differ from ours.
        """
        minutes = self.settings["auto_pause"]
        if not minutes or self.paused or self.state != "waiting":
            return None
        total = minutes * MINUTE
        return max(0.0, self._idle_since + total - time.monotonic()), total

    def run(self):
        try:
            self._apply(self.settings)
            self._loop()
        except Stopped:
            pass
        finally:
            self._set_state("stopped")

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            self.emit("state", state=state)

    def _sleep(self, seconds):
        if self._stop.wait(seconds):
            raise Stopped

    def _pause_point(self):
        """Block without touching the scanner while paused; True if there was a pause."""
        if not self.paused:
            return False
        self._set_state("paused")
        s = self.settings
        # Only an automatic pause: whoever pauses by hand wants scan-watch to rest.
        watch = self.scanner.wake_watch(s, time.monotonic) if self._auto_paused and s["host"] else None
        if self._auto_paused and s["snmp_community"] and watch is None:
            self.emit("warning", _("Wake-up detection is not available for this scanner (driver {driver}).",
                                   driver=self.scanner.NAME))
        warned = False
        while self.paused:
            self._wake.wait(WAKE_INTERVAL if watch else 3600)
            self._wake.clear()
            if self._stop.is_set():
                raise Stopped
            if watch is None or not self.paused:
                continue
            try:
                if watch.poll():
                    self.pause(False, auto=True)
            except WakeUnavailable as e:
                self.emit("warning", _("Wake-up detection not possible ({reason}) – resume by hand.",
                                       reason=e.args[0]))
                watch = None
            except OSError as e:
                if not warned:
                    self.emit("warning", _("Wake-up detection via SNMP not available ({error}) – still trying, "
                                           "or resume by hand.", error=e))
                    warned = True
        return True

    def _apply(self, s):
        if s["host"] != self.settings["host"] or self.scanner is None:
            self.scanner_info = None
            self._reachable = False
            # The generic driver until the scanner tells its model (_identify).
            self.scanner = scanner_for(s["host"])
        self.settings = s
        self._idle_since = time.monotonic()
        self._check_out()
        self.check_paperless(self._trust_cert)
        if uses_paperless(s) and self.paperless is None:
            self.emit("warning", _("Scans are saved locally in {folder} until Paperless is reachable again.",
                                   folder=s["out"]))
        if not s["host"]:
            self.emit("ready", _("No scanner address set – enter it on the configuration page of the web interface."))
            return
        where = {"folder": s["out"], "paperless": "Paperless", "both": f'{s["out"]} + Paperless'}[s["target"]]
        # "Ready" follows once the scanner has answered.
        self.emit("ready", _("Scanner {host}, target: {target}", host=s["host"], target=where))

    def check_paperless(self, trust_cert=None):
        """Connect to Paperless if it is a target; returns and keeps the outcome in
        paperless_status. trust_cert may ask about an untrusted certificate, so it
        is only passed at start (terminal), not for retries or the web page's test."""
        s = self.settings
        if not uses_paperless(s):
            self.paperless = self.paperless_status = None
            return None
        with self._paperless_lock:
            problems = []

            def emit(event, message="", **data):
                if event in ("paperless_error", "untrusted_cert"):
                    # Only the cause: the page already says "Paperless: not reachable".
                    cause = message.args.get("error") if isinstance(message, Text) else None
                    problems.append(cause or message)
                self.emit(event, message, **data)
            self.paperless = connect_paperless(s, emit, trust_cert)
            self.paperless_status = {"ok": self.paperless is not None, "time": time.time(),
                                     "message": None if self.paperless or not problems else problems[0]}
        self.emit("paperless_status")
        return self.paperless_status

    def _check_out(self):
        """Check the output folder (target, or fallback when Paperless fails); report only changes."""
        s = self.settings
        error = check_out_dir(s)
        old, self.out_error = self.out_error, (
            _("Output folder {folder} is not writable ({error}) – check its owner and permissions.",
              folder=s["out"], error=error) if error else None)
        if self.out_error and str(self.out_error) != str(old):
            self.emit("out_error", self.out_error)
        elif old and not self.out_error:
            self.emit("out_ok", _("Output folder {folder} is writable again.", folder=s["out"]))

    def _apply_pending(self):
        with self._lock:
            s, self._pending = self._pending, None
        if s is not None:
            self.emit("info", _("New settings applied."))
            self._apply(s)

    def _loop(self):
        # If paper is already in the feeder at start, scan it right away.
        while True:
            self._wait_for("ScannerAdfLoaded")
            self._set_state("scanning")
            self.emit("paper", _("Paper detected – scan starts in {seconds:.0f} s …", seconds=SETTLE_DELAY))
            self._check_out()  # rights may have changed since the start
            if uses_paperless(self.settings) and self.paperless is None:
                self.check_paperless()  # back by now? Otherwise the stack goes to the folder
            self._sleep(SETTLE_DELAY)
            s = self.settings
            try:
                docs, dropped = self.scan_stack(s)
            except ScannerBusy:
                self._other_scanner_user(_("The scanner refused the scan job because it is busy with another one."))
                continue
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                # Paper left in the feeder (jam, double feed) must not trigger
                # an endless rescan loop, so wait until the feeder is cleared.
                self.emit("scan_error", _("Scan failed: {error}", error=e))
                self.emit("info", _("Please empty the feeder, then it continues."))
                self._wait_for("ScannerAdfEmpty")
                continue
            if not docs and not dropped:
                # Paper was there, but the job delivered nothing: someone else got the pages.
                self._other_scanner_user(_("The scanner delivered no page although paper was in the feeder."))
                continue
            if docs:
                self.deliver_all(docs, dropped, s)
            else:
                self.emit("info", _("No pages with content – nothing saved."))
            # Duplex always yields front and back (blank backs are only dropped afterwards):
            # an odd count means pages went elsewhere. What arrived is saved anyway.
            if s["duplex"] and (sum(len(d) for d in docs) + dropped) % 2:
                self._report_conflict(_("The scanner delivered an odd number of pages in duplex mode, so pages "
                                        "are missing."))
            self.emit("ready", _("Ready for the next stack."))

    def _report_conflict(self, what):
        self.emit("conflict", _("{what} Is a second scan-watch instance or another scan program using this "
                                "scanner?", what=what))

    def _other_scanner_user(self, what):
        """Another program (likely a second scan-watch) scans the same stack: report it,
        do not scan after it, and continue once the feeder is empty."""
        self._report_conflict(what)
        self._wait_for("ScannerAdfEmpty", busy_state="busy_elsewhere")

    def _wait_for(self, wanted, busy_state="feeder_blocked"):
        """Poll until the idle scanner reports the wanted feeder state.

        AdfState is only trusted while the scanner is Idle, because it is not
        updated while a job is still being processed. Network hiccups are tolerated.
        """
        waiting = "waiting" if wanted == "ScannerAdfLoaded" else busy_state
        self._idle_since = time.monotonic()
        # "Ready" only once the scanner has answered; at start that can take a timeout.
        self._set_state(waiting if self._reachable else "connecting")
        warned = False
        busy_since = None
        while True:
            self._apply_pending()
            if not self.settings["host"]:
                # Nothing to poll until an address is entered in the web interface.
                self._set_state("no_scanner")
                self._sleep(POLL_INTERVAL)
                continue
            if not self._reachable and self.state not in ("connecting", "unreachable"):
                # At start, with a new address or after a pause: not ready until it answers.
                self._set_state("connecting")
            minutes = self.settings["auto_pause"]
            # Only while waiting for paper: not while paper is stuck in the feeder (that
            # needs someone anyway) and not while the scanner is unreachable (nothing
            # that could go to standby).
            if (self.state == "waiting" and minutes
                    and time.monotonic() - self._idle_since >= minutes * MINUTE):
                self.pause(auto=True)
            if self._pause_point():
                self._reachable = False  # it may have gone to standby
                self._set_state("connecting")
                warned = False
                busy_since = None
            try:
                adf, state = self.scanner.adf_state()
                if self.state != waiting:
                    if warned:
                        self.emit("info", _("Scanner reachable again."))
                    elif self.state == "connecting" and waiting == "waiting":
                        self.emit("info", _("Ready. Insert paper and it is scanned automatically."))
                    self._idle_since = time.monotonic()
                    self._set_state(waiting)
                    warned = False
                self._reachable = True
                if self.scanner_info is None:
                    self._identify()
                if state == "Idle":
                    busy_since = None
                    if adf == wanted:
                        return
                elif busy_since is None:
                    busy_since = time.monotonic()
                elif time.monotonic() - busy_since > 30:
                    self.emit("warning", _("Scanner has reported “{state}” for 30 s – is it being used elsewhere?",
                                           state=state))
                    busy_since = time.monotonic() + 270  # remind again after 5 minutes
            except (urllib.error.URLError, OSError) as e:
                self._reachable = False
                if not warned:
                    self.emit("warning", _("Scanner not reachable ({error}) – still trying …", error=e))
                    self._set_state("unreachable")
                    warned = True
            self._sleep(POLL_INTERVAL)

    def _identify(self):
        """Read model, serial number and picture once the scanner answers."""
        try:
            info = self.scanner.capabilities()
        except (urllib.error.URLError, OSError, ValueError):
            info = {}  # not every scanner offers it; don't ask again
        icon = None
        if info.get("icon_path"):
            try:
                icon = self.scanner.icon(info["icon_path"])
            except (urllib.error.URLError, OSError):
                pass
        info.pop("icon_path", None)
        scanner = scanner_for(self.settings["host"], info.get("model"))
        if type(scanner) is not type(self.scanner):
            self.scanner = scanner
            self.emit("info", _("Driver: {name}", name=scanner.NAME))
        info["driver"] = self.scanner.NAME
        self.scanner_info = info
        if info.get("model"):
            message = (_("Scanner detected: {model} (serial number {serial})", model=info["model"], serial=info["serial"])
                       if info.get("serial") else _("Scanner detected: {model}", model=info["model"]))
            self.emit("scanner", message, info=info,
                      icon=icon[0] if icon else None, icon_type=icon[1] if icon else None)

    def scan_stack(self, s):
        """Scan the loaded stack; returns ([pages of each document], dropped pages)."""
        job = self.scanner.start_job(s)
        docs, dropped = [], 0
        last_sheet = None
        try:
            for n, jpeg in enumerate(self.scanner.pages(job), 1):
                label = _("Page {n}", n=n)
                sheet = n
                if s["duplex"]:
                    label = _("Page {n} (front)", n=n) if n % 2 else _("Page {n} (back)", n=n)
                    sheet = (n + 1) // 2
                if s["skip_blank"]:
                    ratio = content_score(jpeg, s["dpi"])
                    if ratio < s["blank_threshold"]:
                        dropped += 1
                        self.emit("page", _("{page}: blank ({ratio:.2f} %) – dropped", page=label, ratio=ratio),
                                  page=n, score=ratio, dropped=True)
                        continue
                    self.emit("page", _("{page}: {ratio:.2f} % content", page=label, ratio=ratio),
                              page=n, score=ratio, dropped=False)
                else:
                    self.emit("page", label, page=n, score=None, dropped=False)
                # A sheet without content starts no document, so blank sheets vanish.
                if not docs or (s["split"] == "sheet" and sheet != last_sheet):
                    docs.append([])
                last_sheet = sheet
                docs[-1].append(jpeg)
        finally:
            self.scanner.cancel(job)
        return docs, dropped

    def deliver_all(self, docs, dropped, s):
        stamp = f'{s["prefix"]}_{datetime.now():%Y-%m-%d_%H-%M-%S}'
        for i, pages in enumerate(docs, 1):
            # Documents of one stack share the timestamp, so number them.
            name = f"{stamp}_{i:03d}.pdf" if len(docs) > 1 else f"{stamp}.pdf"
            info = _("{n} pages", n=len(pages)) if len(pages) != 1 else _("1 page")
            if dropped and len(docs) == 1:
                info = _("{pages}, {dropped} blank dropped", pages=info, dropped=dropped)
            self.deliver(pages, name, info, s)
        if len(docs) > 1:
            self.emit("info", _("{n} documents, {dropped} blank pages dropped", n=len(docs), dropped=dropped)
                      if dropped else _("{n} documents", n=len(docs)))

    def deliver(self, pages, name, info, s):
        """Save and/or upload one document. Never loses it: what cannot be stored
        anywhere is handed on with the "unsaved" event (the web page offers it)."""
        self.emit("document", name=name, pages=len(pages), thumb=thumbnail(pages[0]))
        pdf = build_pdf(pages, s)
        saved = self._save(pdf, name, info, s) if uses_folder(s) else None
        uploaded = False
        if uses_paperless(s):
            try:
                if self.paperless is None:
                    raise PaperlessError(_("not reachable when the stack was inserted"))
                client, tags = self.paperless
                task = client.upload(pdf, name, tags)
                uploaded = True
                self.emit("uploaded", _("Handed to Paperless: {name} ({info})", name=name, info=info), name=name,
                          pages=len(pages))
                client.watch_task(task, lambda *outcome: self._report_task(name, *outcome))
            except PaperlessError as e:
                self.emit("upload_error", _("Paperless upload failed: {error}", error=e), name=name)
                if not uses_folder(s):
                    # Never lose a scan: fall back to the local folder.
                    saved = self._save(pdf, name, info, s, fallback=True)
        if saved is None and not uploaded:
            self.emit("unsaved", _("{name} could not be stored anywhere – download it in the web interface "
                                   "before scan-watch is restarted.", name=name), name=name, pdf=pdf)

    def _save(self, pdf, name, info, s, fallback=False):
        """Write the PDF into the output folder; returns its path, or None after reporting the error."""
        try:
            path = write_pdf(pdf, name, s)
        except OSError as e:
            self.emit("save_error", _("Could not save {name} in {folder}: {error}", name=name, folder=s["out"],
                                      error=e), name=name)
            return None
        message = (_("Saved locally instead: {path}", path=path) if fallback
                   else _("Saved: {path} ({info})", path=path, info=info))
        self.emit("saved", message, name=name, path=str(path))
        return path

    def _report_task(self, name, ok, doc, message):
        if ok:
            message = (_("Paperless: {name} processed (document #{document})", name=name, document=doc) if doc
                       else _("Paperless: {name} processed", name=name))
            self.emit("paperless_done", message, name=name, document=doc)
        else:
            self.emit("paperless_failed", _("Paperless: {name} FAILED – {result}", name=name, result=message),
                      name=name, result=message)
