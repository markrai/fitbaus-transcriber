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

    def request(self, method, path, body=None, operator=OPERATOR, user=USER):
        headers = {}
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
