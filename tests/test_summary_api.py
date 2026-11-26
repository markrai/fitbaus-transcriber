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
from transcriber.summaries import RecapRequest, SummaryRequest
from transcriber.summary_cache import SummaryCache
from transcriber.section_diagrams import DiagramCache, digest_text
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

    def diagram_body(self, summary=None):
        if summary is None:
            self.prepare([(0, 300000, [Cue(1000, 2000, "first")])])
            code, summary = self.request(self.body(0, 10000))
            self.assertEqual(code, 200)
        return {**self.body(0, 10000), "transcript_digest": summary["transcript_digest"],
                "summary_model": summary["model"], "summary_prompt_version": summary["prompt_version"],
                "summary_digest": digest_text(summary["summary"])}

    def diagram(self, body, **kwargs):
        return self.request(body, path="/v1/section-diagrams", **kwargs)

    def diagram_provider(self, system, text):
        self.complete(system, text)
        return 'Here is the graph [concepts]:\n\x60\x60\x60json\n{"nodes":["First concept","Second concept","Unused"],"edges":[[1,2],[1,2],[3,3]]}\n\x60\x60\x60\nDone.'

    def test_diagram_http_cache_and_parent_authorization(self):
        body = self.diagram_body()
        self.server.summary_provider = SimpleNamespace(complete=self.diagram_provider)
        code, capabilities = self.request(path="/v1/capabilities", method="GET")
        self.assertEqual(code, 200)
        self.assertTrue(capabilities["summary_diagram_api"])
        self.assertEqual(capabilities["summary_diagram_formats"], ["flowchart"])
        code, result = self.diagram(body)
        self.assertEqual(code, 200)
        self.assertEqual(result["schema"], "fitbaus.section-diagram/v1")
        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["cache_hit"])
        self.assertEqual(result["graph"]["edges"], [{"from": "A", "to": "B"}])
        self.assertEqual(result["mermaid"], 'flowchart TD\nA["First concept"]\nB["Second concept"]\nA --> B')
        self.assertEqual(result["diagram_prompt_version"], "flowchart-v1")
        self.assertEqual(result["parser_version"], "flowchart-subset-v1")
        for key, value in body.items():
            self.assertEqual(result[key], value)
        self.assertEqual(self.calls[-1][1], "Concise summary.")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.transcriptions, [])
        self.server.diagram_cache = DiagramCache(self.server.config.data_dir / "diagrams")
        self.failure = "provider_unavailable"
        self.assertTrue(self.diagram(body)[1]["cache_hit"])
        self.assertEqual(self.diagram(body, user="denied")[0], 403)
        self.assertEqual(self.diagram(body, operator=None)[0], 401)
        self.assertEqual(self.diagram(body, user=None)[0], 401)
        self.assertEqual(self.diagram({**body, "summary_digest": "c" * 64})[0], 409)
        self.assertEqual(self.diagram({**body, "transcript_digest": "d" * 64})[0], 409)
        self.assertEqual(self.diagram({**body, "summary_model": "another-model"})[0], 409)
        self.assertEqual(self.diagram({**body, "prompt": "override"})[0], 400)
        config = self.server.config
        self.server.config = replace(config, summary_provider=replace(config.summary_provider, cache_epoch="2"))
        self.assertEqual(self.diagram(body)[0], 409)
        self.assertEqual(len(self.calls), 2)

    def test_diagram_missing_parent_never_schedules_transcript(self):
        body = {**self.body(0, 10000), "transcript_digest": "a" * 64,
                "summary_model": "test-model", "summary_prompt_version": "section-v1", "summary_digest": "b" * 64}
        self.assertEqual(self.diagram(body), (409, {"error": "diagram_parent_unavailable"}))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transcriptions, [])

    def test_diagram_invalid_graph_and_revocation_never_cache(self):
        body = self.diagram_body()
        self.server.summary_provider = SimpleNamespace(complete=self.complete)
        self.assertEqual(self.diagram(body), (502, {"error": "invalid_diagram"}))
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        self.server.summary_provider = SimpleNamespace(complete=self.diagram_provider)
        self.on_complete = self.server.pairing.revoke_all
        self.assertEqual(self.diagram(body, operator=device["token"]), (401, {"error": "unauthorized"}))
        self.on_complete = None
        self.assertFalse(self.diagram(body)[1]["cache_hit"])
        self.assertTrue(self.diagram(body)[1]["cache_hit"])

    def test_diagram_defers_when_summary_slot_busy(self):
        body = self.diagram_body()
        self.server.summary_provider = SimpleNamespace(complete=self.diagram_provider)
        self.assertTrue(self.server.summary_slot.acquire_foreground())
        try:
            code, deferred = self.diagram(body)
            self.assertEqual(code, 202)
            self.assertEqual(deferred["state"], "deferred")
            self.assertNotIn("graph", deferred)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.diagram(body, user="denied")[0], 403)
        finally:
            self.server.summary_slot.release()
        self.assertEqual(self.diagram(body)[0], 200)

    def test_foreground_summary_admitted_before_next_diagram(self):
        body = self.diagram_body()
        self.server.summary_provider = SimpleNamespace(complete=self.diagram_provider)
        self.entered.clear()
        self.release.clear()
        diagram_results, summary_results = [], []
        diagram_worker = threading.Thread(target=lambda: diagram_results.append(self.diagram(body)))
        summary_worker = threading.Thread(target=lambda: summary_results.append(self.request(self.body(0, 10000))))
        diagram_worker.start()
        try:
            self.assertTrue(self.entered.wait(3))
            summary_worker.start()
            deadline = time.monotonic() + 2
            while self.server.summary_slot.diagram_dispatch_allowed() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(self.server.summary_slot.diagram_dispatch_allowed())
            self.assertEqual(self.diagram(body)[0], 202)
        finally:
            self.release.set()
            diagram_worker.join(7)
            if summary_worker.ident is not None:
                summary_worker.join(7)
        self.assertEqual(diagram_results[0][0], 200)
        self.assertEqual(summary_results[0][0], 200)
        self.assertTrue(summary_results[0][1]["cache_hit"])
        self.assertEqual(len(self.calls), 2)

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

    def test_section_plan_uses_prepared_windows_without_inference(self):
        self.prepare([
            (0, 300000, [Cue(280000, 290000, "Sentence ends.")]),
            (300000, 600000, [Cue(300000, 305000, "Next phrase")]),
        ])
        body = {"source_id": "home", "item_id": "book", "audio_revision": IDENTITY.audio_revision,
                "boundaries_ms": [0, 300000, 600000]}
        self.server.summary_provider = None
        code, result = self.request(body, path="/v1/section-plan")
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "refined")
        self.assertEqual(result["boundaries_ms"], [0, 290000, 600000])
        self.assertEqual(result["nominal_boundaries_ms"], body["boundaries_ms"])
        self.assertEqual(len(result["transcript_digest"]), 64)
        self.assertEqual(self.request(body, path="/v1/section-plan"), (200, result))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transcriptions, [])
        self.assertTrue(self.request(path="/v1/capabilities", method="GET")[1]["section_plan_api"])
        self.assertEqual(self.request(body, operator=None, path="/v1/section-plan")[0], 401)
        self.assertEqual(self.request(body, user=None, path="/v1/section-plan")[0], 401)
        self.assertEqual(self.request(body, user="denied", path="/v1/section-plan")[0], 403)
        self.assertEqual(self.request({**body, "audio_revision": "c" * 64}, path="/v1/section-plan")[0], 409)
        self.assertEqual(self.request({**body, "source_id": "other"}, path="/v1/section-plan")[0], 404)

    def test_section_plan_missing_transcript_does_not_schedule_work(self):
        self.prepare([(0, 300000, [Cue(280000, 290000, "Sentence ends.")])])
        body = {"source_id": "home", "item_id": "book", "audio_revision": IDENTITY.audio_revision,
                "boundaries_ms": [0, 300000, 600000]}
        code, result = self.request(body, path="/v1/section-plan")
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "nominal")
        self.assertEqual(result["reason"], "missing_transcript")
        self.assertEqual(result["boundaries_ms"], body["boundaries_ms"])
        self.assertIsNone(result["transcript_digest"])
        self.assertEqual(self.transcriptions, [])
        self.assertEqual(self.calls, [])
        for invalid in ([0, True, 600000], [0, 600001], [0, 300000, 300000], [0, 400000, 750001]):
            self.assertEqual(self.request({**body, "boundaries_ms": invalid}, path="/v1/section-plan")[0], 400)

    def test_section_plan_revocation_is_checked_after_reading(self):
        self.prepare([
            (0, 300000, [Cue(280000, 290000, "Sentence ends.")]),
            (300000, 600000, []),
        ])
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        original = self.server.runner.prepared.find_prepared
        def revoke(*args):
            result = original(*args)
            self.server.pairing.revoke_all()
            return result
        self.server.runner.prepared.find_prepared = revoke
        body = {"source_id": "home", "item_id": "book", "audio_revision": IDENTITY.audio_revision,
                "boundaries_ms": [0, 300000, 600000]}
        self.assertEqual(self.request(body, operator=device["token"], path="/v1/section-plan")[0], 401)

    def recap_body(self, position=10000, **extra):
        return dict(source_id="home", item_id="book", audio_revision=IDENTITY.audio_revision,
                    position_ms=position, **extra)

    def recap(self, position=10000, **kwargs):
        return self.request(self.recap_body(position), path="/v1/recaps", **kwargs)

    def test_recap_excludes_unheard_and_straddling_cues(self):
        self.prepare([
            (0, 300000, [Cue(90000, 110000, "too old straddling"), Cue(120000, 130000, "heard one")]),
            (300000, 600000, [Cue(300000, 310000, "heard two")]),
            (600000, 750000, [Cue(610000, 620000, "heard three"),
                               Cue(680000, 690000, "heard at cutoff"),
                               Cue(690000, 710000, "unheard continuation"),
                               Cue(720000, 730000, "future")]),
        ])
        code, result = self.recap(700000)
        self.assertEqual(code, 200)
        self.assertEqual(result["schema"], "fitbaus.recap/v1")
        self.assertEqual(result["position_ms"], 700000)
        self.assertEqual((result["start_ms"], result["end_ms"]), (100000, 700000))
        self.assertEqual(result["prompt_version"], "recap-v1")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(self.calls[0][1], "heard one\nheard two\nheard three\nheard at cutoff")
        self.assertIn("Do not add information", self.calls[0][0])
        self.assertEqual(self.transcriptions, [])

    def test_recap_cache_is_separate_and_bound_to_exact_position(self):
        self.prepare([(0, 300000, [Cue(1000, 2000, "first"), Cue(10000, 11000, "next")])])
        self.assertFalse(self.request(self.body(0, 10000))[1]["cache_hit"])
        first = self.recap()[1]
        self.assertFalse(first["cache_hit"])
        self.assertTrue(self.recap()[1]["cache_hit"])
        self.assertFalse(self.recap(11000)[1]["cache_hit"])
        self.assertEqual(self.calls[-1][1], "first\nnext")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.recap(user="denied")[0], 403)
        self.source.identity = AudioIdentity("home", "book", "c" * 64)
        self.assertEqual(self.recap()[0], 409)
        self.assertEqual(len(self.calls), 3)

    def test_recap_zero_and_no_contained_narration_do_not_invoke_provider(self):
        code, zero = self.recap(0)
        self.assertEqual(code, 200)
        self.assertEqual((zero["start_ms"], zero["end_ms"], zero["position_ms"]), (0, 0, 0))
        self.assertEqual(zero["state"], "empty")
        self.assertIsNone(zero["summary"])
        self.assertEqual(self.transcriptions, [])
        self.assertEqual(self.recap(0, operator=None)[0], 401)
        self.assertEqual(self.recap(0, user="denied")[0], 403)
        self.prepare([(0, 300000, [Cue(1000, 2000, "not yet fully heard")])])
        self.assertEqual(self.recap(1500)[1]["state"], "empty")
        self.assertEqual(self.calls, [])

    def test_recap_pending_resolves_only_intersecting_windows(self):
        code, result = self.recap(310000)
        self.assertEqual(code, 202)
        self.assertEqual(result["state"], "pending_transcript")
        self.assertEqual(result["position_ms"], 310000)
        self.assertTrue(set(result["pending_windows"]) <= {0, 300000})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = self.recap(310000)
            if code == 200:
                break
            self.assertEqual(code, 202)
            time.sleep(0.02)
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(self.transcriptions, [0, 300000])
        self.assertEqual(self.calls[0][1], "Window 0\nWindow 300000")

    def test_recap_rejects_invalid_positions_and_caller_supplied_ranges(self):
        for position in (-1, True, "10000", 750001, 10000.5):
            self.assertEqual(self.recap(position)[0], 400)
        for extra in ({"start_ms": 0}, {"end_ms": 20000}, {"prompt": "override"}, {"retry": 1}):
            self.assertEqual(self.request(self.recap_body(**extra), path="/v1/recaps")[0], 400)
        self.assertEqual(self.request(self.recap_body(), path="/v1/summaries")[0], 400)
        self.assertEqual(self.request(self.body(), path="/v1/recaps")[0], 400)
        self.assertEqual(self.recap(operator=None)[0], 401)
        self.assertEqual(self.recap(user=None)[0], 401)

    def test_recap_revocation_and_shared_summary_slot(self):
        self.prepare([(0, 300000, [Cue(1000, 2000, "first")])])
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        self.release.clear()
        results = []
        worker = threading.Thread(target=lambda: results.append(self.recap(operator=device["token"])))
        worker.start()
        try:
            self.assertTrue(self.entered.wait(3))
            self.assertEqual(self.request(self.body(0, 10000)), (429, {"error": "summary_busy"}))
            self.server.pairing.revoke_all()
        finally:
            self.release.set()
            worker.join(timeout=7)
        self.assertEqual(results, [(401, {"error": "unauthorized"})])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.recap()[1]["cache_hit"])

    def test_recap_window_math_and_capability(self):
        request = RecapRequest.parse(self.recap_body(700000))
        self.assertEqual(request.windows(750000), [(0, 300000), (300000, 600000), (600000, 750000)])
        self.assertEqual(RecapRequest.parse(self.recap_body(300000)).windows(750000), [(0, 300000)])
        self.assertEqual(RecapRequest.parse(self.recap_body(0)).windows(750000), [])
        self.assertTrue(self.request(path="/v1/capabilities", method="GET")[1]["recap_api"])

    def test_recap_digest_tracks_profile_but_never_sends_future_cues(self):
        runner = self.server.runner
        job = runner.submit("book", USER, 0, 300000)
        runner.store.claim(job.spec.id)
        runner.store.complete(job.spec.id, TranscriptWindow(IDENTITY, 0, 300000,
                              (Cue(1000, 2000, "heard"), Cue(20000, 21000, "future one"))))
        first = self.recap()[1]
        self.assertFalse(first["cache_hit"])
        self.prepare([(0, 300000, [Cue(1000, 2000, "heard"), Cue(20000, 21000, "future two")])])
        second = self.recap()[1]
        self.assertFalse(second["cache_hit"])
        self.assertNotEqual(first["transcript_digest"], second["transcript_digest"])
        self.assertEqual(self.calls[0][1], self.calls[1][1])
        self.assertEqual(self.calls[1][1], "heard")

    def cumulative(self, position=735000, **kwargs):
        return self.request(self.recap_body(position), path="/v1/cumulative-recaps", **kwargs)

    def test_cumulative_hierarchy_is_bounded_and_reuses_cached_prefix(self):
        self.prepare([
            (0, 300000, [Cue(1000, 2000, "beginning")]),
            (300000, 600000, [Cue(301000, 302000, "middle")]),
            (600000, 750000, [Cue(601000, 602000, "recent"),
                               Cue(730000, 740000, "straddling cutoff"),
                               Cue(745000, 746000, "unheard")]),
        ])
        for index in (1, 2, 3):
            code, result = self.cumulative()
            self.assertEqual(code, 200 if index == 3 else 202)
            self.assertEqual(result["state"], "ready" if index == 3 else "pending_summary")
            self.assertEqual(result["completed_nodes"], index)
            self.assertEqual(result["total_nodes"], 3)
            self.assertEqual(result["schema"], "fitbaus.cumulative-recap/v1")
            self.assertEqual(result["algorithm"], "cumulative-v1")
            self.assertEqual((result["start_ms"], result["end_ms"], result["position_ms"]), (0, 735000, 735000))
            self.assertEqual(len(self.calls), index)
        self.assertEqual(self.calls[0][1], "beginning\nmiddle")
        self.assertEqual(self.calls[1][1], "recent")
        self.assertEqual(self.calls[2][1], "Concise summary.\n\nConcise summary.")
        self.assertFalse(result["cache_hit"])
        self.assertEqual(result["summary"], "Concise summary.")
        self.assertTrue(self.cumulative()[1]["cache_hit"])
        self.assertTrue(self.cumulative(600000)[1]["cache_hit"])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.transcriptions, [])
        self.assertEqual(self.cumulative(user="denied")[0], 403)
        self.source.identity = AudioIdentity("home", "book", "c" * 64)
        self.assertEqual(self.cumulative()[0], 409)

    def test_cumulative_missing_coverage_never_generates_or_schedules(self):
        self.prepare([(0, 300000, [Cue(1000, 2000, "beginning")])])
        code, result = self.cumulative()
        self.assertEqual(code, 409)
        self.assertEqual(result["state"], "missing_transcript")
        self.assertEqual(result["missing_windows"], [300000, 600000])
        self.assertEqual(result["missing_window_count"], 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transcriptions, [])

    def test_cumulative_zero_silence_and_strict_request(self):
        code, result = self.cumulative(0)
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "empty")
        self.assertEqual(result["total_nodes"], 0)
        self.assertIsNone(result["summary"])
        self.prepare([(0, 300000, [Cue(1000, 2000, "not wholly contained")])])
        self.assertEqual(self.cumulative(1500)[1]["state"], "empty")
        for position in (-1, True, "1000", 750001, 86400001):
            self.assertEqual(self.cumulative(position)[0], 400)
        for extra in ({"retry": True}, {"start_ms": 0}, {"prompt": "override"}):
            self.assertEqual(self.request(self.recap_body(0, **extra), path="/v1/cumulative-recaps")[0], 400)
        self.assertEqual(self.cumulative(0, operator=None)[0], 401)
        self.assertEqual(self.cumulative(0, user=None)[0], 401)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transcriptions, [])
        caps = self.request(path="/v1/capabilities", method="GET")[1]
        self.assertTrue(caps["cumulative_recap_api"])
        self.assertEqual(caps["cumulative_recap_max_position_ms"], 86400000)

    def test_cumulative_provider_failure_preserves_completed_nodes(self):
        self.prepare([
            (0, 300000, [Cue(1000, 2000, "beginning")]),
            (300000, 600000, []),
            (600000, 750000, [Cue(601000, 602000, "recent")]),
        ])
        self.assertEqual(self.cumulative()[0], 202)
        self.failure = "provider_unavailable"
        self.assertEqual(self.cumulative()[0], 502)
        self.failure = None
        self.server.summary_cache = SummaryCache(self.server.config.data_dir / "summaries")
        self.assertEqual(self.cumulative()[1]["completed_nodes"], 2)
        self.assertEqual(self.cumulative()[0], 200)
        self.assertEqual([text for _, text in self.calls], [
            "beginning", "recent", "recent", "Concise summary.\n\nConcise summary."])

    def test_cumulative_revocation_prevents_cache_commit(self):
        self.prepare([(0, 300000, [Cue(1000, 2000, "first")])])
        _, device = self.server.pairing.redeem(self.server.pairing.issue())
        self.release.clear()
        results = []
        worker = threading.Thread(target=lambda: results.append(self.cumulative(10000, operator=device["token"])))
        worker.start()
        try:
            self.assertTrue(self.entered.wait(3))
            self.assertEqual(self.request(self.body(0, 10000)), (429, {"error": "summary_busy"}))
            self.server.pairing.revoke_all()
        finally:
            self.release.set()
            worker.join(timeout=7)
        self.assertEqual(results, [(401, {"error": "unauthorized"})])
        self.assertFalse(self.cumulative(10000)[1]["cache_hit"])
        self.assertEqual(len(self.calls), 2)

    def test_window_math_and_capability(self):
        request = SummaryRequest.parse(self.body(299999, 750000))
        self.assertEqual(request.windows(750000), [(0, 300000), (300000, 600000), (600000, 750000)])
        self.assertTrue(self.request(path="/v1/capabilities", method="GET")[1]["summary_api"])
