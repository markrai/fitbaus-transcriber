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
from .background import Background, BackgroundStore, PreparedSpec
from .jobs import JobStore, QueueError, MAX_ATTEMPTS
from .runner import JobRunner
from .engine import ENGINE_ID, WhisperCpuEngine
from .model import DEFAULT_MODEL, model_directory, model_present
from .model_downloads import ModelDownloads, DownloadError
from .qualification import inventory as qualification_inventory
from .automatic_qualification import AutomaticQualification
from .summary_provider import OpenAIChatProvider, SummaryProviderError, summary_capability
from .summaries import RecapRequest, SummaryError, SummaryRequest, generate_summary
from .summary_cache import SummaryCache, SummaryCacheError
from .section_plan import SectionPlanRequest, section_plan
from .cumulative_recap import CumulativeRecapRequest, cumulative_recap, MAX_POSITION_MS


class Service(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, config, runner=None, model_available=None):
        self.config = config
        self.pairing = Pairing(config.data_dir)
        self.source = Audiobookshelf(config.source) if config.source else None
        self.slots = threading.BoundedSemaphore(8)
        self.summary_slot = threading.BoundedSemaphore(1)
        self.section_plan_slot = threading.BoundedSemaphore(1)
        self.summary_provider = OpenAIChatProvider(config.summary_provider) if config.summary_provider else None
        self.summary_cache = SummaryCache(config.data_dir / "summaries")
        self.model_available = model_available or (lambda: model_present(model_directory(config.data_dir)))
        self.downloads = ModelDownloads(config.data_dir, config.model_quota_mib)
        self.dispatcher = None
        self.background = None
        self.qualification = None
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
            prepared = BackgroundStore(config.data_dir / "background")
            self.runner.prepared = prepared
            self.background = Background(self.runner, prepared)
            self.qualification = AutomaticQualification(self.runner)
            self.dispatcher = Dispatcher(self.runner, config.queue.max_active, background=self.background,
                                         qualification=self.qualification)

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

    def _device_guard(self):
        if self._operator_authorized():
            return None
        pairing = self.server.pairing
        device_token = self.headers["Authorization"][7:]
        def guard():
            try:
                return pairing.authorized(device_token)
            except (OSError, sqlite3.Error):
                return False
        return guard

    def _pairing_request(self):
        if self.path in ("/v1/pairing/issue", "/v1/pairing/revoke-all"):
            if self.client_address[0] not in ("127.0.0.1", "::1") or not self._operator_authorized():
                return self._json(401, {"error": "unauthorized"})
            if self.path.endswith("/issue"):
                return self._json(200, {"code": self.server.pairing.issue(), "expires_in": TTL_SECONDS})
            try:
                self.server.pairing.revoke_all()
                if self.server.dispatcher is not None:
                    self.server.dispatcher.revoke_paired()
                if self.server.background is not None:
                    self.server.background.revoke_paired()
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
                "automatic_model_qualification": bool(self.server.qualification and self.server.qualification.available),
                "model_quota_mib": self.server.config.model_quota_mib,
                "job_api": True,
                "background_job_api": True,
                "prepared_transcripts": True,
                "unattended_background_local": True,
                "background_job_lifetime_authorization": True,
                "paired_model_management": True,
                "paired_background_refresh": True,
                "background_reauthorize": True,
                "source_url": self.server.config.source.base_url if self.server.config.source else None,
                "running_job_cancel": True,
                "inference_timeout_seconds": self.server.config.engine.inference_timeout,
                "pairing_api": True,
                "summary_provider": summary_capability(self.server.config.summary_provider),
                "summary_api": True,
                "recap_api": True,
                "cumulative_recap_api": True,
                "cumulative_recap_max_position_ms": MAX_POSITION_MS,
                "section_plan_api": True,
                "max_window_ms": MAX_WINDOW_MS,
                "source_configured": self.server.source is not None,
                "source_id": self.server.config.source.source_id if self.server.config.source else None,
            })
        if self.path == "/v1/qualification":
            result = (self.server.qualification.inventory() if self.server.qualification is not None else
                      qualification_inventory(self.server.config.data_dir, self.server.config.engine))
            return self._json(200, result)
        if self.path == "/v1/models":
            return self._json(200, {"models": self.server.downloads.inventory(), "quota_mib": self.server.config.model_quota_mib})
        match = re.fullmatch(r"/v1/background/([0-9a-f]{64})", self.path)
        if match:
            return self._background_request("status", match.group(1))
        match = re.fullmatch(r"/v1/items/([A-Za-z0-9_-]{1,128})/background", self.path)
        if match:
            return self._background_request("list", match.group(1))
        match = re.fullmatch(r"/v1/items/([A-Za-z0-9_-]{1,128})/manifest", self.path)
        if match:
            return self._manifest(match.group(1))
        match = re.fullmatch(r"/v1/jobs/([0-9a-f]{64})(/transcript)?", self.path)
        if match:
            return self._job_request(match.group(1), "transcript" if match.group(2) else "status")
        return self._json(404, {"error": "not_found"})

    def _manage_model(self, name, cancel=False):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if self._operator_authorized() and self.client_address[0] not in ("127.0.0.1", "::1"):
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
        match = re.fullmatch(r"/v1/models/(base|small)/qualify", self.path)
        if match:
            if not self._authorized() or (self._operator_authorized() and self.client_address[0] not in ("127.0.0.1", "::1")):
                return self._json(401, {"error": "unauthorized"})
            if self.server.qualification is None:
                return self._json(503, {"error": "qualification_unavailable"})
            try:
                accepted = self.server.qualification.retry(match.group(1))
                self.server.dispatcher.wake()
                return self._json(202 if accepted else 200, {"accepted": accepted})
            except QueueError as error:
                return self._job_error(error)
        match = re.fullmatch(r"/v1/models/([a-z0-9-]{1,32})/(download|cancel)", self.path)
        if match:
            return self._manage_model(match.group(1), match.group(2) == "cancel")
        if self.path in ("/v1/pairing/issue", "/v1/pairing/redeem", "/v1/pairing/revoke-all"):
            return self._pairing_request()
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if self.path == "/v1/section-plan":
            return self._section_plan_request()
        if self.path == "/v1/summaries":
            return self._summary_request()
        if self.path == "/v1/recaps":
            return self._summary_request(recap=True)
        if self.path == "/v1/cumulative-recaps":
            return self._summary_request(cumulative=True)
        if self.path == "/v1/background":
            return self._background_request("submit")
        match = re.fullmatch(r"/v1/background/([0-9a-f]{64})/(resume|pause|cancel|discard|reauthorize)", self.path)
        if match:
            return self._background_request(match.group(2), match.group(1))
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
            job, accepted = self.server.dispatcher.schedule(job, token, body.get("retry", False),
                                                            guard=self._device_guard())
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

    def _section_plan_request(self):
        token = self._source_token()
        if token is None:
            return
        service = self.server
        if service.runner is None:
            return self._json(503, {"error": "worker_unavailable"})
        try:
            request = SectionPlanRequest.parse(self._body())
        except TimeoutError:
            return self._json(408, {"error": "request_timeout"})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._json(400, {"error": "invalid_request"})
        if not service.section_plan_slot.acquire(blocking=False):
            return self._json(429, {"error": "section_plan_busy"})
        try:
            result = section_plan(request, token, service.runner, self._device_guard())
        except (SourceError, QueueError) as error:
            return self._job_error(error)
        except SummaryError as error:
            return self._json(error.status, {"error": error.code})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._json(400, {"error": "invalid_request"})
        finally:
            service.section_plan_slot.release()
        return self._json(200, result)

    def _summary_request(self, recap=False, cumulative=False):
        token = self._source_token()
        if token is None:
            return
        service = self.server
        if service.summary_provider is None:
            return self._json(503, {"error": "summary_provider_not_configured"})
        if service.runner is None or service.dispatcher is None:
            return self._json(503, {"error": "worker_unavailable"})
        try:
            request_type = CumulativeRecapRequest if cumulative else RecapRequest if recap else SummaryRequest
            request = request_type.parse(self._body())
        except TimeoutError:
            return self._json(408, {"error": "request_timeout"})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._json(400, {"error": "invalid_request"})
        if not service.summary_slot.acquire(blocking=False):
            return self._json(429, {"error": "summary_busy"})
        try:
            if cumulative:
                result = cumulative_recap(request, token, service.runner,
                                          service.summary_provider, service.config.summary_provider,
                                          self._device_guard(), cache=service.summary_cache,
                                          source_url=service.config.source.base_url)
            else:
                result = generate_summary(request, token, service.runner, service.dispatcher,
                                          service.summary_provider, service.config.summary_provider,
                                          service.readiness, self._device_guard(), cache=service.summary_cache,
                                          source_url=service.config.source.base_url)
        except (SourceError, QueueError) as error:
            return self._job_error(error)
        except SummaryError as error:
            return self._json(error.status, {"error": error.code})
        except SummaryCacheError:
            return self._json(503, {"error": "summary_cache_unavailable"})
        except SummaryProviderError as error:
            code = str(error)
            if code not in ("provider_authentication", "provider_rate_limited", "provider_http_error",
                            "provider_unavailable", "provider_timeout", "response_too_large",
                            "invalid_response", "invalid_completion"):
                code = "provider_unavailable"
            return self._json(504 if code == "provider_timeout" else 502, {"error": code})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._json(400, {"error": "invalid_request"})
        finally:
            service.summary_slot.release()
        status = 409 if result["state"] == "missing_transcript" else (
            202 if result["state"] in ("pending_transcript", "pending_summary") else 200)
        return self._json(status, result)

    def _background_request(self, action, identifier=None):
        token = self._source_token()
        if token is None:
            return
        background = self.server.background
        if background is None:
            return self._json(503, {"error": "worker_unavailable"})
        try:
            leases = self.headers.get_all("X-FitBaus-Background-Lease") or []
            refreshes = self.headers.get_all("X-FitBaus-Background-Refresh") or []
            operator = self._operator_authorized()
            guard = self._device_guard()
            if leases or refreshes:
                if operator and self.client_address[0] not in ("127.0.0.1", "::1"):
                    return self._json(401, {"error": "unauthorized"})
                if leases and not operator:
                    return self._json(401, {"error": "unauthorized"})
                if action not in ("submit", "resume") or leases not in ([], ["24h"]) or len(refreshes) > 1 or (
                        refreshes and not valid_bearer(refreshes[0])):
                    raise ValueError("invalid_request")
            refresh = refreshes[0] if refreshes else None
            if action == "submit":
                body = self._body()
                required = {"source_id", "item_id", "audio_revision", "model"}
                if not isinstance(body, dict) or not required <= body.keys() or body.keys() - required - {"start_ms"}:
                    raise ValueError("invalid_request")
                identity = AudioIdentity(body["source_id"], body["item_id"], body["audio_revision"])
                result = background.submit(identity, body["model"], token, body.get("start_ms", 0),
                                           refresh=refresh, guard=guard)
            elif action == "list":
                manifest = background.runner.source.manifest(identifier, token)
                result = {"jobs": [background.status(job) for job in background.store.for_item(
                    manifest.identity.source_id, manifest.identity.item_id)]}
            elif action == "status":
                job = background.store.get(identifier)
                background.authorize(job, token, exact=False)
                result = background.status(job)
            else:
                result = background.action(identifier, token, action, refresh=refresh, guard=guard)
            self.server.dispatcher.wake()
            return self._json(202 if action in ("submit", "resume", "reauthorize") and
                              result.get("state") in ("queued", "processing", "waiting_resources") else 200, result)
        except TimeoutError:
            return self._json(408, {"error": "request_timeout"})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._json(400, {"error": "invalid_request"})
        except (SourceError, QueueError) as error:
            return self._job_error(error)

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
            "prepared": isinstance(spec, PreparedSpec),
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
            "background_not_found": 404, "background_full": 429, "background_storage_full": 409,
            "background_not_discardable": 409, "background_busy": 409,
            "background_storage_error": 503, "background_disk_full": 409, "model_not_prepared": 409,
            "model_not_qualified": 409, "resource_unavailable": 503, "qualification_unavailable": 503,
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
