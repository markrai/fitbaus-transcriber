import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from transcriber.audiobookshelf import Audiobookshelf, SourceConfig, SourceError, parse_manifest
from transcriber.config import Config
from transcriber.server import Service


OPERATOR = "a" * 64
USER = "authorized-user-token"
AUDIO = b"fake audio data"


def fixture():
    return {
        "id": "book-1", "mediaType": "book",
        "media": {"tracks": [
            {"ino": "123", "startOffset": 0, "duration": 10.125,
             "contentUrl": "/api/items/book-1/file/123",
             "metadata": {"size": len(AUDIO), "mtimeMs": 1700000000000.125, "ctimeMs": 1700000000000.5}},
            {"ino": "456", "startOffset": 10.125, "duration": 20,
             "contentUrl": "/api/items/book-1/file/456",
             "metadata": {"size": len(AUDIO), "mtimeMs": 1700000000000, "ctimeMs": 1700000000000}}
        ]}
    }


class UpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server = self.server
        server.requests.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + USER:
            return self.reply(403, b"forbidden")
        if server.redirect:
            self.send_response(302)
            self.send_header("Location", server.redirect)
            self.end_headers()
            return
        if self.path == "/audiobookshelf/api/items/book-1?expanded=1":
            return self.reply(200, json.dumps(server.payload).encode())
        if self.path == "/audiobookshelf/api/items/book-1/file/123":
            self.reply(200, server.audio, "audio/mpeg")
            if server.change_after_audio:
                server.payload["media"]["tracks"][0]["metadata"]["mtimeMs"] += 1
            return
        return self.reply(404, b"missing")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.config = SourceConfig("home", "http://localhost:13378/audiobookshelf/")

    def test_revision_and_ordered_timeline(self):
        data = fixture()
        first = parse_manifest(self.config, "book-1", data)
        self.assertEqual(first.duration_ms, 30125)
        self.assertEqual(first.tracks[1].offset_ms, 10125)
        data["media"]["metadata"] = {"title": "Renamed"}
        self.assertEqual(first.identity, parse_manifest(self.config, "book-1", data).identity)
        for key, value in [("mtimeMs", 1700000000000.25), ("ctimeMs", 1700000000001), ("size", len(AUDIO) + 1)]:
            changed = copy.deepcopy(data)
            changed["media"]["tracks"][0]["metadata"][key] = value
            self.assertNotEqual(first.identity, parse_manifest(self.config, "book-1", changed).identity)
        self.assertNotIn("mtime", json.dumps(first.to_json()))

    def test_bad_track_endpoint_and_timing(self):
        for key, value in [("contentUrl", "https://other.example/audio"), ("contentUrl", "/api/items/other/file/123"), ("ino", "../123"), ("startOffset", 1), ("duration", 0), ("exclude", True)]:
            data = fixture()
            data["media"]["tracks"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_manifest(self.config, "book-1", data)
        data = fixture()
        data["media"]["tracks"][1]["startOffset"] = 2
        with self.assertRaises(ValueError):
            parse_manifest(self.config, "book-1", data)

    def test_source_configuration(self):
        self.assertEqual(self.config.base_url, "http://localhost:13378/audiobookshelf")
        for url in ("file:///etc/passwd", "http://u:p@host", "https://host?x=1", "http://host/other", "http://host:0"):
            with self.assertRaises(ValueError):
                SourceConfig("home", url)
        with self.assertRaises(ValueError):
            Config.from_env({"TRANSCRIBER_TOKEN": OPERATOR, "AUDIOBOOKSHELF_URL": "http://host"})
        config = Config.from_env({"TRANSCRIBER_TOKEN": OPERATOR, "AUDIOBOOKSHELF_URL": "http://host", "AUDIOBOOKSHELF_SOURCE_ID": "home"})
        self.assertEqual(config.source.source_id, "home")


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream.payload = fixture()
        self.upstream.requests = []
        self.upstream.redirect = None
        self.upstream.audio = AUDIO
        self.upstream.change_after_audio = False
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.source = SourceConfig("home", f"http://127.0.0.1:{self.upstream.server_port}/audiobookshelf")
        self.adapter = Audiobookshelf(self.source)
        self.service = Service(("127.0.0.1", 0), Config(OPERATOR, "127.0.0.1", 8765, Path("/data"), self.source))
        self.service_thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.service_thread.start()

    def tearDown(self):
        for server, thread in ((self.service, self.service_thread), (self.upstream, self.upstream_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def request(self, operator=None, user=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.service.server_port, timeout=5)
        headers = {}
        if operator:
            headers["Authorization"] = "Bearer " + operator
        if user:
            headers["X-Audiobookshelf-Token"] = user
        try:
            conn.request("GET", "/v1/items/book-1/manifest", headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_requires_both_credentials_and_checks_every_request(self):
        self.assertEqual(self.request(None, USER)[0], 401)
        self.assertEqual(self.request(OPERATOR)[0], 401)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.request(OPERATOR, "denied")[0], 403)
        status, manifest = self.request(OPERATOR, USER)
        self.assertEqual(status, 200)
        self.assertEqual(manifest["duration_ms"], 30125)
        self.assertEqual(self.request(OPERATOR, "denied")[0], 403)
        self.assertEqual(len(self.upstream.requests), 3)
        self.assertNotIn(USER, json.dumps(manifest))

    def test_redirect_is_not_followed(self):
        self.upstream.redirect = self.source.base_url + "/other"
        self.assertEqual(self.request(OPERATOR, USER)[0], 502)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_materialization_rechecks_revision_and_cleans_up(self):
        manifest = self.adapter.manifest("book-1", USER)
        with tempfile.TemporaryDirectory() as directory:
            with self.adapter.materialize_track(manifest.identity, "123", USER, directory) as file:
                self.assertEqual(file.read(), AUDIO)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.upstream.payload["media"]["tracks"][0]["metadata"]["mtimeMs"] += 1
            with self.assertRaisesRegex(SourceError, "stale_revision"):
                with self.adapter.materialize_track(manifest.identity, "123", USER, directory):
                    self.fail("Stale audio was yielded")

    def test_size_limit_and_changed_audio(self):
        manifest = self.adapter.manifest("book-1", USER)
        with tempfile.TemporaryDirectory() as directory:
            small = Audiobookshelf(self.source, max_track_bytes=1)
            with self.assertRaisesRegex(SourceError, "size_limit"):
                with small.materialize_track(manifest.identity, "123", USER, directory):
                    self.fail("Oversized audio was yielded")
            self.upstream.audio = AUDIO + b"x"
            with self.assertRaisesRegex(SourceError, "stale_revision"):
                with self.adapter.materialize_track(manifest.identity, "123", USER, directory):
                    self.fail("Unexpected audio size was yielded")
            self.upstream.audio = AUDIO
            self.upstream.change_after_audio = True
            with self.assertRaisesRegex(SourceError, "stale_revision"):
                with self.adapter.materialize_track(manifest.identity, "123", USER, directory):
                    self.fail("Changed audio was yielded")
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
