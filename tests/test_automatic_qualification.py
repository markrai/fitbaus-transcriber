from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from transcriber.automatic_qualification import AutomaticQualification
from transcriber.contract import AudioIdentity, TranscriptWindow
from transcriber.dispatcher import Dispatcher
from transcriber.engine import EngineConfig, EngineError
from transcriber.jobs import JobSpec, JobStore
from transcriber.qualification import AUTOMATIC_SAMPLE_KIND, load_record, qualify
from transcriber.runner import JobRunner, RunControl, engine_profile
from test_qualification import sample, resources, measured


class AutomaticQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = EngineConfig(language="fr")
        self.audio = self.root / "sample.wav"
        self.audio.write_bytes(sample())
        self.now = 0
        self.memory = resources()
        self.calls = []
        self.identity = AudioIdentity("home", "book", "a" * 64)
        source = SimpleNamespace(manifest=lambda *args: SimpleNamespace(identity=self.identity))
        self.runner = JobRunner(JobStore(self.root / "queue"), source,
                                SimpleNamespace(config=self.config), self.root,
                                transcribe=lambda *args: TranscriptWindow(args[1], args[5], args[6], ()))
        self.manager = AutomaticQualification(self.runner, self.audio, clock=lambda: self.now,
                                              sampler=lambda: self.memory, execute=self.execute,
                                              installed=lambda name: name == "base")
        self.addCleanup(self.manager.close)
        self.model_patch = patch("transcriber.qualification.model_present", return_value=True)
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)

    def execute(self, *args, **kwargs):
        def measure(directory, name, config, raw, fd, timeout, cancelled):
            self.calls.append((name, config.language))
            return measured()
        return qualify(*args, runner=measure, **kwargs)

    def test_automatic_qualifies_once_and_preserves_record_across_restart(self):
        control = RunControl()
        self.assertEqual(self.manager.take(control), "base")
        self.manager.step("base", control)
        record = load_record(self.root, "base", self.config, self.memory)
        self.assertEqual(record["state"], "live_candidate")
        self.assertEqual(record["sample_kind"], AUTOMATIC_SAMPLE_KIND)
        self.assertEqual(record["benchmark_language"], "en")
        self.assertEqual(self.calls, [("base", "en"), ("base", "en")])
        reopened = AutomaticQualification(self.runner, self.audio, sampler=lambda: self.memory,
                                           installed=lambda name: name == "base")
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.take(RunControl()))
        self.assertFalse(reopened.retry("base"))

    def test_waits_for_memory_without_writing_failure(self):
        self.memory = resources(used=1500)
        self.assertIsNone(self.manager.take(RunControl()))
        self.assertEqual(self.manager.inventory()["models"][1]["automatic"]["state"], "waiting_resources")
        self.assertEqual(load_record(self.root, "base", self.config, self.memory)["state"], "unqualified")
        self.memory = resources()
        self.now = 6
        self.assertEqual(self.manager.take(RunControl()), "base")

    def test_live_request_preempts_qualification_without_failed_record(self):
        entered = threading.Event()
        live_done = threading.Event()
        def execute(*args, **kwargs):
            entered.set()
            control = kwargs["control"]
            deadline = time.monotonic() + 5
            while not control.cancelled() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not control.cancelled():
                raise RuntimeError("not preempted")
            raise EngineError("cancelled")
        self.manager.execute = execute
        self.runner.transcribe = lambda *args: (live_done.set() or TranscriptWindow(args[1], args[5], args[6], ()))
        dispatcher = Dispatcher(self.runner, 4, qualification=self.manager)
        self.addCleanup(dispatcher.close)
        self.assertTrue(entered.wait(3))
        job = self.runner.store.submit(JobSpec(self.identity, 0, 1000, engine_profile(self.config)))
        dispatcher.schedule(job, "source-token")
        self.assertTrue(live_done.wait(3))
        self.assertEqual(load_record(self.root, "base", self.config, self.memory)["state"], "unqualified")
        self.assertEqual(self.manager.inventory()["models"][1]["automatic"]["state"], "queued")

    def test_failure_requires_retry_and_success_clears_failure(self):
        self.manager.execute = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid sample"))
        control = RunControl()
        self.assertEqual(self.manager.take(control), "base")
        self.manager.step("base", control)
        self.now = 10
        self.assertIsNone(self.manager.take(RunControl()))
        self.assertEqual(self.manager.inventory()["models"][1]["automatic"]["state"], "failed")
        self.assertTrue(self.manager.retry("base"))
        self.manager.execute = self.execute
        control = RunControl()
        self.assertEqual(self.manager.take(control), "base")
        self.manager.step("base", control)
        self.assertEqual(load_record(self.root, "base", self.config, self.memory)["state"], "live_candidate")

    def test_cancelled_measurement_does_not_replace_record(self):
        control = RunControl()
        def measure(*args, **kwargs):
            control.cancel()
            raise EngineError("cancelled")
        with self.assertRaisesRegex(EngineError, "cancelled"):
            qualify(self.root, "base", self.config, sample(), 0, sampler=lambda: self.memory,
                    runner=measure, control=control, automatic=True)
        self.assertEqual(load_record(self.root, "base", self.config, self.memory)["state"], "unqualified")
