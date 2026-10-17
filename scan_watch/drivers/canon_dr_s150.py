"""Canon DR-S150 (tested with firmware 1.23).

Wake-up detection: any eSCL request wakes the scanner from standby, but
inserting paper wakes it too, and its SNMP agent answers Host Resources queries
without waking it. In standby the process panel_service uses about twice as
much CPU time as while awake, so a drop back to the awake level means the
scanner woke up, usually because paper was inserted. Needs SNMP v1 switched on
in the scanner's web interface and its community in the snmp_community setting.

Otherwise the generic eSCL driver fits; it also deletes every finished job,
without which this scanner stays in "Processing" with a frozen AdfState.
"""

import statistics

from ..i18n import _
from ..snmp import Snmp
from .escl import Escl, WakeUnavailable

PROCESS = "panel_service"


class CanonDrS150(Escl):
    NAME = "Canon DR-S150"
    MODELS = ("CANON DR-S150",)

    def wake_watch(self, settings, clock):
        if not settings["snmp_community"]:
            return None
        return WakeWatch(self.host, settings["snmp_community"], clock)


class WakeDetector:
    """Decides from CPU rates (centiseconds per second) of panel_service when the
    scanner woke up again. Calibrates itself, as the levels may differ elsewhere:
    the pause starts with the scanner awake (it was polled until then), a lasting
    rise means standby, a drop well below the standby level means it woke up.
    """

    CALIBRATE = 5   # samples of the awake level at the start of the pause
    SETTLE = 5      # samples in a row above it that count as standby
    CONFIRM = 2     # samples in a row below the standby level that count as woken up

    def __init__(self):
        self.awake = None
        self.asleep = None   # standby level once it was seen
        self._samples = []
        self._low = 0

    def feed(self, rate):
        """Add one rate; True once the scanner woke up from standby."""
        if self.awake is None:
            self._samples.append(rate)
            if len(self._samples) == self.CALIBRATE:
                self.awake = statistics.median(self._samples)
                self._samples = []
            return False
        if self.asleep is None:
            self._samples = (self._samples + [rate])[-self.SETTLE:]
            if len(self._samples) == self.SETTLE and min(self._samples) > self.awake * 1.3 + 0.3:
                self.asleep = statistics.median(self._samples)
            return False
        self._low = self._low + 1 if rate < self.asleep * 0.75 else 0
        return self._low >= self.CONFIRM


class WakeWatch:
    """Samples panel_service on the scanner and feeds a WakeDetector."""

    def __init__(self, host, community, clock):
        self.snmp = Snmp(host, community)
        self.detector = WakeDetector()
        self._clock = clock
        self._pid = None
        self._last = None  # (time, cpu time)

    def poll(self):
        """True once the scanner woke up. Raises OSError (incl. SnmpError) if SNMP
        does not answer, WakeUnavailable if the process does not exist there."""
        if self._pid is None:
            self._pid = self.snmp.find_process(PROCESS)
            if self._pid is None:
                raise WakeUnavailable(_("no process {name} on the scanner – other firmware?", name=PROCESS))
        cpu = self.snmp.cpu_time(self._pid)
        now = self._clock()
        if cpu is None:  # restarted with another PID: look it up again
            self._pid = self._last = None
            return False
        last, self._last = self._last, (now, cpu)
        if last is None or now <= last[0] or cpu < last[1]:
            return False
        return self.detector.feed((cpu - last[1]) / (now - last[0]))
