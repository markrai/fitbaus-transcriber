from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import threading

from .contract import SCHEMA, MAX_WINDOW_MS


class Service(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, config):
        self.config = config
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
            })
        return self._json(404, {"error": "not_found"})
