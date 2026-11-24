import hashlib
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from transcriber.config import Config
from transcriber.model import MODEL_FILES, model_directory, model_spec
from transcriber.model_downloads import DownloadError, ModelDownloads, fetch_model, write_status
from transcriber.server import Service


def sleeper(args, **kwargs):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.root = self.data / "models"
        self.root.mkdir()
        self.spec = model_spec("base")

    def wait(self, manager):
        deadline = time.monotonic() + 5
        while manager.active is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(manager.active)

    def fake_download(self, corrupt=False):
        data = {name: (name + "-bytes").encode() for name in MODEL_FILES}
        entries = []
        for name, payload in data.items():
            lfs = name == "model.bin"
            digest = hashlib.sha256(payload).hexdigest() if lfs else hashlib.sha1(
                f"blob {len(payload)}\0".encode() + payload).hexdigest()
            entries.append(SimpleNamespace(rfilename=name, size=len(payload),
                                           lfs=SimpleNamespace(sha256=digest) if lfs else None,
                                           blob_id=digest if not lfs else None))
        info = SimpleNamespace(sha=self.spec.revision, siblings=entries)
        hub = SimpleNamespace(HfApi=lambda **kw: SimpleNamespace(model_info=lambda *a, **kw: info),
                              hf_hub_url=lambda repo, filename, **kw: "https://example.test/" + filename)

        def open_url(url, **kwargs):
            name = url.rsplit("/", 1)[-1]
            payload = data[name]
            if corrupt and name == "model.bin":
                payload = b"x" * len(payload)
            response = io.BytesIO(payload)
            response.geturl = lambda: url
            return response

        with patch.dict(sys.modules, {"huggingface_hub": hub}), patch("urllib.request.urlopen", side_effect=open_url):
            fetch_model(self.data, "base", 1024 * 1024 * 1024)

    def test_verified_download_publishes_atomically(self):
        self.fake_download()
        self.assertTrue(model_directory(self.data, "base").is_dir())
        self.assertFalse((self.root / ".download-base").exists())
        manager = ModelDownloads(self.data)
        self.addCleanup(manager.close)
        self.assertFalse(manager.start("base"))
        base = manager.inventory()[1]
        self.assertEqual(base["state"], "installed")
        self.assertEqual(base["received_bytes"], base["total_bytes"])

    def test_checksum_mismatch_never_publishes(self):
        with self.assertRaisesRegex(DownloadError, "verification_failed"):
            self.fake_download(corrupt=True)
        self.assertFalse(model_directory(self.data, "base").exists())
        self.assertFalse((self.root / ".download-base").exists())

    def test_single_worker_cancel_and_retry(self):
        manager = ModelDownloads(self.data, spawn=sleeper)
        self.addCleanup(manager.close)
        self.assertTrue(manager.start("base"))
        self.assertTrue(manager.start("base"))
        with self.assertRaisesRegex(DownloadError, "download_busy"):
            manager.start("small")
        another = ModelDownloads(self.data, spawn=sleeper)
        self.addCleanup(another.close)
        with self.assertRaisesRegex(DownloadError, "download_busy"):
            another.start("small")
        self.assertTrue(manager.cancel("base"))
        self.wait(manager)
        self.assertEqual(manager.inventory()[1]["state"], "cancelled")
        self.assertTrue(manager.start("small"))
        manager.cancel("small")
        self.wait(manager)

    def test_timeout_and_quota(self):
        manager = ModelDownloads(self.data, quota_mib=128, spawn=sleeper)
        self.addCleanup(manager.close)
        with self.assertRaisesRegex(DownloadError, "model_quota_exceeded"):
            manager.start("base")
        with self.assertRaises(ValueError):
            manager.start("medium")
        timed = ModelDownloads(self.data, timeout=0.1, spawn=sleeper)
        self.addCleanup(timed.close)
        timed.start("base")
        self.wait(timed)
        self.assertEqual(timed.inventory()[1]["error"], "download_timeout")

    def test_restart_cleans_orphan_stage(self):
        stage = self.root / ".download-base"
        stage.mkdir()
        (stage / "partial").write_bytes(b"partial")
        write_status(self.root, "base", "downloading", 7, 100)
        manager = ModelDownloads(self.data)
        self.addCleanup(manager.close)
        self.assertFalse(stage.exists())
        self.assertEqual(manager.inventory()[1]["state"], "interrupted")

    def test_http_operator_controls_and_paired_read_only(self):
        token = "a" * 64
        server = Service(("127.0.0.1", 0), Config(token, "127.0.0.1", 8765, self.data))
        server.downloads.spawn = sleeper
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(method, path, credential):
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            try:
                client.request(method, path, headers={"Authorization": "Bearer " + credential})
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()

        code = server.pairing.issue()
        status, result = server.pairing.redeem(code)
        self.assertEqual(status, 200)
        paired = result["token"]
        self.assertEqual(request("GET", "/v1/models", paired)[0], 200)
        self.assertEqual(request("POST", "/v1/models/base/download", paired)[0], 401)
        self.assertEqual(request("POST", "/v1/models/medium/download", token)[0], 400)
        self.assertEqual(request("POST", "/v1/models/base/download", token)[0], 202)
        self.assertEqual(request("POST", "/v1/models/base/cancel", token)[0], 202)
