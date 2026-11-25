from dataclasses import replace
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from transcriber.audiobookshelf import SourceConfig, SourceError
from transcriber.background import profile
from transcriber.config import Config
from transcriber.contract import AudioIdentity, Cue, TranscriptWindow
from transcriber.engine import EngineConfig
from transcriber.jobs import JobStore
from transcriber.runner import JobRunner
from transcriber.server import Service
from transcriber.summaries import SummaryRequest
from transcriber.summary_cache import SummaryCache
from transcriber.summary_provider import SummaryProviderConfig, SummaryProviderError


IDENTITY = AudioIdentity("home", "book", "a" * 64)
TOKEN = "b" * 64
USER = "source-user"


class Source:
    def __init__(self):
        self.identity = IDENTITY
        self.allowed = True

    def manifest(self, item, token):
        if token != USER or not self.allowed:
            raise SourceError("forbidden")
        if item != self.identity.item_id:
            raise SourceError("not_found")
        return SimpleNamespace(identity=self.identity, duration_ms=750000)


class SummaryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.source = Source()
        self.engine = SimpleNamespace(config=EngineConfig())
        self.transcriptions = []
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.failure = None
        self.on_complete = None
        runner = JobRunner(JobStore(directory / "queue"), self.source, self.engine, directory, self.transcribe)
        config = Config(TOKEN, "127.0.0.1", 8765, directory, SourceConfig("home", "http://localhost:13378"),
                        summary_provider=SummaryProviderConfig("https://provider.example/v1", "test-model"))
        self.server = Service(("127.0.0.1", 0), config, runner, lambda: True)
        self.server.summary_provider = SimpleNamespace(complete=self.complete)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def transcribe(self, source, identity, token, directory, engine, start, end):
        self.transcriptions.append(start)
        return TranscriptWindow(identity, start, end, (Cue(start + 1000, start + 2000, "Window " + str(start)),))

    def complete(self, system, text):
        self.calls.append((system, text))
        self.entered.set()
        if not self.release.wait(5):
            raise SummaryProviderError("provider_timeout")
        if self.on_complete:
            self.on_complete()
        if self.failure:
            raise SummaryProviderError(self.failure)
        return "Concise summary."

    def request(self, body=None, operator=TOKEN, user=USER, path="/v1/summaries", method="POST"):
        headers = {}
        if operator is not None:
            headers["Authorization"] = "Bearer " + operator
        if user is not None:
            headers["X-Audiobookshelf-Token"] = user
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=7)
        try:
            client.request(method, path, body=body, headers=headers)
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def body(self, start=0, end=600000, **extra):
        return dict(source_id="home", item_id="book", audio_revision=IDENTITY.audio_revision,
                    start_ms=start, end_ms=end, **extra)

    def prepare(self, windows):
        store = self.server.background.store
        job = store.submit(IDENTITY, "small", profile("small", self.engine.config), 750000)
        for start, end, cues in windows:
            store.checkpoint(job, TranscriptWindow(IDENTITY, start, end, tuple(cues)))
            job = store.get(job.id)

    def test_pending_then_generates_from_verified_live_windows(self):
        code, result = self.request(self.body())
        self.assertEqual(code, 202)
        self.assertEqual(result["state"], "pending_transcript")
        self.assertEqual(self.calls, [])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = self.request(self.body())
            if code == 200:
                break
            self.assertEqual(code, 202)
            time.sleep(0.02)
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["summary"], "Concise summary.")
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(len(result["transcript_digest"]), 64)
        self.assertEqual(self.calls[0][1], "Window 0\nWindow 300000")
        self.assertEqual(self.transcriptions, [0, 300000])

    def test_prepared_three_windows_exclude_boundary_cues_and_use_no_whisper(self):
        self.prepare([
            (0, 300000, [Cue(90000, 110000, "outside first"), Cue(120000, 130000, "first")]),
            (300000, 600000, [Cue(300000, 310000, "second")]),
            (600000, 750000, [Cue(610000, 620000, "third"), Cue(690000, 710000, "outside last")]),
        ])
        code, result = self.request(self.body(100000, 700000))
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(self.calls[0][1], "first\nsecond\nthird")
        self.assertEqual(self.transcriptions, [])

    def test_empty_and_permissions_and_invalid_requests(self):
        self.prepare([(0, 300000, [])])
        body = self.body(0, 10000)
        self.assertEqual(self.request(body, operator=None)[0], 401)
        self.assertEqual(self.request(body, user=None)[0], 401)
        self.assertEqual(self.request(body, user="denied")[0], 403)
        self.assertEqual(self.request({**body, "audio_revision": "c" * 64})[0], 409)
        self.assertEqual(self.request({**body, "source_id": "other"})[0], 404)
        for bad in ({**body, "start_ms": True}, {**body, "end_ms": 600001},
                    {**body, "prompt": "override"}, self.body(700000, 750001)):
            self.assertEqual(self.request(bad)[0], 400)
        code, result = self.request(body)
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "empty")
        self.assertIsNone(result["summary"])
        self.assertEqual(self.calls, [])

    def test_busy_and_revoked_after_provider_call(self):
        self.prepare([(0, 300000, [Cue(1000, 2000, "first")])])
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        body = self.body(0, 10000)
        self.release.clear()
        results = []
        worker = threading.Thread(target=lambda: results.append(self.request(body, operator=device["token"])))
        worker.start()
        try:
            self.assertTrue(self.entered.wait(3))
            self.assertEqual(self.request(body), (429, {"error": "summary_busy"}))
            self.server.pairing.revoke_all()
        finally:
            self.release.set()
            worker.join(timeout=7)
        self.assertEqual(results, [(401, {"error": "unauthorized"})])
        self.assertEqual(len(self.calls), 1)

    def test_provider_failure_and_source_recheck(self):
        self.prepare([(0, 300000, [Cue(1000, 2000, "first")])])
        body = self.body(0, 10000)
        self.failure = "provider_authentication"
        self.assertEqual(self.request(body), (502, {"error": "provider_authentication"}))
        self.failure = None
        self.on_complete = lambda: setattr(self.source, "allowed", False)
        self.assertEqual(self.request(body), (403, {"error": "forbidden"}))
        self.server.summary_provider = None
        self.assertEqual(self.request(body)[0], 503)

    def test_cache_reuse_and_fresh_authorization(self):
        self.prepare([(0, 300000, [Cue(1000, 2000, "first")])])
        body = self.body(0, 10000)
        code, first = self.request(body)
        self.assertEqual(code, 200)
        self.assertFalse(first["cache_hit"])
        self.server.summary_cache = SummaryCache(self.server.config.data_dir / "summaries")
        self.failure = "provider_unavailable"
        code, second = self.request(body)
        self.assertEqual(code, 200)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.request(body, user="denied")[0], 403)
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        self.server.pairing.revoke_all()
        self.assertEqual(self.request(body, operator=device["token"])[0], 401)
        self.source.identity = AudioIdentity("home", "book", "c" * 64)
        self.assertEqual(self.request(body)[0], 409)
        self.assertEqual(len(self.calls), 1)

    def test_provider_epoch_and_transcript_profile_invalidate(self):
        runner = self.server.runner
        job = runner.submit("book", USER, 0, 300000)
        runner.store.claim(job.spec.id)
        runner.store.complete(job.spec.id, TranscriptWindow(IDENTITY, 0, 300000, (Cue(1000, 2000, "live"),)))
        body = self.body(0, 10000)
        first = self.request(body)[1]
        self.assertFalse(first["cache_hit"])
        self.prepare([(0, 300000, [Cue(1000, 2000, "prepared")])])
        second = self.request(body)[1]
        self.assertFalse(second["cache_hit"])
        self.assertNotEqual(first["transcript_digest"], second["transcript_digest"])
        self.assertEqual(self.calls[-1][1], "prepared")
        config = self.server.config
        self.server.config = replace(config, summary_provider=replace(config.summary_provider, cache_epoch="2"))
        third = self.request(body)[1]
        self.assertFalse(third["cache_hit"])
        self.assertEqual(second["transcript_digest"], third["transcript_digest"])
        self.assertEqual(len(self.calls), 3)

    def test_failed_provider_and_cache_storage_error(self):
        self.prepare([(0, 300000, [Cue(1000, 2000, "first")])])
        body = self.body(0, 10000)
        self.failure = "provider_unavailable"
        self.assertEqual(self.request(body)[0], 502)
        self.failure = None
        self.assertFalse(self.request(body)[1]["cache_hit"])
        self.assertTrue(self.request(body)[1]["cache_hit"])
        self.assertEqual(len(self.calls), 2)
        blocked = self.server.config.data_dir / "blocked"
        blocked.write_text("not a directory")
        self.server.summary_cache = SummaryCache(blocked)
        self.assertEqual(self.request(body), (503, {"error": "summary_cache_unavailable"}))
        self.assertEqual(len(self.calls), 2)

    def test_window_math_and_capability(self):
        request = SummaryRequest.parse(self.body(299999, 750000))
        self.assertEqual(request.windows(750000), [(0, 300000), (300000, 600000), (600000, 750000)])
        self.assertTrue(self.request(path="/v1/capabilities", method="GET")[1]["summary_api"])
