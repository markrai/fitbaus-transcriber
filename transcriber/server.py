from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import threading
import re

from .audiobookshelf import Audiobookshelf, SourceError, valid_bearer

from .contract import SCHEMA, MAX_WINDOW_MS


class Service(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, config):
        self.config = config
        self.source = Audiobookshelf(config.source) if config.source else None
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(5)
        return sock, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "FitBausTranscriber"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _json(self, status, data):
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _authorized(self):
        values = self.headers.get_all("Authorization") or []
        if len(values) != 1:
            return False
        supplied = values[0].encode("utf-8")
        expected = ("Bearer " + self.server.config.token).encode("utf-8")
        return hmac.compare_digest(supplied, expected)

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "alive"})
        if self.path == "/readyz":
            return self._json(503, {"status": "not_ready", "reason": "engine_not_implemented"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if self.path == "/v1/capabilities":
            return self._json(200, {
                "api_version": 1,
                "transcript_schema": SCHEMA,
                "engine_ready": False,
                "transcription": False,
                "max_window_ms": MAX_WINDOW_MS,
                "source_configured": self.server.source is not None,
                "source_id": self.server.config.source.source_id if self.server.config.source else None,
            })
        match = re.fullmatch(r"/v1/items/([A-Za-z0-9_-]{1,128})/manifest", self.path)
        if match:
            return self._manifest(match.group(1))
        return self._json(404, {"error": "not_found"})

    def _manifest(self, item_id):
        source = self.server.source
        if source is None:
            return self._json(503, {"error": "source_not_configured"})
        tokens = self.headers.get_all("X-Audiobookshelf-Token") or []
        if len(tokens) != 1 or not valid_bearer(tokens[0]):
            return self._json(401, {"error": "source_credential_required"})
        try:
            manifest = source.manifest(item_id, tokens[0])
        except SourceError as error:
            status = {"unauthorized": 401, "forbidden": 403, "not_found": 404}.get(error.code, 502)
            return self._json(status, {"error": error.code})
        return self._json(200, manifest.to_json())
