"""The generic driver: a minimal eSCL (AirScan / Mopria) client for scanners
with a document feeder. Drivers for particular scanners derive from Escl."""

import html
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

from ..i18n import _
from ..settings import COLOR_MODES, PAPER_SIZES


ICON_TYPES = ("image/png", "image/jpeg", "image/gif")
MAX_ICON = 512 * 1024


def _text(xml, tag):
    """Text of the first <prefix:tag> element, or ""."""
    m = re.search(rf"<(?:[\w-]+:)?{tag}>([^<]*)</(?:[\w-]+:)?{tag}>", xml)
    return html.unescape(m.group(1)).strip() if m else ""


def parse_capabilities(xml):
    capacity = _text(xml, "FeederCapacity")
    admin = _text(xml, "AdminURI")
    return {
        "model": _text(xml, "MakeAndModel"),
        "serial": _text(xml, "SerialNumber"),
        "version": _text(xml, "Version"),  # the first one is the eSCL version
        "feeder_capacity": int(capacity) if capacity.isdigit() else None,
        # Only plain web links, it ends up as href in the browser.
        "admin_url": admin if urlsplit(admin).scheme in ("http", "https") else "",
        "icon_path": urlsplit(_text(xml, "IconURI")).path,
    }


class ScannerBusy(RuntimeError):
    """The scanner refused a new job because it is busy with another one."""


class WakeUnavailable(Exception):
    """The wake-up detection cannot work with this scanner; args[0] says why (a Text)."""


class Escl:
    """Any eSCL scanner. A driver for a particular scanner derives from this class,
    names the models it is for and overrides only what that scanner does differently.
    """

    NAME = "eSCL"
    # Models as the scanner reports them in MakeAndModel (ScannerCapabilities),
    # compared without case. The generic driver is used for every other model.
    MODELS = ()

    @classmethod
    def matches(cls, model):
        return model.strip().upper() in (m.upper() for m in cls.MODELS)

    def __init__(self, host):
        self.host = host
        self.base = f"http://{host}/eSCL/"

    def _request(self, method, path, data=None, timeout=30):
        url = urljoin(self.base, path)
        headers = {"Content-Type": "text/xml"} if data else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        return urllib.request.urlopen(req, timeout=timeout)

    def adf_state(self):
        with self._request("GET", "ScannerStatus", timeout=5) as resp:
            xml = resp.read().decode("utf-8", "replace")
        m = re.search(r"<scan:AdfState>([^<]+)</scan:AdfState>", xml)
        state = re.search(r"<pwg:State>([^<]+)</pwg:State>", xml)
        return (m.group(1) if m else "Unknown", state.group(1) if state else "Unknown")

    def capabilities(self):
        """Model, serial number etc. from ScannerCapabilities."""
        with self._request("GET", "ScannerCapabilities", timeout=10) as resp:
            return parse_capabilities(resp.read(1_000_000).decode("utf-8", "replace"))

    def icon(self, path):
        """(bytes, type) of the scanner's picture, or None.

        Fetched by path from the configured host, whatever host the scanner names.
        """
        if not path.startswith("/"):
            return None
        with self._request("GET", path, timeout=10) as resp:
            kind = resp.headers.get_content_type()
            data = resp.read(MAX_ICON + 1)
        return (data, kind) if kind in ICON_TYPES and len(data) <= MAX_ICON else None

    def start_job(self, s):
        width, height = PAPER_SIZES[s["paper"]][1:]
        settings = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.0</pwg:Version>
  <scan:Intent>TextAndGraphic</scan:Intent>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>{width}</pwg:Width>
      <pwg:Height>{height}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>Feeder</pwg:InputSource>
  <scan:Duplex>{"true" if s["duplex"] else "false"}</scan:Duplex>
  <scan:ColorMode>{COLOR_MODES[s["color"]][1]}</scan:ColorMode>
  <scan:XResolution>{s["dpi"]}</scan:XResolution>
  <scan:YResolution>{s["dpi"]}</scan:YResolution>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
</scan:ScanSettings>
"""
        try:
            with self._request("POST", "ScanJobs", data=settings.encode()) as resp:
                location = resp.headers.get("Location")
        except urllib.error.HTTPError as e:
            if e.code in (409, 503):
                raise ScannerBusy(f"HTTP {e.code}") from None
            raise
        if not location:
            raise RuntimeError(_("Scanner returned no job URL"))
        return urljoin(self.base, location.rstrip("/") + "/")

    def pages(self, job_url):
        """Yield JPEG bytes for every page of the job until the feeder is empty."""
        retries = 0
        while True:
            try:
                with self._request("GET", urljoin(job_url, "NextDocument"), timeout=120) as resp:
                    yield resp.read()
                retries = 0
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return  # no more pages
                if e.code == 503 and retries < 30:
                    retries += 1
                    time.sleep(1)
                    continue
                raise

    def cancel(self, job_url):
        """Delete the job; called after every scan, also a finished one. Some scanners
        (the DR-S150) stay in "Processing" with a frozen AdfState until then."""
        try:
            self._request("DELETE", job_url, timeout=5).close()
        except (urllib.error.URLError, OSError):
            pass

    def wake_watch(self, settings, clock):
        """Something whose poll() says True once the scanner woke up from standby
        without waking it, or None if there is no such way (the default).

        Asked when an automatic pause starts; poll() is then called every
        WAKE_INTERVAL seconds. It may raise OSError (tried again, reported once)
        or WakeUnavailable (given up for this pause).
        """
        return None
