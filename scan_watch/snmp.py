"""A minimal SNMPv1 client (GET, GETNEXT), standard library only, plus the
Host Resources process table. Drivers use it, e.g. to watch a sleeping scanner."""

import socket

HR_SW_RUN_NAME = (1, 3, 6, 1, 2, 1, 25, 4, 2, 1, 2)       # process names, indexed by PID
HR_SW_RUN_PERF_CPU = (1, 3, 6, 1, 2, 1, 25, 5, 1, 1, 1)   # CPU time in centiseconds, by PID
TIMEOUT = 2.0


class SnmpError(OSError):
    """No usable answer: timeout, wrong community, unexpected reply."""


def _tlv(tag, value):
    n = len(value)
    length = bytes([n]) if n < 0x80 else bytes([0x80 | 2]) + n.to_bytes(2, "big")
    return bytes([tag]) + length + value


def _int(n):
    return _tlv(0x02, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _oid(oid):
    out = bytearray([40 * oid[0] + oid[1]])
    for part in oid[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.insert(0, 0x80 | (part & 0x7F))
            part >>= 7
        out += bytes(chunk)
    return _tlv(0x06, bytes(out))


def _read(data, i):
    """(tag, value, index after it) of the element at data[i]."""
    if i + 2 > len(data):
        raise SnmpError("truncated reply")
    tag, n, i = data[i], data[i + 1], i + 2
    if n & 0x80:
        k = n & 0x7F
        n, i = int.from_bytes(data[i:i + k], "big"), i + k
    if i + n > len(data):
        raise SnmpError("truncated reply")
    return tag, data[i:i + n], i + n


def _parse_oid(value):
    parts = [value[0] // 40, value[0] % 40]
    n = 0
    for b in value[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(n)
            n = 0
    return tuple(parts)


def split_host(host):
    """The address part of an eSCL host setting (without port, IPv6 brackets)."""
    if host.startswith("["):
        return host[1:host.index("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class Snmp:
    def __init__(self, host, community, timeout=TIMEOUT):
        self.host = split_host(host)
        self.community = community.encode()
        self.port = 161
        self.timeout = timeout
        self._id = 0

    def _request(self, pdu_tag, oid):
        """Send one request; returns (oid, tag, value) of the single variable bound."""
        self._id = (self._id + 1) % 0x7FFFFFFF
        varbinds = _tlv(0x30, _tlv(0x30, _oid(oid) + b"\x05\x00"))
        pdu = _tlv(pdu_tag, _int(self._id) + _int(0) + _int(0) + varbinds)
        message = _tlv(0x30, _int(0) + _tlv(0x04, self.community) + pdu)
        family, kind, proto, _canon, addr = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)[0]
        with socket.socket(family, kind, proto) as sock:
            sock.settimeout(self.timeout)
            sock.connect(addr)
            sock.send(message)
            while True:
                try:
                    data = sock.recv(65535)
                except socket.timeout:
                    raise SnmpError("no answer (SNMP v1 off or wrong community?)") from None
                try:
                    return self._parse(data)
                except _OtherRequest:
                    continue  # a late answer to an earlier request

    def _parse(self, data):
        _tag, message, _end = _read(data, 0)
        i = 0
        for _field in range(2):  # version, community
            _tag, _value, i = _read(message, i)
        _tag, pdu, _end = _read(message, i)
        _tag, request_id, j = _read(pdu, 0)
        if int.from_bytes(request_id, "big") != self._id:
            raise _OtherRequest
        _tag, error, j = _read(pdu, j)
        _tag, _index, j = _read(pdu, j)
        if int.from_bytes(error, "big"):
            return None
        _tag, varbinds, _end = _read(pdu, j)
        _tag, varbind, _end = _read(varbinds, 0)
        _tag, oid, k = _read(varbind, 0)
        tag, value, _end = _read(varbind, k)
        return _parse_oid(oid), tag, value

    def get(self, oid):
        """(tag, value) of oid, or None if it does not exist."""
        res = self._request(0xA0, oid)
        if res is None or res[0] != tuple(oid) or res[1] in (0x80, 0x81, 0x82):
            return None
        return res[1:]

    def walk(self, prefix):
        """Yield (oid, tag, value) below prefix."""
        oid = tuple(prefix)
        while True:
            res = self._request(0xA1, oid)
            if res is None or res[0][:len(prefix)] != tuple(prefix) or res[1] in (0x80, 0x81, 0x82):
                return
            oid = res[0]
            yield res

    def find_process(self, name):
        """PID of the first process called name, or None."""
        for oid, _tag, value in self.walk(HR_SW_RUN_NAME):
            if value == name.encode():
                return oid[-1]
        return None

    def cpu_time(self, pid):
        """CPU time of the process in centiseconds, or None if it is gone."""
        res = self.get(HR_SW_RUN_PERF_CPU + (pid,))
        return int.from_bytes(res[1], "big") if res and res[0] == 0x02 else None


class _OtherRequest(Exception):
    pass
