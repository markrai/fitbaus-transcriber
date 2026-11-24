import hashlib
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from transcriber.audiobookshelf import SourceError, SourceConfig, Track, Manifest
from transcriber.background import Background, BackgroundStore, profile
from transcriber.contract import AudioIdentity, Cue, TranscriptWindow
from transcriber.dispatcher import Dispatcher
from transcriber.engine import EngineConfig, EngineError
from transcriber.jobs import JobStore, JobSpec, QueueError
from transcriber.runner import JobRunner, RunControl, engine_profile


IDENTITY = AudioIdentity("library", "book", "a" * 64)
TOKEN = "test-audiobook-token"


class Source:
    def __init__(self):
        self.identity = IDENTITY
        self.config = SourceConfig("library", "http://localhost:1234")

    def manifest(self, item, token):
        if token != TOKEN:
            raise SourceError("forbidden")
        return Manifest(self.identity, (Track("track", 0, 600001, 12, 0, 0),))


def window(identity, start, end):
    return TranscriptWindow(identity, start, end, (Cue(start, end, "test narration"),))


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = EngineConfig()
        self.source = Source()
        self.live = JobStore(self.root / "queue")
        self.runner = JobRunner(self.live, self.source, SimpleNamespace(config=self.config),
                                self.root, transcribe=lambda *args: window(args[1], args[5], args[6]))
        self.store = BackgroundStore(self.root / "background")
        self.now = 0
        self.calls = []
        self.background = Background(self.runner, self.store, clock=lambda: self.now, execute=self.execute)
        self.background.check_model = lambda *args, **kwargs: None
        self.addCleanup(self.background.close)

    def execute(self, source, identity, token, directory, engine, start, end, cancelled, fd):
        self.calls.append((engine.model_name, start, end))
        return window(identity, start, end)

    def step(self):
        control = RunControl()
        job, token = self.background.take(control)
        self.background.step(job, token, control)

    def test_lazy_checkpoint_restart_and_complete(self):
        initial = self.background.submit(IDENTITY, "base", TOKEN)
        self.assertEqual(initial["state"], "queued")
        job_id = initial["job_id"]
        self.step()
        self.assertEqual(self.store.get(job_id).cursor_ms, 300000)
        reopened = BackgroundStore(self.root / "background")
        other = Background(self.runner, reopened, execute=self.execute)
        other.check_model = lambda *args, **kwargs: None
        self.addCleanup(other.close)
        self.background.close()
        self.assertEqual(other.status(reopened.get(job_id))["state"], "waiting_credentials")
        self.assertIsNone(other.take(RunControl()))
        other.action(job_id, TOKEN, "resume")
        for _ in range(2):
            control = RunControl()
            job, token = other.take(control)
            other.step(job, token, control)
        self.assertEqual(reopened.get(job_id).state, "completed")
        self.assertEqual(self.calls, [("base", 0, 300000), ("base", 300000, 600000), ("base", 600000, 600001)])
        self.assertNotIn(TOKEN.encode(), reopened.path.read_bytes())

    def test_pause_cancel_discard_and_expiry(self):
        job = self.background.submit(IDENTITY, "tiny", TOKEN)
        job_id = job["job_id"]
        self.step()
        self.now = 601
        self.background.expire()
        self.assertEqual(self.background.status(self.store.get(job_id))["state"], "waiting_credentials")
        self.assertEqual(self.background.action(job_id, TOKEN, "resume")["state"], "queued")
        self.assertEqual(self.background.action(job_id, TOKEN, "pause")["state"], "paused")
        self.assertIsNone(self.background.take(RunControl()))
        self.assertEqual(self.background.action(job_id, TOKEN, "resume")["next_ms"], 300000)
        self.assertEqual(self.background.action(job_id, TOKEN, "cancel")["state"], "cancelled")
        self.assertEqual(self.background.action(job_id, TOKEN, "resume")["state"], "cancelled")
        self.background.action(job_id, TOKEN, "discard")
        with self.assertRaisesRegex(QueueError, "background_not_found"):
            self.store.get(job_id)

    def test_authorization_revision_and_quota(self):
        with self.assertRaises(SourceError):
            self.background.submit(IDENTITY, "tiny", "denied")
        job_id = self.background.submit(IDENTITY, "tiny", TOKEN)["job_id"]
        with self.assertRaises(SourceError):
            self.background.action(job_id, "denied", "pause")
        self.source.identity = AudioIdentity("library", "book", "b" * 64)
        self.step()
        self.assertEqual(self.store.get(job_id).state, "failed")
        self.assertEqual(self.store.get(job_id).error, "stale_revision")
        limited = BackgroundStore(self.root / "limited", max_parents=1, max_bytes=1)
        job = limited.submit(IDENTITY, "tiny", profile("tiny", self.config), 300000)
        with self.assertRaisesRegex(QueueError, "background_storage_full"):
            limited.checkpoint(job, window(IDENTITY, 0, 300000))
        self.assertEqual(limited.get(job.id).cursor_ms, 0)
        with self.assertRaisesRegex(QueueError, "background_full"):
            limited.submit(IDENTITY, "base", profile("base", self.config), 300000)

    def test_live_preempts_and_background_resumes_without_checkpoint_loss(self):
        entered = threading.Event()
        live_entered = threading.Event()
        finished = threading.Event()
        order = []

        def background_execute(source, identity, token, directory, engine, start, end, cancelled, fd):
            order.append("background")
            if len(order) == 1:
                entered.set()
                deadline = time.monotonic() + 5
                while not cancelled() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not cancelled():
                    raise RuntimeError("preemption not delivered")
                raise EngineError("cancelled")
            finished.set()
            return window(identity, start, end)

        def live_execute(*args):
            order.append("live")
            live_entered.set()
            return window(args[1], args[5], args[6])

        self.background.execute = background_execute
        self.runner.transcribe = live_execute
        job_id = self.background.submit(IDENTITY, "small", TOKEN)["job_id"]
        dispatcher = Dispatcher(self.runner, 8, background=self.background)
        self.addCleanup(dispatcher.close)
        self.assertTrue(entered.wait(3))
        job = self.live.submit(JobSpec(IDENTITY, 0, 1000, engine_profile(self.config)))
        dispatcher.schedule(job, TOKEN)
        self.assertTrue(live_entered.wait(3))
        self.assertTrue(finished.wait(3))
        self.assertEqual(order[:3], ["background", "live", "background"])
        deadline = time.monotonic() + 3
        while self.store.get(job_id).state != "completed" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.store.get(job_id).cursor_ms, 600001)
        self.assertEqual(self.live.get(job.spec.id).state, "ready")

    def test_existing_checkpoint_migrates_without_regeneration(self):
        job_id = self.background.submit(IDENTITY, "small", TOKEN)["job_id"]
        self.step()
        with sqlite3.connect(self.store.path) as db:
            db.execute("DROP INDEX prepared_window_id")
            db.execute("ALTER TABLE windows DROP COLUMN prepared_id")
        reopened = BackgroundStore(self.root / "background")
        spec = JobSpec(IDENTITY, 0, 300000, engine_profile(self.config))
        prepared = reopened.find_prepared(spec, self.config)
        self.assertEqual(prepared.state, "ready")
        self.assertEqual(prepared.spec.profile, profile("small", self.config))
        self.assertEqual(reopened.get_prepared(prepared.spec.id), prepared)
        self.assertEqual(reopened.get(job_id).cursor_ms, 300000)
        self.assertIsNone(reopened.find_prepared(spec, EngineConfig(language="fr")))
        self.assertEqual(len(self.calls), 1)

    def test_unattended_renews_access_once_and_keeps_checkpoint(self):
        original_manifest = self.source.manifest
        job_id = self.background.submit(IDENTITY, "small", TOKEN, unattended=True,
                                        refresh="refresh-one")["job_id"]
        self.step()
        self.now = 601
        self.background.expire()
        self.assertIn(job_id, self.background.pending)
        def manifest(item, token):
            if token == TOKEN:
                raise SourceError("unauthorized")
            if token != "new-access":
                raise SourceError("forbidden")
            return original_manifest(item, TOKEN)
        self.source.manifest = manifest
        self.source.refresh_session = lambda refresh: ("new-access", "refresh-two")
        self.step()
        self.assertEqual(self.store.get(job_id).cursor_ms, 300000)
        self.assertEqual(self.background.pending[job_id][:3], ("new-access", 86400, "refresh-two"))
        self.step()
        self.assertEqual(self.store.get(job_id).cursor_ms, 600000)
        for secret in (TOKEN, "new-access", "refresh-one", "refresh-two"):
            self.assertNotIn(secret.encode(), self.store.path.read_bytes())
        self.now = 86400
        self.background.expire()
        self.assertEqual(self.background.status(self.store.get(job_id))["state"], "waiting_credentials")

    def test_refresh_rejection_requires_fresh_login(self):
        job_id = self.background.submit(IDENTITY, "small", TOKEN, unattended=True,
                                        refresh="refresh-one")["job_id"]
        self.step()
        def expired(*args):
            raise SourceError("unauthorized")
        def rejected(*args):
            raise SourceError("login_rejected")
        self.source.manifest = expired
        self.source.refresh_session = rejected
        self.step()
        self.assertEqual(self.store.get(job_id).cursor_ms, 300000)
        self.assertEqual(self.background.status(self.store.get(job_id))["state"], "waiting_credentials")
        self.assertNotIn(job_id, self.background.pending)

    def test_track_spool_is_anonymous_reused_and_credential_scoped(self):
        job_id = self.background.submit(IDENTITY, "tiny", TOKEN)["job_id"]
        job = self.store.get(job_id)
        manifest = self.source.manifest("book", TOKEN)
        with patch("transcriber.background.shutil.disk_usage", return_value=SimpleNamespace(free=10**9)):
            first = self.background.prepare_spool(job, manifest, 300000, TOKEN)
            self.assertFalse(first[2])
            self.background.spool.write(b"verified")
            self.background.spool_ready = True
            second = self.background.prepare_spool(job, manifest, 300000, TOKEN)
            self.assertEqual(first[0], second[0])
            self.assertTrue(second[2])
            third = self.background.prepare_spool(job, manifest, 300000, "another-token")
            self.assertFalse(third[2])
            self.background.spool.seek(0)
            self.assertEqual(self.background.spool.read(), b"")
        self.background.close()
        self.assertIsNone(self.background.spool)
