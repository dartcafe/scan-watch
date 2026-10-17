import unittest

from scan_watch.settings import env_overrides, parse_value


class EnvOverridesTest(unittest.TestCase):
    def test_types_are_converted(self):
        env = {
            "SCAN_WATCH_HOST": "scanner.lan",
            "SCAN_WATCH_DPI": "200",
            "SCAN_WATCH_DUPLEX": "false",
            "SCAN_WATCH_BLANK_THRESHOLD": "1,5",
            "SCAN_WATCH_TARGET": "both",
        }
        self.assertEqual(env_overrides(env), {
            "host": "scanner.lan", "dpi": 200, "duplex": False, "blank_threshold": 1.5, "target": "both",
        })

    def test_empty_and_unrelated_variables_are_ignored(self):
        self.assertEqual(env_overrides({"SCAN_WATCH_HOST": "  ", "HOME": "/root"}), {})

    def test_token_is_never_taken_into_the_settings(self):
        self.assertEqual(env_overrides({"SCAN_WATCH_PAPERLESS_TOKEN": "secret"}), {})

    def test_invalid_choice_is_rejected(self):
        with self.assertRaises(ValueError):
            env_overrides({"SCAN_WATCH_DPI": "600"})
        with self.assertRaises(ValueError):
            env_overrides({"SCAN_WATCH_DUPLEX": "maybe"})

    def test_auto_pause_minutes(self):
        self.assertEqual(env_overrides({"SCAN_WATCH_AUTO_PAUSE": "0"}), {"auto_pause": 0})
        for raw in ("-5", "1.5"):
            with self.assertRaises(ValueError):
                parse_value("auto_pause", raw)

    def test_snmp_community(self):
        self.assertEqual(env_overrides({"SCAN_WATCH_SNMP_COMMUNITY": "public"}), {"snmp_community": "public"})
        self.assertEqual(parse_value("snmp_community", ""), "")
        for raw in ("pub lic", "x" * 65, "pub\x00lic"):
            with self.assertRaises(ValueError):
                parse_value("snmp_community", raw)

    def test_paperless_addresses(self):
        self.assertEqual(env_overrides({"SCAN_WATCH_PAPERLESS_URL": "http://paperless:8000",
                                        "SCAN_WATCH_PAPERLESS_PUBLIC_URL": "https://docs.lan"}),
                         {"paperless_url": "http://paperless:8000", "paperless_public_url": "https://docs.lan"})
        for key in ("paperless_url", "paperless_public_url"):
            with self.assertRaises(ValueError):
                parse_value(key, "docs.lan")

    def test_fingerprint_accepts_printed_form(self):
        self.assertEqual(parse_value("paperless_tls_pin", "AB:CD:0F"), "abcd0f")


if __name__ == "__main__":
    unittest.main()
