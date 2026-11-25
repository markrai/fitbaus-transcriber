import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from transcriber.audiobookshelf import SourceConfig, SourceError
from transcriber.config import Config
from transcriber.background import profile
from transcriber.contract import AudioIdentity, Cue, TranscriptWindow
from transcriber.dispatcher import Dispatcher
from transcriber.engine import EngineConfig, EngineError
from transcriber.jobs import JobStore, QueueLimits
from transcriber.runner import JobRunner
from transcriber.server import Service


OPERATOR = "a" * 64
USER = "source-user"
IDENTITY = AudioIdentity("home", "book", "b" * 64)


class Source:
    def __init__(self):
        self.identity = IDENTITY
        self.allowed = True

    def manifest(self, item, token):
        if token != USER or not self.allowed:
            raise SourceError("forbidden")
        if item != self.identity.item_id:
            raise SourceError("not_found")
        return SimpleNamespace(identity=self.identity, duration_ms=120000)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.source = Source()
        self.engine = SimpleNamespace(config=EngineConfig())
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail_inference = False
        self.model_ready = True
        self.limits = QueueLimits(max_jobs=8, max_active=3)
        self.store = JobStore(self.path / "queue", self.limits)
        self.runner = JobRunner(self.store, self.source, self.engine, self.path, self.transcribe)
        self.config = Config(OPERATOR, "127.0.0.1", 8765, self.path,
                             SourceConfig("home", "http://localhost:13378"), queue=self.limits)
        self.start_server()

    def start_server(self):
        self.server = Service(("127.0.0.1", 0), self.config, self.runner, lambda: self.model_ready)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def tearDown(self):
        self.stop_server()

    def transcribe(self, source, identity, token, directory, engine, start, end):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(5):
            raise EngineError("test_timeout")
        if self.fail_inference:
            raise EngineError("inference_failed")
        return TranscriptWindow(identity, start, end, (Cue(start, end, "Narration"),))

    def request(self, method, path, body=None, operator=OPERATOR, user=USER, extra_headers=None):
        headers = dict(extra_headers or {})
        if operator is not None:
            headers["Authorization"] = "Bearer " + operator
        if user is not None:
            headers["X-Audiobookshelf-Token"] = user
        if body is not None:
            headers["Content-Type"] = "application/json"
            if not isinstance(body, str):
                body = json.dumps(body)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def submit(self, start=0, end=1000, **extra):
        return self.request("POST", "/v1/jobs", {
            "source_id": "home", "item_id": "book", "audio_revision": IDENTITY.audio_revision,
            "start_ms": start, "end_ms": end, **extra,
        })

    def wait_state(self, job_id, state):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, body = self.request("GET", "/v1/jobs/" + job_id)
            if code == 200 and body["state"] == state and body["dispatch"] == "idle":
                return body
            time.sleep(0.02)
        self.fail("Job did not reach " + state)

    def test_background_api_requires_source_access_and_completes(self):
        background = self.server.background
        background.check_model = lambda *args, **kwargs: None
        background.execute = lambda source, identity, token, directory, engine, start, end, cancelled, fd: (
            TranscriptWindow(identity, start, end, (Cue(start, end, "Background narration"),)))
        body = {"source_id": "home", "item_id": "book",
                "audio_revision": IDENTITY.audio_revision, "model": "small"}
        self.assertEqual(self.request("POST", "/v1/background", body, operator=None)[0], 401)
        self.assertEqual(self.request("POST", "/v1/background", body, user="denied")[0], 403)
        code, job = self.request("POST", "/v1/background", body)
        self.assertEqual(code, 202)
        route = "/v1/background/" + job["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, result = self.request("GET", route)
            self.assertEqual(status, 200)
            if result["state"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["processed_ms"], 120000)
        self.assertEqual(self.request("GET", route, user="denied")[0], 403)
        self.assertEqual(self.request("GET", "/v1/items/book/background")[1]["jobs"][0]["job_id"], job["job_id"])
        self.assertEqual(self.request("POST", route + "/discard")[0], 200)
        self.assertEqual(self.request("GET", route)[0], 404)

    def test_paired_background_refresh_and_revocation(self):
        self.server.background.check_model = lambda *args, **kwargs: None
        entered = threading.Event()
        def execute(source, identity, token, directory, engine, start, end, cancelled, fd):
            entered.set()
            deadline = time.monotonic() + 5
            while not cancelled() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not cancelled():
                raise EngineError("test_timeout")
            raise EngineError("cancelled")
        self.server.background.execute = execute
        code = self.server.pairing.issue()
        status, redeemed = self.server.pairing.redeem(code)
        self.assertEqual(status, 200)
        body = {"source_id": "home", "item_id": "book", "audio_revision": IDENTITY.audio_revision, "model": "tiny"}
        headers = {"X-FitBaus-Background-Refresh": "private-refresh"}
        self.assertEqual(self.request("POST", "/v1/background", body, operator="invalid", extra_headers=headers)[0], 401)
        self.assertEqual(self.request("POST", "/v1/background", body, extra_headers={"X-FitBaus-Background-Lease": "invalid"})[0], 400)
        status, result = self.request("POST", "/v1/background", body, operator=redeemed["token"], extra_headers=headers)
        self.assertEqual(status, 202)
        self.assertTrue(result["authorization_active"])
        self.assertIsNone(result["authorization_remaining_seconds"])
        self.assertTrue(result["authorization_renewable"])
        self.assertNotIn("private-refresh", json.dumps(result))
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.request("POST", "/v1/pairing/revoke-all")[0], 200)
        deadline = time.monotonic() + 5
        route = "/v1/background/" + result["job_id"]
        while time.monotonic() < deadline:
            _, current = self.request("GET", route)
            if current["state"] == "waiting_credentials":
                break
            time.sleep(0.02)
        self.assertEqual(current["state"], "waiting_credentials")
        self.assertFalse(current["authorization_active"])
        self.assertEqual(current["processed_ms"], 0)
        self.assertEqual(self.request("GET", route, operator=redeemed["token"])[0], 401)
        entered.clear()
        _, second = self.server.pairing.redeem(self.server.pairing.issue())
        self.assertEqual(self.request("POST", route + "/resume", operator=second["token"], extra_headers=headers)[0], 202)
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.request("POST", "/v1/pairing/revoke-all")[0], 200)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, current = self.request("GET", route)
            if current["state"] == "waiting_credentials":
                break
            time.sleep(0.02)
        self.assertEqual(current["state"], "waiting_credentials")
        self.assertFalse(current["authorization_active"])
        self.assertEqual(current["processed_ms"], 0)

    def test_revocation_cancels_active_and_releases_queued_paired_live_jobs(self):
        self.release.clear()
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        def submit(start, end, operator):
            return self.request("POST", "/v1/jobs", {
                "source_id": "home", "item_id": "book",
                "audio_revision": IDENTITY.audio_revision,
                "start_ms": start, "end_ms": end,
            }, operator=operator)
        code, active = submit(0, 1000, device["token"])
        self.assertEqual(code, 202)
        self.assertTrue(self.entered.wait(3))
        code, queued = submit(1000, 2000, device["token"])
        self.assertEqual(code, 202)
        code, operator_job = submit(2000, 3000, OPERATOR)
        self.assertEqual(code, 202)
        self.assertEqual(self.request("POST", "/v1/pairing/revoke-all")[0], 200)
        dispatcher = self.server.dispatcher
        with dispatcher.condition:
            self.assertTrue(dispatcher.control.cancelled())
            self.assertNotIn(queued["job_id"], dispatcher.pending)
            self.assertIn(operator_job["job_id"], dispatcher.pending)
        route = "/v1/jobs/" + queued["job_id"]
        self.assertEqual(self.request("GET", route)[1]["dispatch"], "awaiting_credentials")
        self.assertEqual(self.request("GET", route, operator=device["token"])[0], 401)
        self.release.set()
        self.assertEqual(self.wait_state(active["job_id"], "failed")["error"], "cancelled")
        self.wait_state(operator_job["job_id"], "ready")
        self.assertEqual(self.calls, 2)
        self.assertEqual(submit(1000, 2000, OPERATOR)[0], 202)
        self.wait_state(queued["job_id"], "ready")

    def test_revoked_device_cannot_schedule_after_source_validation(self):
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        original = self.runner.submit
        def submit_and_revoke(*args, **kwargs):
            job = original(*args, **kwargs)
            self.server.pairing.revoke_all()
            return job
        self.runner.submit = submit_and_revoke
        status, body = self.request("POST", "/v1/jobs", {
            "source_id": "home", "item_id": "book", "audio_revision": IDENTITY.audio_revision,
            "start_ms": 0, "end_ms": 1000,
        }, operator=device["token"])
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        self.assertEqual(self.calls, 0)

    def test_automatic_reauthorization_never_resumes_paused_or_replaces_grant(self):
        background = self.server.background
        background.check_model = lambda *args, **kwargs: None
        job = background.store.submit(IDENTITY, "tiny", profile("tiny", self.engine.config), 120000)
        route = "/v1/background/" + job.id
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        background.defer_until = time.monotonic() + 60
        self.assertEqual(self.request("POST", route + "/reauthorize", user="denied")[0], 403)
        background.store.transition(job.id, "paused")
        status, value = self.request("POST", route + "/reauthorize", operator=device["token"])
        self.assertEqual(status, 200)
        self.assertEqual(value["state"], "paused")
        self.assertNotIn(job.id, background.pending)
        background.store.transition(job.id, "pending")
        status, value = self.request("POST", route + "/reauthorize", operator=device["token"])
        self.assertEqual(status, 202)
        self.assertTrue(value["authorization_active"])
        entry = background.pending[job.id]
        self.assertEqual(self.request("POST", route + "/reauthorize")[0], 202)
        self.assertIs(background.pending[job.id], entry)
        with background.lock:
            background.release(job.id)
            background.store.transition(job.id, "cancelled")
        self.assertEqual(self.request("POST", route + "/reauthorize")[1]["state"], "cancelled")

    def test_qualification_retry_requires_pairing_or_local_operator(self):
        calls = []
        self.server.qualification.retry = lambda name: (calls.append(name) or True)
        route = "/v1/models/base/qualify"
        self.assertEqual(self.request("POST", route, operator=None)[0], 401)
        self.assertEqual(self.request("POST", route, operator="invalid")[0], 401)
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        self.assertEqual(self.request("POST", route, operator=device["token"])[0], 202)
        self.assertEqual(self.request("POST", "/v1/models/small/qualify")[0], 202)
        self.assertEqual(self.request("POST", "/v1/models/tiny/qualify")[0], 404)
        self.assertEqual(calls, ["base", "small"])

    def test_prepared_window_uses_actual_profile_without_live_inference(self):
        prepared = self.server.background.store
        for name in ("tiny", "base", "small"):
            job = prepared.submit(IDENTITY, name, profile(name, self.engine.config), 120000)
            prepared.checkpoint(job, TranscriptWindow(IDENTITY, 0, 120000, (Cue(0, 120000, name),)))
        code, status = self.submit(0, 120000)
        self.assertEqual(code, 200)
        self.assertTrue(status["prepared"])
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["profile"], profile("small", self.engine.config))
        route = "/v1/jobs/" + status["job_id"]
        self.assertEqual(self.request("GET", route + "/transcript")[1]["cues"][0]["text"], "small")
        self.assertEqual(self.request("GET", route + "/transcript", user="denied")[0], 403)
        self.assertEqual(self.request("DELETE", route)[0], 409)
        self.assertEqual(self.calls, 0)
        self.stop_server()
        self.start_server()
        self.assertEqual(self.submit(0, 120000)[1]["job_id"], status["job_id"])
        self.assertEqual(self.request("GET", route + "/transcript")[1]["cues"][0]["text"], "small")
        self.source.identity = AudioIdentity("home", "book", "c" * 64)
        self.assertEqual(self.request("GET", route + "/transcript")[0], 409)
        self.source.identity = IDENTITY
        self.assertEqual(self.submit(0, 1000)[0], 202)

    def test_prepared_discard_removes_only_its_model_and_falls_back(self):
        prepared = self.server.background.store
        for name in ("base", "small"):
            job = prepared.submit(IDENTITY, name, profile(name, self.engine.config), 120000)
            prepared.checkpoint(job, TranscriptWindow(IDENTITY, 0, 120000, ()))
        first = self.submit(0, 120000)[1]
        self.assertTrue(first["prepared"])
        self.assertEqual(self.request("POST", "/v1/background/" + job.id + "/discard")[0], 200)
        self.assertEqual(self.request("GET", "/v1/jobs/" + first["job_id"])[0], 404)
        second = self.submit(0, 120000)[1]
        self.assertEqual(second["profile"], profile("base", self.engine.config))
        self.assertEqual(self.calls, 0)

    def test_readiness_distinguishes_model_availability(self):
        self.assertEqual(self.request("GET", "/readyz", operator=None, user=None)[0], 200)
        self.model_ready = False
        self.assertEqual(self.request("GET", "/readyz")[1]["reason"], "model_not_prepared")
        self.assertEqual(self.submit()[0], 503)

    def test_async_duplicate_cache_and_permission_checks(self):
        self.release.clear()
        code, job = self.submit()
        self.assertEqual(code, 202)
        job_id = job["job_id"]
        self.assertTrue(self.entered.wait(3))
        self.assertEqual(self.submit()[1]["job_id"], job_id)
        self.assertEqual(self.request("GET", "/v1/jobs/" + job_id)[1]["state"], "running")
        self.assertEqual(self.request("GET", "/v1/jobs/" + job_id + "/transcript")[0], 409)
        self.assertEqual(self.request("GET", "/v1/jobs/" + job_id, user=None)[0], 401)
        self.release.set()
        self.wait_state(job_id, "ready")
        code, transcript = self.request("GET", "/v1/jobs/" + job_id + "/transcript")
        self.assertEqual(code, 200)
        self.assertEqual(transcript["cues"][0]["text"], "Narration")
        self.assertEqual(self.submit()[0], 200)
        self.assertEqual(self.calls, 1)
        self.source.allowed = False
        self.assertEqual(self.request("GET", "/v1/jobs/" + job_id)[0], 403)
        self.assertEqual(self.request("GET", "/v1/jobs/" + job_id + "/transcript")[0], 403)

    def test_retry_only_when_explicit(self):
        self.fail_inference = True
        job_id = self.submit()[1]["job_id"]
        self.wait_state(job_id, "failed")
        self.assertEqual(self.submit()[0], 200)
        self.assertEqual(self.calls, 1)
        self.fail_inference = False
        self.assertEqual(self.submit(retry=True)[0], 202)
        self.assertEqual(self.wait_state(job_id, "ready")["attempts"], 2)

    def test_explicit_retry_reopens_exhausted_window_without_automatic_retries(self):
        self.fail_inference = True
        job_id = self.submit()[1]["job_id"]
        for attempt in range(1, 4):
            failed = self.wait_state(job_id, "failed")
            self.assertEqual(failed["attempts"], attempt)
            self.assertEqual(failed["error"], "inference_failed")
            self.assertEqual(self.submit()[0], 200)
            self.assertEqual(self.calls, attempt)
            if attempt < 3:
                code, accepted = self.submit(retry=True)
                self.assertEqual(code, 202)
                self.assertEqual(accepted["state"], "queued")
        self.fail_inference = False
        self.release.clear()
        self.entered.clear()
        try:
            code, accepted = self.submit(retry=True)
            self.assertEqual(code, 202)
            self.assertEqual(accepted["job_id"], job_id)
            self.assertEqual(accepted["state"], "queued")
            self.assertEqual(accepted["attempts"], 0)
            self.assertTrue(self.entered.wait(3))
            self.assertEqual(self.submit(retry=True)[0], 202)
        finally:
            self.release.set()
        self.assertEqual(self.wait_state(job_id, "ready")["attempts"], 1)
        self.assertEqual(self.calls, 4)

    def test_validation_and_stale_revision(self):
        for body in ("{}", '{"item_id":"a","item_id":"b"}', "x" * 4097):
            self.assertEqual(self.request("POST", "/v1/jobs", body)[0], 400)
        self.assertEqual(self.submit(start=True)[0], 400)
        self.assertEqual(self.submit(end=120001)[0], 400)
        self.assertEqual(self.submit(audio_revision="c" * 64)[0], 409)
        self.assertEqual(self.submit(source_id="other")[0], 404)
        job_id = self.submit()[1]["job_id"]
        self.wait_state(job_id, "ready")
        self.source.identity = AudioIdentity("home", "book", "c" * 64)
        self.assertEqual(self.request("GET", "/v1/jobs/" + job_id + "/transcript")[0], 409)

    def test_cancel_queued_and_running_and_enforce_capacity(self):
        self.release.clear()
        first = self.submit()[1]["job_id"]
        self.assertTrue(self.entered.wait(3))
        second = self.submit(1000, 2000)[1]["job_id"]
        self.assertEqual(self.submit(2000, 3000)[0], 202)
        self.assertEqual(self.submit(3000, 4000)[0], 429)
        self.assertEqual(self.request("DELETE", "/v1/jobs/" + first, user="denied")[0], 403)
        self.assertEqual(self.request("DELETE", "/v1/jobs/" + first)[0], 202)
        self.assertEqual(self.request("DELETE", "/v1/jobs/" + second, user="denied")[0], 403)
        self.assertEqual(self.request("DELETE", "/v1/jobs/" + second)[0], 200)
        self.assertEqual(self.request("GET", "/v1/jobs/" + second)[0], 404)
        self.assertEqual(self.submit(3000, 4000)[0], 202)
        self.release.set()
        self.assertEqual(self.wait_state(first, "failed")["error"], "cancelled")
        self.assertEqual(self.request("GET", "/v1/jobs/" + first + "/transcript")[0], 409)
        self.assertEqual(self.submit()[0], 200)
        self.assertEqual(self.submit(retry=True)[0], 202)
        self.wait_state(first, "ready")

    def test_restart_requires_fresh_credentials(self):
        self.stop_server()
        job = self.runner.submit("book", USER, 0, 1000)
        with self.store.worker():
            self.store.claim(job.spec.id)
        self.start_server()
        code, status = self.request("GET", "/v1/jobs/" + job.spec.id)
        self.assertEqual(code, 200)
        self.assertEqual(status["state"], "queued")
        self.assertEqual(status["dispatch"], "awaiting_credentials")
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.submit()[1]["job_id"], job.spec.id)
        self.assertEqual(self.wait_state(job.spec.id, "ready")["attempts"], 2)
        self.assertNotIn(USER.encode(), self.store.path.read_bytes())

    def test_expired_pending_credentials_are_not_used(self):
        self.stop_server()
        clock = [0.0]
        dispatcher = Dispatcher(self.runner, 3, credential_ttl=10, clock=lambda: clock[0])
        self.addCleanup(dispatcher.close)
        self.release.clear()
        first = self.runner.submit("book", USER, 0, 1000)
        second = self.runner.submit("book", USER, 1000, 2000)
        dispatcher.schedule(first, USER)
        self.assertTrue(self.entered.wait(3))
        dispatcher.schedule(second, USER)
        clock[0] = 11
        self.assertEqual(dispatcher.dispatch_state(second), "awaiting_credentials")
        self.release.set()
        dispatcher.close()
        self.start_server()
        self.assertEqual(self.store.get(second.spec.id).state, "queued")
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
