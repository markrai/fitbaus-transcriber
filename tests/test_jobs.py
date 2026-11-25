from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from transcriber.audiobookshelf import SourceError
from transcriber.contract import AudioIdentity, Cue, TranscriptWindow
from transcriber.engine import EngineConfig
from transcriber.jobs import JobSpec, JobStore, QueueError, QueueLimits
from transcriber.runner import JobRunner, engine_profile


IDENTITY = AudioIdentity("home", "book", "a" * 64)


class Source:
    identity = IDENTITY
    allowed = True

    def manifest(self, item, token):
        if not self.allowed or token != "authorized":
            raise SourceError("forbidden")
        return SimpleNamespace(identity=self.identity, duration_ms=120000)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = JobStore(self.path, QueueLimits(max_jobs=3, max_active=2, cache_bytes=1024 * 1024))
        self.source = Source()
        self.engine = SimpleNamespace(config=EngineConfig())
        self.calls = 0
        self.runner = JobRunner(self.store, self.source, self.engine, self.path, self.transcribe)

    def transcribe(self, source, identity, token, directory, engine, start, end):
        self.calls += 1
        return TranscriptWindow(identity, start, end, (Cue(start, end, "Narration"),))

    def submit(self, start=0, end=1000):
        return self.runner.submit("book", "authorized", start, end)

    def test_duplicate_and_cache_survive_restart(self):
        first = self.submit()
        self.assertEqual(first.spec.id, self.submit().spec.id)
        self.runner.run(first.spec.id, "authorized")
        reopened = JobStore(self.path)
        runner = JobRunner(reopened, self.source, self.engine, self.path, self.transcribe)
        self.assertEqual(runner.run(first.spec.id, "authorized").state, "ready")
        self.assertEqual(runner.result(first.spec.id, "authorized")["cues"][0]["text"], "Narration")
        self.assertEqual(self.calls, 1)
        self.assertNotIn(b"authorized", (self.path / "jobs.sqlite3").read_bytes())

    def test_recovery_and_exclusive_worker(self):
        job = self.submit()
        with self.store.worker():
            self.store.claim(job.spec.id)
            with self.assertRaisesRegex(QueueError, "worker_busy"):
                with JobStore(self.path).worker():
                    self.fail("Second worker admitted")
        with self.store.worker():
            recovered = self.store.get(job.spec.id)
            self.assertEqual(recovered.state, "queued")
            self.assertEqual(recovered.attempts, 1)
        self.runner.run(job.spec.id, "authorized")
        self.assertEqual(self.store.get(job.spec.id).attempts, 2)

    def test_retry_budget(self):
        job = self.submit()
        for attempt in range(3):
            with self.store.worker():
                self.assertEqual(self.store.claim(job.spec.id).attempts, attempt + 1)
                self.store.fail(job.spec.id, "engine_failed")
        with self.assertRaisesRegex(QueueError, "job_not_runnable"):
            self.runner.run(job.spec.id, "authorized")

    def test_failure_diagnostics_are_sanitized_and_persisted(self):
        from transcriber.engine import EngineError
        job = self.submit()
        def fail(*args):
            raise EngineError("audio_decode_failed", exception_type="CalledProcessError", exit_status=1)
        self.runner.transcribe = fail
        with self.assertLogs("transcriber.diagnostics", level="WARNING") as logs:
            with self.assertRaisesRegex(EngineError, "audio_decode_failed"):
                self.runner.run(job.spec.id, "authorized")
        self.assertEqual(self.store.get(job.spec.id).error, "audio_decode_failed")
        line = logs.output[0]
        for field in ("kind=live", "model=tiny", "start_ms=0", "end_ms=1000",
                      "stage=decode", "code=audio_decode_failed", "exception_type=CalledProcessError",
                      "exit_status=1", "elapsed_ms="):
            self.assertIn(field, line)
        self.assertNotIn("authorized", line)
        self.assertNotIn("Narration", line)

    def test_authorization_and_revision_on_cached_read(self):
        job = self.submit()
        self.runner.run(job.spec.id, "authorized")
        self.source.allowed = False
        with self.assertRaises(SourceError):
            self.runner.result(job.spec.id, "authorized")
        with self.assertRaises(SourceError):
            self.runner.submit("book", "authorized", 0, 1000)
        self.source.allowed = True
        self.source.identity = AudioIdentity("home", "book", "b" * 64)
        with self.assertRaisesRegex(SourceError, "stale_revision"):
            self.runner.result(job.spec.id, "authorized")
        self.assertNotEqual(self.submit().spec.id, job.spec.id)

    def test_resource_gate_rejects_uncached_work_but_preserves_cache_hits(self):
        from transcriber.engine import EngineError
        from transcriber.resources import Resources
        cached = self.submit()
        self.runner.run(cached.spec.id, "authorized")
        blocked = self.submit(1000, 2000)
        runner = JobRunner(self.store, self.source, self.engine, self.path)
        low = Resources(100, 90, 10, 2, 2, "x86_64", "test")
        with patch("transcriber.runner.snapshot", return_value=low), patch("transcriber.runner.supervised_window") as spawn:
            self.assertEqual(runner.run(cached.spec.id, "authorized").state, "ready")
            with self.assertRaisesRegex(EngineError, "resource_unavailable"):
                runner.run(blocked.spec.id, "authorized")
            spawn.assert_not_called()
        self.assertEqual(self.store.get(blocked.spec.id).error, "resource_unavailable")

    def test_profile_part_of_identity(self):
        job = self.submit()
        other = JobSpec(IDENTITY, 0, 1000, engine_profile(EngineConfig(language="en")))
        self.assertNotEqual(job.spec.id, other.id)
        self.engine.config = EngineConfig(language="en")
        with self.assertRaisesRegex(QueueError, "profile_changed"):
            self.runner.run(job.spec.id, "authorized")

    def test_admission_and_eviction(self):
        first = self.submit()
        second = self.submit(1000, 2000)
        with self.assertRaisesRegex(QueueError, "queue_full"):
            self.submit(2000, 3000)
        self.runner.run(first.spec.id, "authorized")
        third = self.submit(2000, 3000)
        self.runner.run(second.spec.id, "authorized")
        fourth = self.submit(3000, 4000)
        with self.assertRaisesRegex(QueueError, "job_not_found"):
            self.store.get(first.spec.id)
        self.assertEqual(self.store.get(third.spec.id).state, "queued")
        self.assertEqual(self.store.get(fourth.spec.id).state, "queued")

    def test_cache_byte_budget(self):
        first = self.submit(0, 100000)
        second = self.submit(10000, 110000)
        def large(job):
            start = job.spec.start_ms
            cues = tuple(Cue(start + i * 100, start + i * 100 + 50, "x" * 1200) for i in range(600))
            return TranscriptWindow(IDENTITY, start, job.spec.end_ms, cues)
        with self.store.worker():
            self.store.claim(first.spec.id)
            self.store.complete(first.spec.id, large(first))
            self.store.claim(second.spec.id)
            self.store.complete(second.spec.id, large(second))
        with self.assertRaisesRegex(QueueError, "job_not_found"):
            self.store.get(first.spec.id)
        self.assertEqual(self.store.get(second.spec.id).state, "ready")

    def test_invalid_result_not_published(self):
        job = self.submit()
        with self.store.worker():
            self.store.claim(job.spec.id)
            with self.assertRaisesRegex(QueueError, "invalid_result"):
                self.store.complete(job.spec.id, TranscriptWindow(IDENTITY, 0, 999, ()))
        self.assertEqual(self.store.get(job.spec.id).state, "running")


if __name__ == "__main__":
    unittest.main()
