from contextlib import contextmanager
import io
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
import wave

from transcriber.audiobookshelf import Manifest, SourceError, Track
from transcriber.contract import AudioIdentity, Cue, MAX_CUES, MAX_CUE_CHARS, TranscriptWindow
from transcriber.engine import EngineConfig, EngineError, WhisperCpuEngine, decode_pcm, normalize_segments, transcribe_window


IDENTITY = AudioIdentity("home", "book-1", "a" * 64)


class FakeSource:
    def __init__(self):
        self.current = Manifest(IDENTITY, (
            Track("1", 0, 60000, 100, 1, 1),
            Track("2", 60000, 60000, 100, 1, 1),
        ))
        self.materialized = []
        self.change_after_decode = False

    def manifest(self, item, token):
        if token != "authorized":
            raise SourceError("forbidden")
        return self.current

    @contextmanager
    def materialize_track(self, identity, file_id, token, directory):
        self.materialized.append(file_id)
        yield io.BytesIO(file_id.encode())
        if self.change_after_decode:
            self.current = Manifest(AudioIdentity("home", "book-1", "b" * 64), self.current.tracks)


class FakeEngine:
    config = EngineConfig()

    def transcribe(self, pcm, start, end):
        return (Cue(start, end, "Track " + pcm.decode()),)


class EngineTests(unittest.TestCase):
    def test_segments_are_absolute_clamped_and_nonoverlapping(self):
        segments = [
            SimpleNamespace(start=-0.1, end=1.5, text="  First  phrase "),
            SimpleNamespace(start=1.3, end=3.0, text="Second phrase"),
            SimpleNamespace(start=3.1, end=3.4, text=" "),
            SimpleNamespace(start=3.4, end=5.0, text="Last"),
        ]
        self.assertEqual(normalize_segments(segments, 60000, 64000), (
            Cue(60000, 61500, "First phrase"),
            Cue(61500, 63000, "Second phrase"),
            Cue(63400, 64000, "Last"),
        ))

    def test_invalid_model_timestamp(self):
        with self.assertRaisesRegex(EngineError, "invalid_model_timestamp"):
            normalize_segments([SimpleNamespace(start=float("nan"), end=1, text="x")], 0, 1000)

    def test_partial_window_preserves_valid_cues_at_book_relative_offset(self):
        segments = [
            SimpleNamespace(start=-0.25, end=1.5, text="First"),
            SimpleNamespace(start=1.5, end=302.5, text="Outside"),
            SimpleNamespace(start=1.4, end=3, text="Second"),
            SimpleNamespace(start=float("inf"), end=4, text="Infinite"),
            SimpleNamespace(start=4, end=3, text="Reversed"),
            SimpleNamespace(start=5, end=6, text="Third"),
            SimpleNamespace(start=299, end=300.75, text="Last"),
        ]
        with self.assertLogs("transcriber.diagnostics", level="WARNING") as logs:
            cues = normalize_segments(segments, 1_800_000, 2_100_000)
        self.assertEqual(cues, (
            Cue(1_800_000, 1_801_500, "First"),
            Cue(1_801_500, 1_803_000, "Second"),
            Cue(1_805_000, 1_806_000, "Third"),
            Cue(2_099_000, 2_100_000, "Last"),
        ))
        self.assertEqual(TranscriptWindow(IDENTITY, 1_800_000, 2_100_000, cues).cues, cues)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("accepted=4 discarded=3 invalid=3 clamped=2", logs.output[0])
        self.assertIn("first_invalid_end_s=302.500000", logs.output[0])
        self.assertNotIn("Outside", logs.output[0])

    def test_actual_pcm_duration_controls_clipping_and_rejection(self):
        segments = [
            SimpleNamespace(start=0, end=1, text="First"),
            SimpleNamespace(start=1, end=8, text="Outside decoded audio"),
            SimpleNamespace(start=1, end=3.5, text="Last"),
        ]
        with self.assertLogs("transcriber.diagnostics", level="WARNING") as logs:
            cues = normalize_segments(segments, 60_000, 70_000, audio_duration_ms=3_000)
        self.assertEqual(cues, (Cue(60_000, 61_000, "First"), Cue(61_000, 63_000, "Last")))
        self.assertIn("audio_duration_ms=3000", logs.output[0])

    def test_invalid_types_and_nonfinite_values_are_safe_and_do_not_leak(self):
        invalid = [True, None, "private\ncredential", float("nan"), float("inf"),
                   float("-inf"), 10 ** 1000, -1.01]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                segments = [
                    SimpleNamespace(start=value, end=1, text="Private narration"),
                    SimpleNamespace(start=1, end=2, text="Valid"),
                ]
                with self.assertLogs("transcriber.diagnostics", level="WARNING") as logs:
                    cues = normalize_segments(segments, 0, 3000)
                self.assertEqual(cues, (Cue(1000, 2000, "Valid"),))
                self.assertNotIn("credential", logs.output[0])
                self.assertNotIn("Private narration", logs.output[0])

    def test_all_invalid_nonempty_segments_fail_but_silence_remains_valid(self):
        with self.assertLogs("transcriber.diagnostics", level="WARNING"):
            with self.assertRaisesRegex(EngineError, "invalid_model_timestamp"):
                normalize_segments([
                    SimpleNamespace(start=1800, end=1801, text="Absolute timestamp"),
                    SimpleNamespace(start=0, end=float("nan"), text="Invalid"),
                ], 1_800_000, 2_100_000)
        self.assertEqual(normalize_segments([], 0, 1000), ())
        self.assertEqual(normalize_segments([SimpleNamespace(start=0, end=1, text=" ")], 0, 1000), ())

    def test_resource_and_text_limits_remain_strict(self):
        with self.assertRaisesRegex(EngineError, "model_cue_limit"):
            normalize_segments((SimpleNamespace(start=0, end=0, text="") for _ in range(MAX_CUES + 1)), 0, 1000)
        with self.assertRaisesRegex(EngineError, "model_text_limit"):
            normalize_segments([SimpleNamespace(start=0, end=1, text="x" * (MAX_CUE_CHARS + 1))], 0, 1000)
        for duration in (-1, 1001, True):
            with self.assertRaises(ValueError):
                normalize_segments([], 0, 1000, audio_duration_ms=duration)

    def test_cross_track_window(self):
        source = FakeSource()
        decoded = []
        def decoder(audio, start, end, timeout):
            data = audio.read()
            decoded.append((data, start, end))
            return data
        window = transcribe_window(source, IDENTITY, "authorized", Path("/unused"), FakeEngine(),
                                   55000, 65000, decoder)
        self.assertEqual(decoded, [(b"1", 55000, 60000), (b"2", 0, 5000)])
        self.assertEqual(window.cues, (Cue(55000, 60000, "Track 1"), Cue(60000, 65000, "Track 2")))

    def test_stale_or_denied_audio_never_yields_window(self):
        source = FakeSource()
        with self.assertRaises(SourceError):
            transcribe_window(source, IDENTITY, "denied", Path("/unused"), FakeEngine(), 0, 1000)
        self.assertEqual(source.materialized, [])
        source.change_after_decode = True
        with self.assertRaisesRegex(SourceError, "stale_revision"):
            transcribe_window(source, IDENTITY, "authorized", Path("/unused"), FakeEngine(), 0, 1000,
                              lambda file, start, end, timeout: file.read())

    def test_window_limits_and_missing_model(self):
        source = FakeSource()
        for start, end in ((-1, 1), (10, 10), (0, 300001), (0, 120001)):
            with self.assertRaises(ValueError):
                transcribe_window(source, IDENTITY, "authorized", Path("/unused"), FakeEngine(), start, end)
        with tempfile.TemporaryDirectory() as directory:
            engine = WhisperCpuEngine(Path(directory))
            with self.assertRaisesRegex(EngineError, "model_not_prepared"):
                engine.transcribe(b"\0" * 32, 0, 1000)
        with self.assertRaises(ValueError):
            EngineConfig(cpu_threads=9)

    @unittest.skipUnless(shutil.which("ffmpeg") and Path("/proc/self/fd").is_dir(), "Linux FFmpeg required")
    def test_real_ffmpeg_decodes_only_requested_range(self):
        with tempfile.TemporaryFile() as audio:
            with wave.open(audio, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(16000)
                writer.writeframes(b"\x00\x00" * 32000)
            audio.flush()
            audio.seek(0)
            pcm = decode_pcm(audio, 500, 1000)
            self.assertEqual(len(pcm), 16000)
            self.assertEqual(pcm, bytes(16000))
            with self.assertRaises(EngineError):
                decode_pcm(audio, 1000, 10000)


if __name__ == "__main__":
    unittest.main()
