from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from transcriber.contract import AudioIdentity
from transcriber.summaries import SummaryRequest
from transcriber.summary_cache import SummaryCache, SummaryCacheError, cache_key
from transcriber.summary_provider import SummaryProviderConfig


class SummaryCacheTests(unittest.TestCase):
    def test_key_is_sensitive_to_content_and_inference_identity(self):
        request = SummaryRequest(AudioIdentity("home", "book", "a" * 64), 0, 1000)
        config = SummaryProviderConfig("https://example.test/v1", "model", "secret")
        def key(req=request, cfg=config, source="https://books.test", digest="b" * 64,
                version="section-v1", prompt="prompt", tokens=1024):
            return cache_key(req, source, cfg, digest, version, prompt, tokens)
        baseline = key()
        for other in (
            key(req=replace(request, identity=AudioIdentity("other", "book", "a" * 64))),
            key(req=replace(request, identity=AudioIdentity("home", "other", "a" * 64))),
            key(req=replace(request, identity=AudioIdentity("home", "book", "c" * 64))),
            key(req=replace(request, start_ms=1)), key(req=replace(request, end_ms=999)),
            key(cfg=replace(config, model="other")), key(cfg=replace(config, base_url="https://other.test/v1")),
            key(cfg=replace(config, cache_epoch="2")), key(source="https://other-books.test"),
            key(digest="d" * 64), key(version="section-v2"), key(prompt="new"), key(tokens=512),
        ):
            self.assertNotEqual(baseline, other)
        self.assertEqual(baseline, key(req=replace(request, retry=True)))
        self.assertEqual(baseline, key(cfg=replace(config, api_key="rotated", timeout=90)))

    def test_persistence_lru_and_byte_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b, c, d = (letter * 64 for letter in "abcd")
            cache = SummaryCache(directory, max_entries=2, max_bytes=12)
            self.assertIsNone(cache.get(a))
            cache.put(a, "first")
            cache.put(b, "second")
            self.assertEqual(cache.get(a), "first")
            cache = SummaryCache(directory, max_entries=2, max_bytes=12)
            cache.put(c, "third")
            self.assertEqual(cache.get(a), "first")
            self.assertIsNone(cache.get(b))
            self.assertEqual(cache.get(c), "third")
            cache.put(d, "é" * 5)
            self.assertIsNone(cache.get(a))
            self.assertIsNone(cache.get(c))
            self.assertEqual(cache.get(d), "é" * 5)
            self.assertFalse(cache.put(a, "x" * 13))
            self.assertEqual(cache.get(d), "é" * 5)
            self.assertEqual(os.stat(cache.path).st_mode & 0o777, 0o600)

    def test_replacement_and_entry_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SummaryCache(directory, max_entries=1, max_bytes=100)
            a, b = "a" * 64, "b" * 64
            cache.put(a, "one")
            cache.put(a, "replacement")
            self.assertEqual(cache.get(a), "replacement")
            cache.put(b, "two")
            self.assertIsNone(cache.get(a))
            self.assertEqual(cache.get(b), "two")

    def test_storage_failure_and_corrupt_value_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            a = "a" * 64
            path = Path(directory)
            (path / "file").write_text("not a directory")
            with self.assertRaisesRegex(SummaryCacheError, "^summary_cache_unavailable$"):
                SummaryCache(path / "file").get(a)
            cache = SummaryCache(path / "cache")
            cache.put(a, "one")
            with sqlite3.connect(cache.path) as db:
                db.execute("UPDATE summaries SET size=99")
            with self.assertRaises(SummaryCacheError):
                cache.get(a)
            with self.assertRaises(ValueError):
                cache.put(a, "\ud800")
