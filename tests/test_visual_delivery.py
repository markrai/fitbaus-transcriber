import base64
from contextlib import contextmanager
from types import SimpleNamespace
import hashlib
import json
import struct
import unittest
import zlib
from unittest.mock import patch

from transcriber.visual_delivery import DeliveryError, canonical, import_delivery, validate_candidate
from transcriber.audiobookshelf import Audiobookshelf, SourceConfig, SourceError
from transcriber.jobs import QueueError
import test_heavy_import


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png():
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + chunk(b"IEND", b""))


class VisualDeliveryTests(unittest.TestCase):
    def setUp(self):
        fixture = test_heavy_import.HeavyImportTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.store = fixture.store
        self.source = fixture.source
        self.source.verify_epub = lambda *args: None
        self.config = fixture.config
        self.job_id = fixture.publish()["job_id"]
        raw = png()
        key = hashlib.sha256(raw).hexdigest()
        transcript = hashlib.sha256(b"".join(canonical(w) + b"\n" for w in fixture.bundle["windows"])).hexdigest()
        self.value = {"schema": "fitbaus.epub-delivery-candidate/v1",
                      "render_policy": "static-rgba-png/v2", "timing_policy": "exact-anchor-bounds/v1",
                      "source_artifact_id": "c" * 64, "edition_verified": False,
                      "render_qualified": False, "playback_qualified": False,
                      "identity": {"source_id": "home", "item_id": "book", "file_id": "42",
                                   "audio_revision": "a" * 64, "profile": fixture.bundle["profile"],
                                   "duration_ms": 301000, "epub_sha256": "b" * 64,
                                   "transcript_sha256": transcript},
                      "summary": {"candidate_count": 1, "unique_png_count": 1,
                                  "withheld_count": 0, "withheld_reasons": {}},
                      "pngs": [{"png_sha256": key, "width": 1, "height": 1, "size": len(raw),
                                "payload_base64": base64.b64encode(raw).decode("ascii")}],
                      "occurrences": [{"occurrence_index": 0, "source_asset_id": "d" * 64,
                                       "png_sha256": key, "earliest_ms": 0, "latest_ms": 1000,
                                       "method": "within_exact_cue"}]}

    def stage(self):
        raw = canonical(self.value)
        return import_delivery(self.store, self.source, self.config, "token", raw,
                               hashlib.sha256(raw).hexdigest())

    def stored(self):
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM visual_candidates WHERE parent_id=?", (self.job_id,)).fetchone()
            return None if row is None else json.loads(row["payload"])

    def test_stage_idempotent_and_reimport_invalidates(self):
        with patch.object(self.source, "verify_epub") as verify:
            result = self.stage()
            verify.assert_called_once_with("book", "42", "b" * 64, "token")
        self.assertEqual(result["state"], "staged")
        self.assertFalse(result["playback_qualified"])
        self.assertEqual(self.stored(), self.value)
        self.assertTrue(self.stage()["reused"])
        self.fixture.publish()
        self.assertIsNone(self.stored())

    def test_invalid_replacement_preserves_previous(self):
        self.stage()
        old = self.stored()
        self.value["occurrences"][0]["latest_ms"] = 999
        with self.assertRaisesRegex(DeliveryError, "transcript"):
            self.stage()
        self.assertEqual(self.stored(), old)
        self.value["occurrences"][0]["latest_ms"] = 1000
        self.value["identity"]["transcript_sha256"] = "e" * 64
        with self.assertRaisesRegex(DeliveryError, "transcript"):
            self.stage()
        self.assertEqual(self.stored(), old)

    def test_invalid_source_and_storage_quota(self):
        with patch.object(self.source, "verify_epub", side_effect=SourceError("stale_revision")):
            with self.assertRaises(SourceError):
                self.stage()
        self.assertIsNone(self.stored())
        with patch("transcriber.visual_delivery.MAX_STORE_BYTES", 1), self.assertRaises(QueueError):
            self.stage()
        self.assertIsNone(self.stored())

    def test_strict_candidate_validation(self):
        raw = canonical(self.value)
        with self.assertRaises(DeliveryError):
            validate_candidate(raw, "f" * 64, self.config)
        for target, key, changed in (
                (self.value, "playback_qualified", True),
                (self.value["pngs"][0], "width", True),
                (self.value["pngs"][0], "payload_base64", "AAAA"),
                (self.value["occurrences"][0], "method", "invented"),
                (self.value["summary"], "candidate_count", 2)):
            previous = target[key]
            target[key] = changed
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.stage()
            target[key] = previous

    def test_incomplete_transcript_is_not_accepted(self):
        with self.store.transaction() as db:
            db.execute("DELETE FROM windows WHERE parent_id=? AND start_ms=300000", (self.job_id,))
        with self.assertRaises(DeliveryError):
            self.stage()
        self.assertIsNone(self.stored())


class CompanionVerificationTests(unittest.TestCase):
    def test_streams_only_selected_epub_and_rechecks_metadata(self):
        source = Audiobookshelf(SourceConfig("home", "https://example.test"))
        payload = b"example EPUB bytes"
        calls = []
        row = {"ino": "42", "fileType": "ebook",
               "metadata": {"filename": "Book.epub", "ext": ".epub", "size": len(payload), "mtimeMs": 1}}
        item = {"id": "book", "mediaType": "book", "media": {"ebookFile": row},
                "libraryFiles": [row]}

        @contextmanager
        def get(path, token):
            calls.append((path, token))
            data = json.dumps(item).encode() if path.endswith("?expanded=1") else payload
            headers = SimpleNamespace(get_content_type=lambda: "application/json",
                                      get=lambda key: str(len(data)) if key == "Content-Length" else None)
            yield SimpleNamespace(headers=headers, data=data)

        with patch.object(source, "_get", side_effect=get), \
                patch.object(source, "_chunks", side_effect=lambda response, *args: [response.data]):
            source.verify_epub("book", "42", hashlib.sha256(payload).hexdigest(), "token")
            self.assertEqual(calls, [("/api/items/book?expanded=1", "token"),
                                     ("/api/items/book/file/42", "token"),
                                     ("/api/items/book?expanded=1", "token")])
            with self.assertRaisesRegex(SourceError, "stale_revision"):
                source.verify_epub("book", "42", "a" * 64, "token")
            with self.assertRaisesRegex(SourceError, "not_found"):
                source.verify_epub("book", "43", "a" * 64, "token")


if __name__ == "__main__":
    unittest.main()
