import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from transcriber.engine import EngineConfig
from transcriber.qualification import classify, inventory, load_record, parse_sample, qualify
from transcriber.resources import MIB, Resources, admission, fingerprint, required_bytes, snapshot


def sample(seconds=300):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(seconds * 32000))
    return output.getvalue()


def resources(used=128, limit=2048, quota=2):
    return Resources(limit * MIB, used * MIB, 8192 * MIB, quota, 6, "x86_64", "test CPU")


def measured(seconds=20, rss=600):
    return {"load_seconds": 0.5, "inference_seconds": seconds - 1, "worker_seconds": seconds,
            "peak_rss_bytes": rss * MIB, "cue_count": 60, "word_count": 700, "cue_coverage_ms": 240000}


class QualificationTests(unittest.TestCase):
    def test_memory_admission_and_measured_headroom(self):
        self.assertTrue(admission("tiny", resources())["allowed"])
        self.assertTrue(admission("small", resources())["allowed"])
        self.assertFalse(admission("small", resources(used=600))["allowed"])
        self.assertFalse(admission("base", resources(), 1600 * MIB)["allowed"])
        self.assertEqual(required_bytes("tiny"), 896 * MIB)
        unknown = Resources(None, None, None, None, 1, "x86_64", "test")
        self.assertEqual(admission("tiny", unknown)["reason"], "memory_unknown")

    def test_cgroup_snapshot_uses_reclaimable_file_cache_and_host_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cg = root / "cg"
            proc = root / "proc"
            cg.mkdir()
            proc.mkdir()
            for name, value in {"memory.max": str(2048*MIB), "memory.current": str(512*MIB),
                                "memory.stat": f"inactive_file {256*MIB}", "cpu.max": "200000 100000"}.items():
                (cg / name).write_text(value)
            (proc / "meminfo").write_text("MemAvailable: 1048576 kB\n")
            (proc / "cpuinfo").write_text("model name: test CPU\n")
            result = snapshot(cg, proc)
            self.assertEqual(result.memory_used, 256*MIB)
            self.assertEqual(result.available, 1024*MIB)
            self.assertEqual(result.cpu_quota, 2)

    def test_fingerprint_invalidates_on_model_configuration_or_limit_change(self):
        config = EngineConfig()
        first = fingerprint("tiny", config, resources())
        self.assertEqual(first, fingerprint("tiny", config, resources(used=256)))
        self.assertNotEqual(first, fingerprint("base", config, resources()))
        self.assertNotEqual(first, fingerprint("tiny", EngineConfig(cpu_threads=3), resources()))
        self.assertNotEqual(first, fingerprint("tiny", config, resources(quota=1)))
        self.assertNotEqual(first, fingerprint("tiny", config, resources(limit=4096)))

    def test_sample_bounds_and_probe(self):
        raw = sample(2)
        self.assertEqual(parse_sample(raw, probe=True)[1], 2000)
        with self.assertRaises(ValueError):
            parse_sample(raw)
        with self.assertRaises(ValueError):
            parse_sample(raw[:-1], probe=True)
        self.assertEqual(parse_sample(sample())[1], 300000)

    def test_classification_requires_speech_and_two_fresh_runs(self):
        self.assertEqual(classify([measured(), measured(59)], 300000), "live_candidate")
        self.assertEqual(classify([measured(), measured(61)], 300000), "background_candidate")
        self.assertEqual(classify([measured(), measured(301)], 300000), "slow")
        self.assertEqual(classify([measured()], 300000), "unqualified")
        self.assertEqual(classify([measured(), {**measured(), "word_count": 0}], 300000), "insufficient_speech")
        self.assertEqual(classify([measured()], 2000, True), "unqualified")

    def test_qualification_persistence_probe_and_failed_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = EngineConfig()
            calls = []
            def runner(*args):
                calls.append(args[1])
                return measured()
            with patch("transcriber.qualification.model_present", return_value=True):
                value = qualify(path, "base", config, sample(), 0, sampler=resources, runner=runner)
                self.assertEqual(value["state"], "live_candidate")
                self.assertEqual(calls, ["base", "base"])
                self.assertEqual(load_record(path, "base", config, resources()), value)
                self.assertEqual(load_record(path, "base", config, resources(quota=1)), {"state": "stale"})
                qualify(path, "base", config, sample(2), 0, probe=True, sampler=resources, runner=runner)
                self.assertEqual(load_record(path, "base", config, resources()), value)
                failed = qualify(path, "small", config, sample(), 0, sampler=lambda: resources(used=1500), runner=runner)
                self.assertEqual(failed["error"], "insufficient_memory")
                self.assertEqual(calls, ["base", "base", "base"])
                self.assertEqual(inventory(path, config, resources())["models"][1]["qualification"], value)
            with patch("transcriber.qualification.model_present", return_value=False):
                self.assertEqual(inventory(path, config, resources())["models"][1]["qualification"]["state"], "not_installed")
