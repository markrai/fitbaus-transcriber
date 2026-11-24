import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from transcriber.engine import EngineError
from transcriber.inference_process import supervise
from transcriber.jobs import QueueError


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.TemporaryFile()
        self.addCleanup(self.file.close)
        self.payload = {
            "identity": {"source_id": "home", "item_id": "book", "audio_revision": "a" * 64},
            "start_ms": 0, "end_ms": 1000, "token": "test-only-credential"
        }

    def run_code(self, code, timeout=3, cancelled=lambda: False):
        return supervise(self.payload, timeout, cancelled, self.file.fileno(),
                         command=[sys.executable, "-c", code])

    def valid_code(self):
        return """import json, sys
p = json.load(sys.stdin)
print(json.dumps({"window": {"schema": "fitbaus.transcript/v1", **p["identity"],
    "coverage": {"start_ms": p["start_ms"], "end_ms": p["end_ms"]},
    "cues": [{"start_ms": 0, "end_ms": 1000, "text": "Narration"}]}}))
"""

    def test_success_and_identity_validation(self):
        result = self.run_code(self.valid_code())
        self.assertEqual(result.cues[0].text, "Narration")
        with self.assertRaisesRegex(EngineError, "invalid_worker_result"):
            self.run_code(self.valid_code().replace('**p["identity"]', '**{**p["identity"], "item_id": "other"}'))

    def test_large_request_with_delayed_reader(self):
        self.payload["token"] = "x" * 8192
        code = "import time; time.sleep(0.25)\n" + self.valid_code().replace(
            "p = json.load(sys.stdin)", 'p = json.load(sys.stdin)\nassert p["token"] == "x" * 8192')
        self.assertEqual(self.run_code(code).cues[0].text, "Narration")

    def test_timeout_then_next_window_succeeds(self):
        with self.assertRaisesRegex(EngineError, "inference_timeout"):
            self.run_code("import time; time.sleep(60)", timeout=0.2)
        self.assertEqual(self.run_code(self.valid_code()).cues[0].text, "Narration")

    def test_cancel_then_next_window_succeeds(self):
        event = threading.Event()
        timer = threading.Timer(0.2, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaisesRegex(EngineError, "cancelled"):
            self.run_code("import time; time.sleep(60)", cancelled=event.is_set)
        self.assertEqual(self.run_code(self.valid_code()).cues[0].text, "Narration")

    def test_crash_malformed_and_oversized_output(self):
        with self.assertRaisesRegex(EngineError, "worker_crashed"):
            self.run_code("import os; os._exit(7)")
        with self.assertRaisesRegex(EngineError, "invalid_worker_result"):
            self.run_code("print('not-json')")
        with self.assertRaisesRegex(QueueError, "result_limit"):
            self.run_code("print('x' * (1024 * 1024 + 1025))")

    def test_credentials_not_in_arguments_or_environment(self):
        code = """import json, os, sys
p = json.load(sys.stdin)
assert p["token"] not in " ".join(sys.argv)
assert "TRANSCRIBER_TOKEN" not in os.environ
assert "HF_TOKEN" not in os.environ
"""
        with patch.dict(os.environ, {"TRANSCRIBER_TOKEN": "secret", "HF_TOKEN": "secret"}):
            self.assertEqual(self.run_code(code + self.valid_code().replace("p = json.load(sys.stdin)", "")).cues[0].text, "Narration")
