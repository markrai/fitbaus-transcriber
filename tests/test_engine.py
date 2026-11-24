from contextlib import contextmanager
import io
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
import wave

from transcriber.audiobookshelf import Manifest, SourceError, Track
from transcriber.contract import AudioIdentity, Cue
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
