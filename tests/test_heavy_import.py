import json
import tempfile
import unittest
from types import SimpleNamespace

from transcriber.background import BackgroundStore, heavy_profile, profile
from transcriber.contract import AudioIdentity, Cue, TranscriptWindow
from transcriber.engine import EngineConfig
from transcriber.heavy_import import BUNDLE_SCHEMA, import_heavy
from transcriber.jobs import JobSpec, QueueError
from transcriber.audiobookshelf import SourceError


class HeavyImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = BackgroundStore(self.temp.name)
        self.config = EngineConfig()
        self.identity = AudioIdentity("home", "book", "a" * 64)
        self.manifest = SimpleNamespace(identity=self.identity, duration_ms=301000)
        self.source = SimpleNamespace(manifest=lambda item, token: self.manifest)
        self.bundle = {
            "schema": BUNDLE_SCHEMA, "source_id": "home", "item_id": "book",
            "audio_revision": "a" * 64, "duration_ms": 301000,
            "profile": heavy_profile(self.config),
            "windows": [TranscriptWindow(self.identity, 0, 300000, (Cue(0, 1000, "Large"),)).to_json(),
                        TranscriptWindow(self.identity, 300000, 301000, ()).to_json()],
        }

    def publish(self):
        return import_heavy(self.store, self.source, self.config, "token", json.dumps(self.bundle))

    def read(self):
        return self.store.find_prepared(JobSpec(self.identity, 0, 300000, profile("tiny", self.config)), self.config)

    def test_prefer_heavy_without_exposing_unknown_model_to_old_clients(self):
        job = self.store.submit(self.identity, "small", profile("small", self.config), 301000)
        self.store.checkpoint(job, TranscriptWindow(self.identity, 0, 300000, (Cue(0, 1000, "Small"),)))
        self.assertEqual(json.loads(self.read().result)["cues"][0]["text"], "Small")
        imported = self.publish()
        self.assertEqual(imported["state"], "completed")
        selected = self.read()
        self.assertEqual(selected.spec.profile, heavy_profile(self.config))
        self.assertEqual(json.loads(selected.result)["cues"][0]["text"], "Large")
        self.assertEqual(self.store.get_prepared(selected.spec.id), selected)
        self.assertEqual([job.model for job in self.store.for_item("home", "book")], ["small"])
        self.assertEqual(self.publish()["job_id"], imported["job_id"])

    def test_invalid_replacement_preserves_old_transcript(self):
        self.publish()
        self.bundle["windows"].pop()
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(json.loads(self.read().result)["cues"][0]["text"], "Large")

    def test_stale_revision_and_wrong_profile_are_rejected(self):
        self.manifest.identity = AudioIdentity("home", "book", "b" * 64)
        with self.assertRaises(SourceError):
            self.publish()
        self.manifest.identity = self.identity
        self.bundle["profile"] = profile("small", self.config)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertIsNone(self.read())

    def test_replacement_over_quota_rolls_back(self):
        self.publish()
        self.store.max_bytes = 1
        with self.assertRaises(QueueError):
            self.publish()
        self.assertEqual(json.loads(self.read().result)["cues"][0]["text"], "Large")
