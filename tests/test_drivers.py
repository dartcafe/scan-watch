import unittest

from scan_watch import snmp
from scan_watch.drivers import Escl, WakeUnavailable, drivers, scanner_for
from scan_watch.drivers.canon_dr_s150 import CanonDrS150, WakeDetector, WakeWatch
from scan_watch.settings import DEFAULTS
from tests.test_snmp import FakeAgent, processes

# panel_service on a DR-S150 (firmware 1.23), centiseconds per 2 s, recorded 2026-10-15:
# awake, then switched off / standby, then paper inserted.
AWAKE = [3, 3, 2, 4, 3, 2, 4, 3, 2, 5, 2, 3, 4, 3]
STANDBY = [7, 6, 5, 6, 6, 7, 6, 5, 6, 4, 7, 5, 6, 8, 5, 6, 6, 6, 5, 7, 6, 5, 6, 6]
WOKEN = [3, 3, 3, 3]


class DriverTest(unittest.TestCase):
    def test_driver_by_model(self):
        self.assertIn(CanonDrS150, drivers())
        self.assertIsInstance(scanner_for("scanner", "CANON DR-S150"), CanonDrS150)
        self.assertIsInstance(scanner_for("scanner", "Canon DR-S150 "), CanonDrS150)
        for model in (None, "", "HP OfficeJet"):
            self.assertIs(type(scanner_for("scanner", model)), Escl)

    def test_wake_up_detection_needs_a_community(self):
        self.assertIsNone(Escl("scanner").wake_watch(dict(DEFAULTS, snmp_community="public"), None))
        canon = CanonDrS150("scanner")
        self.assertIsNone(canon.wake_watch(DEFAULTS, None))
        self.assertIsInstance(canon.wake_watch(dict(DEFAULTS, snmp_community="public"), None), WakeWatch)


class WakeDetectorTest(unittest.TestCase):
    def feed(self, samples, detector=None):
        d = detector or WakeDetector()
        return d, [n for n, cs in enumerate(samples) if d.feed(cs / 2)]

    def test_recorded_standby_and_wake_up(self):
        d, woke = self.feed(AWAKE + STANDBY * 20 + WOKEN)
        self.assertEqual((d.awake, d.asleep), (1.5, 3.0))
        self.assertEqual(woke[0], len(AWAKE) + len(STANDBY) * 20 + 1)  # second sample after waking

    def test_no_wake_up_without_standby_first(self):
        d, woke = self.feed(AWAKE * 10)
        self.assertIsNone(d.asleep)
        self.assertEqual(woke, [])

    def test_single_low_sample_in_standby_is_no_wake_up(self):
        d, woke = self.feed(AWAKE + STANDBY + [4, 6, 3, 6, 6])
        self.assertIsNotNone(d.asleep)
        self.assertEqual(woke, [])

    def test_awake_level_near_zero(self):
        d, woke = self.feed([0] * 5 + STANDBY + [0, 0])
        self.assertEqual(woke[0], 5 + len(STANDBY) + 1)


class WakeWatchTest(unittest.TestCase):
    def test_rates_from_cpu_time_and_new_pid_after_restart(self):
        agent = FakeAgent(processes(0))
        self.addCleanup(agent.close)
        now = [0.0]
        watch = WakeWatch("127.0.0.1", "public", lambda: now[0])
        watch.snmp.port = agent.port
        watch.snmp.timeout = 1
        cpu = 0
        for cs in AWAKE[:6] + STANDBY[:6]:
            cpu += cs
            now[0] += 2
            agent.values.update(processes(cpu))
            self.assertFalse(watch.poll())
        self.assertEqual(watch.detector.asleep, 3.0)
        # The scanner restarted: panel_service has another PID and starts at zero.
        agent.values = processes(10, pid=1900)
        now[0] += 2
        self.assertFalse(watch.poll())  # old PID gone
        results = []
        cpu = 10
        for cs in WOKEN:
            cpu += cs
            now[0] += 2
            agent.values.update(processes(cpu, pid=1900))
            results.append(watch.poll())
        self.assertEqual(results, [False, False, True, True])

    def test_missing_process(self):
        agent = FakeAgent({snmp.HR_SW_RUN_NAME + (1,): (0x04, b"init")})
        self.addCleanup(agent.close)
        watch = WakeWatch("127.0.0.1", "public", lambda: 0.0)
        watch.snmp.port = agent.port
        with self.assertRaises(WakeUnavailable):
            watch.poll()


if __name__ == "__main__":
    unittest.main()
