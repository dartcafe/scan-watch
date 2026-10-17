import socket
import threading
import unittest

from scan_watch import snmp
from scan_watch.snmp import Snmp, SnmpError, split_host

class FakeAgent:
    """UDP SNMPv1 agent answering from a dict {oid: (tag, value)}."""

    def __init__(self, values, community=b"public"):
        self.values = dict(values)
        self.community = community
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                return
            _tag, message, _end = snmp._read(data, 0)
            _tag, _version, i = snmp._read(message, 0)
            _tag, community, i = snmp._read(message, i)
            if community != self.community:
                continue  # like a real agent: no answer
            pdu_tag, pdu, _end = snmp._read(message, i)
            _tag, request_id, j = snmp._read(pdu, 0)
            _tag, _e, j = snmp._read(pdu, j)
            _tag, _e, j = snmp._read(pdu, j)
            _tag, varbinds, _end = snmp._read(pdu, j)
            _tag, varbind, _end = snmp._read(varbinds, 0)
            _tag, oid, _end = snmp._read(varbind, 0)
            oid = snmp._parse_oid(oid)
            if pdu_tag == 0xA1:  # GETNEXT
                oid = next((o for o in sorted(self.values) if o > oid), None)
            found = self.values.get(oid)
            error = 0 if found else 2  # noSuchName
            bound = snmp._oid(oid or (1, 3)) + (snmp._tlv(*found) if found else b"\x05\x00")
            reply = snmp._tlv(0xA2, snmp._tlv(0x02, request_id) + snmp._int(error) + snmp._int(0)
                              + snmp._tlv(0x30, snmp._tlv(0x30, bound)))
            self.sock.sendto(snmp._tlv(0x30, snmp._int(0) + snmp._tlv(0x04, community) + reply), addr)

    def close(self):
        self.sock.close()


def processes(cpu, pid=1816):
    return {snmp.HR_SW_RUN_NAME + (1,): (0x04, b"init"),
            snmp.HR_SW_RUN_NAME + (pid,): (0x04, b"panel_service"),
            snmp.HR_SW_RUN_PERF_CPU + (1,): (0x02, snmp._int(5)[2:]),
            snmp.HR_SW_RUN_PERF_CPU + (pid,): (0x02, snmp._int(cpu)[2:])}



class SnmpTest(unittest.TestCase):
    def setUp(self):
        self.agent = FakeAgent(processes(70000))
        self.addCleanup(self.agent.close)
        self.client = Snmp(f"127.0.0.1:{self.agent.port}", "public", timeout=1)
        self.client.port = self.agent.port

    def test_finds_the_process_and_reads_its_cpu_time(self):
        pid = self.client.find_process("panel_service")
        self.assertEqual(pid, 1816)
        self.assertEqual(self.client.cpu_time(pid), 70000)
        self.assertIsNone(self.client.cpu_time(4242))
        self.assertIsNone(self.client.find_process("nothing"))

    def test_wrong_community_times_out(self):
        client = Snmp("127.0.0.1", "secret", timeout=0.2)
        client.port = self.agent.port
        with self.assertRaises(SnmpError):
            client.cpu_time(1816)

    def test_host_without_port_and_brackets(self):
        self.assertEqual(split_host("192.168.50.145"), "192.168.50.145")
        self.assertEqual(split_host("scanner.lan:8080"), "scanner.lan")
        self.assertEqual(split_host("[fe80::1]:80"), "fe80::1")
        self.assertEqual(split_host("[fe80::1]"), "fe80::1")


if __name__ == "__main__":
    unittest.main()
