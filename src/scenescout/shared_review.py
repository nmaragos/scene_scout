"""Serve a captured review to a browser on a trusted local network."""
from base64 import b64encode
from dataclasses import replace
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from secrets import token_urlsafe
from threading import Lock, Thread
from urllib.parse import parse_qs, urlsplit

from .review_document import (
    REVIEW_DECISIONS, SHARED_REVIEW_EDIT_LIMIT_BYTES, SHARED_REVIEW_SCRIPT,
    THUMBNAIL_REVIEW_LIMIT, render_shared_review,
)


_SCRIPT_HASH = b64encode(sha256(SHARED_REVIEW_SCRIPT.encode("utf-8")).digest()).decode("ascii")
_CONTENT_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    f"script-src 'sha256-{_SCRIPT_HASH}'; connect-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def _form_value(fields, name):
    values = fields.get(name)
    if values is None or len(values) != 1:
        raise ValueError("Incomplete review form.")
    return values[0]


def _apply_edits(document, fields):
    review_updates = {}
    finding_updates = {}
    for name in fields.keys() - {"revision"}:
        value = _form_value(fields, name)
        if name == "decision":
            if value not in REVIEW_DECISIONS:
                raise ValueError("Unknown review decision.")
            review_updates[name] = value
        elif name == "notes":
            review_updates[name] = value.replace("\r\n", "\n").replace("\r", "\n")
        else:
            field, _, index_text = name.rpartition("-")
            if field not in {"notes", "reviewed", "open_issue"} or not index_text.isascii() or not index_text.isdecimal():
                raise ValueError("Unknown finding field.")
            index = int(index_text)
            if index_text != str(index) or index >= len(document.findings):
                raise ValueError("Unknown finding.")
            if field != "notes":
                if value not in ("0", "1"):
                    raise ValueError("Invalid review flag.")
                value = value == "1"
            finding_updates.setdefault(index, {})[field] = value

    if finding_updates:
        findings = list(document.findings)
        for index, updates in finding_updates.items():
            findings[index] = replace(findings[index], **updates)
        review_updates["findings"] = tuple(findings)
    return replace(document, **review_updates)


class _ReviewRequest(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_args):
        # Request paths contain the private review token.
        pass

    def _authorized(self):
        return (self.path.partition("?")[0] == self.server.review_path
                and self.headers.get("Host") == self.server.authority)

    def _respond(self, status, body="", *, html=False):
        content = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _CONTENT_POLICY)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if not self._authorized():
            self._respond(HTTPStatus.NOT_FOUND, "Review link unavailable.")
            return
        document, revision = self.server.session.snapshot()
        if self.server.session.closed:
            self._respond(HTTPStatus.GONE, "This review has stopped sharing.")
            return
        message = "Review saved and synced to SceneScout." if urlsplit(self.path).query == "saved=1" else ""
        self._respond(HTTPStatus.OK, render_shared_review(document, revision, message), html=True)

    def do_POST(self):
        if not self._authorized():
            self._respond(HTTPStatus.NOT_FOUND, "Review link unavailable.")
            return
        try:
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError("Unsupported review form.")
            size = int(self.headers.get("Content-Length", "0"))
            if size > SHARED_REVIEW_EDIT_LIMIT_BYTES:
                self._respond(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                              "This save exceeds 8 MiB. Shorten or undo some edits and try again.")
                return
            if size <= 0:
                raise ValueError("Review form is empty.")
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError("Incomplete review form.")
            fields = parse_qs(data.decode("utf-8"), keep_blank_values=True, errors="strict",
                              max_num_fields=3 + 3 * self.server.session.finding_count)
            status = self.server.session.submit(fields)
        except ValueError as exc:
            self._respond(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if status == HTTPStatus.OK:
            self._respond(status)
        elif status == HTTPStatus.CONFLICT:
            self._respond(status, "A newer review was saved. Copy your edits before reloading the review.")
        else:
            self._respond(status, "This review has stopped sharing.")


class SharedReview:
    def __init__(self, address, document):
        self._document = document
        self._revision = 0
        self._lock = Lock()
        self.closed = False
        self.finding_count = len(document.findings)
        self._finding_indices = {finding.change: index for index, finding in enumerate(document.findings)}
        self._thumbnail_bytes = sum(finding.thumbnails.size for finding in document.findings if finding.thumbnails)
        self._server = ThreadingHTTPServer((address, 0), _ReviewRequest)
        self._server.session = self
        self._server.review_path = f"/review/{token_urlsafe(32)}"
        self._server.authority = f"{address}:{self._server.server_port}"
        self.url = f"http://{self._server.authority}{self._server.review_path}"
        self._thread = Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._server.server_close()
            raise

    def snapshot(self):
        with self._lock:
            return self._document, self._revision

    def add_thumbnails(self, change, thumbnails):
        with self._lock:
            index = self._finding_indices.get(change)
            if self.closed or index is None:
                return
            finding = self._document.findings[index]
            previous_size = finding.thumbnails.size if finding.thumbnails is not None else 0
            total_size = self._thumbnail_bytes - previous_size + thumbnails.size
            if finding.thumbnails == thumbnails or total_size > THUMBNAIL_REVIEW_LIMIT:
                return
            findings = list(self._document.findings)
            findings[index] = replace(finding, thumbnails=thumbnails)
            self._document = replace(self._document, findings=tuple(findings))
            self._thumbnail_bytes = total_size
            # The revision tracks editable form fields. An image does not invalidate an open form.

    def submit(self, fields):
        with self._lock:
            if self.closed:
                return HTTPStatus.GONE
            if _form_value(fields, "revision") != str(self._revision):
                return HTTPStatus.CONFLICT
            document = _apply_edits(self._document, fields)
            if document != self._document:
                self._document = document
                self._revision += 1
            return HTTPStatus.OK

    def close(self):
        with self._lock:
            self.closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
