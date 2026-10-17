"""Paperless-ngx client with optional certificate pinning."""

import functools
import hashlib
import http.client
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote, urljoin, urlsplit

from .i18n import _


RUNNING = {"PENDING", "RECEIVED", "STARTED", "RETRY"}


def task_outcome(response):
    """Read a tasks/?task_id= response -> None while running, else (ok, document_id, message).

    Older Paperless versions answer with a plain list, status "SUCCESS", "related_document"
    and "result"; newer ones paginate and use status "success", "related_document_ids" and
    "result_data".
    """
    tasks = response.get("results") if isinstance(response, dict) else response
    if not tasks:
        return None
    task = tasks[0]
    status = str(task.get("status", "")).upper()
    if status in RUNNING:
        return None
    data = task.get("result_data") if isinstance(task.get("result_data"), dict) else {}
    ids = task.get("related_document_ids") or []
    document = task.get("related_document") or data.get("document_id") or (ids[0] if ids else None)
    message = (task.get("result") or data.get("error") or data.get("message")
               or (json.dumps(data, ensure_ascii=False) if data else None) or task.get("status_display") or status)
    return status == "SUCCESS", document, str(message)


class PaperlessError(Exception):
    def __init__(self, message, untrusted_cert=False):
        super().__init__(message)
        self.untrusted_cert = untrusted_cert


def cert_fingerprint(url):
    """SHA-256 fingerprint of the certificate a HTTPS server presents."""
    parts = urlsplit(url)
    pem = ssl.get_server_certificate((parts.hostname, parts.port or 443), timeout=10)
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def format_fingerprint(fp):
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()


class CertificateChanged(ssl.SSLError):
    pass


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Accepts exactly one certificate, identified by its SHA-256 fingerprint.

    Meant for self-signed certificates (e.g. the Synology default one), where
    normal CA and hostname validation cannot succeed.
    """

    def __init__(self, *args, pin, **kwargs):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs["context"] = context
        super().__init__(*args, **kwargs)
        self.pin = pin

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        if hashlib.sha256(der).hexdigest() != self.pin:
            self.close()
            raise CertificateChanged()


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pin):
        super().__init__()
        self.pin = pin

    def https_open(self, req):
        return self.do_open(functools.partial(PinnedHTTPSConnection, pin=self.pin), req)


class Paperless:
    def __init__(self, url, token, tls_pin="", on_tag_created=None):
        self.api = url.rstrip("/") + "/api/"
        self.token = token
        self._tag_cache = {}
        self._on_tag_created = on_tag_created
        handlers = [PinnedHTTPSHandler(tls_pin)] if tls_pin else []
        self._opener = urllib.request.build_opener(*handlers)

    def _request(self, method, path, data=None, content_type=None, timeout=30):
        headers = {"Authorization": f"Token {self.token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(urljoin(self.api, path), data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raise PaperlessError(self._explain(e)) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, CertificateChanged):
                raise PaperlessError(_("Certificate has changed – enter the Paperless URL again in the menu "
                                       "or clear SCAN_WATCH_PAPERLESS_TLS_PIN to check and pin the new "
                                       "certificate")) from None
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise PaperlessError(_("Certificate not trusted ({reason})", reason=reason.verify_message),
                                     untrusted_cert=True) from None
            raise PaperlessError(_("not reachable ({reason})", reason=reason)) from None

    @staticmethod
    def _explain(e):
        if e.code in (401, 403):
            return _("HTTP {code} – API token invalid or without permission", code=e.code)
        if e.code == 400 and b"Bad Request" in e.read()[:500]:
            return _("HTTP 400 – Paperless rejects the host name; "
                     "add the address to PAPERLESS_URL or PAPERLESS_ALLOWED_HOSTS")
        if e.code == 404:
            return _("HTTP 404 – no Paperless answers at this address (https instead of http?)")
        return f"HTTP {e.code} {e.reason}"

    def check(self):
        self._request("GET", "tags/?page_size=1")

    def tag_ids(self, names):
        """Resolve tag names to IDs, creating tags that do not exist yet."""
        ids = []
        for name in names:
            if name not in self._tag_cache:
                found = self._request("GET", f"tags/?name__iexact={quote(name)}")["results"]
                if found:
                    self._tag_cache[name] = found[0]["id"]
                else:
                    body = json.dumps({"name": name}).encode()
                    self._tag_cache[name] = self._request("POST", "tags/", body, "application/json")["id"]
                    if self._on_tag_created:
                        self._on_tag_created(name)
            ids.append(self._tag_cache[name])
        return ids

    def upload(self, pdf, filename, tag_ids):
        """Hand the PDF to the consumer; returns the task ID."""
        boundary = uuid.uuid4().hex
        parts = [('name="tags"', str(t).encode()) for t in tag_ids]
        parts.append((f'name="document"; filename="{filename}"\r\nContent-Type: application/pdf', pdf))
        body = b"".join(
            f"--{boundary}\r\nContent-Disposition: form-data; {header}\r\n\r\n".encode() + value + b"\r\n"
            for header, value in parts
        ) + f"--{boundary}--\r\n".encode()
        return self._request("POST", "documents/post_document/", body,
                             f"multipart/form-data; boundary={boundary}", timeout=120)

    def watch_task(self, task_id, on_done, timeout=1800):
        """Call on_done(ok, document_id, message) in the background once Paperless has finished the task."""
        def run():
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                time.sleep(10)
                try:
                    outcome = task_outcome(self._request("GET", f"tasks/?task_id={quote(str(task_id))}"))
                except PaperlessError:
                    continue
                if outcome is not None:
                    on_done(*outcome)
                    return
        threading.Thread(target=run, daemon=True).start()
