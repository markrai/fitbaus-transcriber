from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import threading
import re
import sqlite3

from .pairing import Pairing, TTL_SECONDS

from .audiobookshelf import Audiobookshelf, SourceError, valid_bearer

from .contract import AudioIdentity, SCHEMA, MAX_WINDOW_MS
from .dispatcher import Dispatcher
from .jobs import JobStore, QueueError, MAX_ATTEMPTS
from .runner import JobRunner
from .engine import ENGINE_ID, WhisperCpuEngine
from .model import DEFAULT_MODEL, model_directory, model_present
from .model_downloads import ModelDownloads, DownloadError
from .qualification import inventory as qualification_inventory


class Service(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, config, runner=None, model_available=None):
        self.config = config
        self.pairing = Pairing(config.data_dir)
        self.source = Audiobookshelf(config.source) if config.source else None
        self.slots = threading.BoundedSemaphore(8)
        self.model_available = model_available or (lambda: model_present(model_directory(config.data_dir)))
        self.downloads = ModelDownloads(config.data_dir, config.model_quota_mib)
        self.dispatcher = None
        self.runner = runner
        if self.source is not None and self.runner is None:
            self.runner = JobRunner(JobStore(config.data_dir / "queue", config.queue),
                                    self.source, WhisperCpuEngine(config.data_dir, config.engine), config.data_dir)
        super().__init__(address, Handler)
        if self.runner is not None:
            try:
                with self.runner.store.worker():
                    pass
            except QueueError as error:
                if str(error) != "worker_busy":
                    super().server_close()
                    raise
            self.dispatcher = Dispatcher(self.runner, config.queue.max_active)

    def readiness(self):
        if self.source is None:
            return "source_not_configured"
        if not self.model_available():
            return "model_not_prepared"
        if self.dispatcher is None or not self.dispatcher.thread.is_alive() or self.dispatcher.closed:
            return "worker_unavailable"
        return None

    def server_close(self):
        self.downloads.close()
        if self.dispatcher is not None:
            self.dispatcher.close()
        super().server_close()

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

    def _operator_authorized(self):
        values = self.headers.get_all("Authorization") or []
        if len(values) != 1:
            return False
        supplied = values[0].encode("utf-8")
        expected = ("Bearer " + self.server.config.token).encode("utf-8")
        return hmac.compare_digest(supplied, expected)

    def _authorized(self):
        if self._operator_authorized():
            return True
        values = self.headers.get_all("Authorization") or []
        if len(values) != 1 or not values[0].startswith("Bearer "):
            return False
        try:
            return self.server.pairing.authorized(values[0][7:])
        except (OSError, sqlite3.Error):
            return False

    def _pairing_request(self):
        if self.path in ("/v1/pairing/issue", "/v1/pairing/revoke-all"):
            if self.client_address[0] not in ("127.0.0.1", "::1") or not self._operator_authorized():
                return self._json(401, {"error": "unauthorized"})
            if self.path.endswith("/issue"):
                return self._json(200, {"code": self.server.pairing.issue(), "expires_in": TTL_SECONDS})
            try:
                self.server.pairing.revoke_all()
                return self._json(200, {"revoked": True})
            except (OSError, sqlite3.Error):
                return self._json(503, {"error": "pairing_storage_error"})
        try:
            body = self._body()
            if not isinstance(body, dict) or set(body) != {"code"} or not isinstance(body["code"], str):
                raise ValueError("invalid_request")
            status, result = self.server.pairing.redeem(body["code"])
            return self._json(status, result)
        except TimeoutError:
            return self._json(408, {"error": "request_timeout"})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._json(400, {"error": "invalid_request"})
        except (OSError, sqlite3.Error):
            return self._json(503, {"error": "pairing_storage_error"})

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "alive"})
        if self.path == "/readyz":
            reason = self.server.readiness()
            return self._json(503, {"status": "not_ready", "reason": reason}) if reason else self._json(200, {"status": "ready"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if self.path == "/v1/capabilities":
            return self._json(200, {
                "api_version": 1,
                "transcript_schema": SCHEMA,
                "engine_ready": self.server.readiness() is None,
                "transcription": self.server.readiness() is None,
                "engine_id": ENGINE_ID,
                "model_present": self.server.model_available(),
                "default_model": DEFAULT_MODEL,
                "models": self.server.downloads.inventory(),
                "model_download_api": True,
                "model_qualification_api": True,
                "model_quota_mib": self.server.config.model_quota_mib,
                "job_api": True,
                "running_job_cancel": True,
                "inference_timeout_seconds": self.server.config.engine.inference_timeout,
                "pairing_api": True,
                "max_window_ms": MAX_WINDOW_MS,
                "source_configured": self.server.source is not None,
                "source_id": self.server.config.source.source_id if self.server.config.source else None,
            })
        if self.path == "/v1/qualification":
            return self._json(200, qualification_inventory(self.server.config.data_dir, self.server.config.engine))
        if self.path == "/v1/models":
            return self._json(200, {"models": self.server.downloads.inventory(), "quota_mib": self.server.config.model_quota_mib})
        match = re.fullmatch(r"/v1/items/([A-Za-z0-9_-]{1,128})/manifest", self.path)
        if match:
            return self._manifest(match.group(1))
        match = re.fullmatch(r"/v1/jobs/([0-9a-f]{64})(/transcript)?", self.path)
        if match:
            return self._job_request(match.group(1), "transcript" if match.group(2) else "status")
        return self._json(404, {"error": "not_found"})

    def _manage_model(self, name, cancel=False):
        if self.client_address[0] not in ("127.0.0.1", "::1") or not self._operator_authorized():
            return self._json(401, {"error": "unauthorized"})
        try:
            accepted = self.server.downloads.cancel(name) if cancel else self.server.downloads.start(name)
            return self._json(202 if accepted else 200, {"accepted": accepted, "models": self.server.downloads.inventory()})
        except ValueError:
            return self._json(400, {"error": "unsupported_model"})
        except DownloadError as error:
            return self._json(409, {"error": str(error)})
        except (OSError, RuntimeError):
            return self._json(503, {"error": "download_unavailable"})

    def do_POST(self):
        match = re.fullmatch(r"/v1/models/([a-z0-9-]{1,32})/(download|cancel)", self.path)
        if match:
            return self._manage_model(match.group(1), match.group(2) == "cancel")
        if self.path in ("/v1/pairing/issue", "/v1/pairing/redeem", "/v1/pairing/revoke-all"):
            return self._pairing_request()
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if self.path != "/v1/jobs":
            return self._json(404, {"error": "not_found"})
        token = self._source_token()
        if token is None:
            return
        reason = self.server.readiness()
        if reason:
            return self._json(503, {"error": reason})
        try:
            body = self._body()
            required = {"source_id", "item_id", "audio_revision", "start_ms", "end_ms"}
            if not isinstance(body, dict) or not required <= body.keys() or body.keys() - required - {"retry"}:
                raise ValueError("invalid_request")
            identity = AudioIdentity(body["source_id"], body["item_id"], body["audio_revision"])
            if type(body.get("retry", False)) is not bool:
                raise ValueError("invalid_request")
            job = self.server.runner.submit(identity.item_id, token, body["start_ms"], body["end_ms"],
                                            identity.audio_revision, identity.source_id)
            job, accepted = self.server.dispatcher.schedule(job, token, body.get("retry", False))
            return self._json(202 if accepted else 200, self._status(job))
        except TimeoutError:
            return self._json(408, {"error": "request_timeout"})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._json(400, {"error": "invalid_request"})
        except (SourceError, QueueError) as error:
            return self._job_error(error)

    def do_DELETE(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        match = re.fullmatch(r"/v1/jobs/([0-9a-f]{64})", self.path)
        if not match:
            return self._json(404, {"error": "not_found"})
        return self._job_request(match.group(1), "cancel")

    def _body(self):
        if self.headers.get_all("Transfer-Encoding"):
            raise ValueError("invalid_request")
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,4}", lengths[0]):
            raise ValueError("invalid_request")
        length = int(lengths[0])
        if not 1 <= length <= 4096 or self.headers.get_content_type() != "application/json":
            raise ValueError("invalid_request")
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise ValueError("invalid_request")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_key")
                result[key] = value
            return result
        return json.loads(payload.decode("utf-8"), object_pairs_hook=unique)

    def _source_token(self):
        if self.server.source is None:
            self._json(503, {"error": "source_not_configured"})
            return None
        tokens = self.headers.get_all("X-Audiobookshelf-Token") or []
        if len(tokens) != 1 or not valid_bearer(tokens[0]):
            self._json(401, {"error": "source_credential_required"})
            return None
        return tokens[0]

    def _status(self, job):
        spec = job.spec
        return {
            "job_id": spec.id, "source_id": spec.identity.source_id,
            "item_id": spec.identity.item_id, "audio_revision": spec.identity.audio_revision,
            "start_ms": spec.start_ms, "end_ms": spec.end_ms, "profile": spec.profile,
            "state": job.state, "dispatch": self.server.dispatcher.dispatch_state(job),
            "attempts": job.attempts, "max_attempts": MAX_ATTEMPTS, "error": job.error,
        }

    def _job_request(self, job_id, action):
        token = self._source_token()
        if token is None:
            return
        try:
            if action == "cancel":
                active = self.server.dispatcher.cancel(job_id, token)
                if active:
                    return self._json(202, {"job_id": job_id, "cancel_requested": True})
                return self._json(200, {"job_id": job_id, "cancelled": True})
            if action == "transcript":
                return self._json(200, self.server.runner.result(job_id, token))
            return self._json(200, self._status(self.server.runner.status(job_id, token)))
        except (SourceError, QueueError) as error:
            return self._job_error(error)

    def _job_error(self, error):
        code = str(error)
        statuses = {
            "unauthorized": 401, "forbidden": 403, "not_found": 404, "job_not_found": 404,
            "stale_revision": 409, "profile_changed": 409, "job_not_ready": 409,
            "job_not_cancellable": 409, "queue_full": 429, "worker_busy": 503,
            "worker_unavailable": 503, "queue_storage_error": 503,
        }
        return self._json(statuses.get(code, 502), {"error": code})

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
