import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from transcriber.config import Config
from transcriber.contract import AudioIdentity, Cue, TranscriptWindow
from transcriber.server import Service


TOKEN = "a" * 64


class ConfigTests(unittest.TestCase):
    def test_valid_config(self):
        config = Config.from_env({"TRANSCRIBER_TOKEN": TOKEN})
        self.assertEqual(config.port, 8765)
        self.assertEqual(config.bind, "127.0.0.1")

    def test_invalid_config(self):
        for env in ({}, {"TRANSCRIBER_TOKEN": "short"},
                    {"TRANSCRIBER_TOKEN": TOKEN, "TRANSCRIBER_PORT": "0"},
                    {"TRANSCRIBER_TOKEN": TOKEN, "TRANSCRIBER_DATA_DIR": "relative"}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                Config.from_env(env)

    def test_file_token_and_exclusive_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text(TOKEN + "\n", encoding="utf-8")
            env = {"TRANSCRIBER_TOKEN_FILE": str(path)}
            self.assertEqual(Config.from_env(env).token, TOKEN)
            with self.assertRaises(ValueError):
                Config.from_env({**env, "TRANSCRIBER_TOKEN": TOKEN})


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.identity = AudioIdentity("library-server", "item-1", "a" * 64)

    def test_revision_and_source_isolation(self):
        self.assertNotEqual(self.identity.cache_key(), AudioIdentity("library-server", "item-1", "b" * 64).cache_key())
        self.assertNotEqual(self.identity.cache_key(), AudioIdentity("another-server", "item-1", "a" * 64).cache_key())
        self.assertEqual(self.identity.cache_key(), AudioIdentity("library-server", "item-1", "a" * 64).cache_key())
        with self.assertRaises(ValueError):
            AudioIdentity("library-server", "../item", "a" * 64)

    def test_book_relative_window_and_silence(self):
        window = TranscriptWindow(self.identity, 60_000, 65_000, (Cue(61_000, 62_000, "Hello."),))
        self.assertEqual(window.to_json()["coverage"], {"start_ms": 60_000, "end_ms": 65_000})
        self.assertEqual(window.to_json()["cues"][0]["start_ms"], 61_000)
        self.assertEqual(TranscriptWindow(self.identity, 0, 1000, ()).to_json()["cues"], [])

    def test_invalid_timing_and_limits(self):
        for start, end, value in [(0, 0, "x"), (-1, 1, "x"), (True, 10, "x"), (0, 10, ""), (0, 10, "x" * 1201)]:
            with self.assertRaises(ValueError):
                Cue(start, end, value)
        for cues in [(Cue(0, 2, "a"), Cue(1, 3, "b")), (Cue(9, 11, "x"),)]:
            with self.assertRaises(ValueError):
                TranscriptWindow(self.identity, 0, 10, cues)
        with self.assertRaises(ValueError):
            TranscriptWindow(self.identity, 0, 300_001, ())


class HttpTests(unittest.TestCase):
    def setUp(self):
        config = Config(TOKEN, "127.0.0.1", 8765, Path("/data"))
        self.server = Service(("127.0.0.1", 0), config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path, token=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            headers = {} if token is None else {"Authorization": "Bearer " + token}
            client.request("GET", path, headers=headers)
            response = client.getresponse()
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_liveness_is_not_engine_readiness(self):
        self.assertEqual(self.get("/healthz"), (200, {"status": "alive"}))
        status, body = self.get("/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(body["reason"], "engine_not_implemented")

    def test_capabilities_require_bearer(self):
        for token in (None, "wrong"):
            self.assertEqual(self.get("/v1/capabilities", token)[0], 401)
        status, body = self.get("/v1/capabilities", TOKEN)
        self.assertEqual(status, 200)
        self.assertFalse(body["transcription"])
        self.assertEqual(body["transcript_schema"], "fitbaus.transcript/v1")

    def test_no_query_token_or_unimplemented_transcript_routes(self):
        self.assertEqual(self.get("/v1/capabilities?token=" + TOKEN)[0], 401)
        self.assertEqual(self.get("/v1/transcripts", TOKEN)[0], 404)


if __name__ == "__main__":
    unittest.main()
