import io
import unittest
import urllib.error
from unittest import mock

from scan_watch.drivers.escl import Escl as Scanner, ScannerBusy, parse_capabilities
from scan_watch.settings import DEFAULTS

CAPS = """<?xml version='1.0' encoding='UTF-8'?><scan:ScannerCapabilities \
xmlns:pwg='http://www.pwg.org/schemas/2010/12/sm' xmlns:scan='http://schemas.hp.com/imaging/escl/2011/05/03'>\
<pwg:Version>2.9</pwg:Version><pwg:MakeAndModel>CANON DR-S150</pwg:MakeAndModel>\
<pwg:Manufacturer>Canon</pwg:Manufacturer><scan:Certifications><scan:Certification>\
<scan:Name>mopria-certified-scan</scan:Name><scan:Version>1.4</scan:Version></scan:Certification>\
</scan:Certifications><scan:SerialNumber>JU415486</scan:SerialNumber>\
<scan:AdminURI>http://192.168.50.145</scan:AdminURI>\
<scan:IconURI>http://192.168.50.145/eSCL/icon/imageFORMULA.png</scan:IconURI>\
<scan:Adf><scan:FeederCapacity>100</scan:FeederCapacity></scan:Adf></scan:ScannerCapabilities>"""


class CapabilitiesTest(unittest.TestCase):
    def test_canon_dr_s150(self):
        self.assertEqual(parse_capabilities(CAPS), {
            "model": "CANON DR-S150", "serial": "JU415486", "version": "2.9", "feeder_capacity": 100,
            "admin_url": "http://192.168.50.145", "icon_path": "/eSCL/icon/imageFORMULA.png"})

    def test_missing_and_unsafe_values(self):
        info = parse_capabilities(CAPS.replace("http://192.168.50.145</scan:AdminURI>",
                                               "javascript:alert(1)</scan:AdminURI>")
                                  .replace("<scan:FeederCapacity>100", "<scan:FeederCapacity>"))
        self.assertEqual(info["admin_url"], "")
        self.assertIsNone(info["feeder_capacity"])
        self.assertEqual(parse_capabilities("<x/>")["model"], "")


class StartJobTest(unittest.TestCase):
    def start(self, code):
        error = urllib.error.HTTPError("http://scanner/eSCL/ScanJobs", code, "x", {}, io.BytesIO())
        with mock.patch.object(Scanner, "_request", side_effect=error):
            Scanner("scanner").start_job(DEFAULTS)

    def test_busy_scanner(self):
        for code in (409, 503):
            with self.assertRaises(ScannerBusy):
                self.start(code)

    def test_other_errors_stay_errors(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.start(500)
        self.assertNotIsInstance(caught.exception, ScannerBusy)


if __name__ == "__main__":
    unittest.main()
