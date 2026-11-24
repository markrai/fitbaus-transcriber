import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
import wave

from transcriber.audiobookshelf import SourceConfig
from transcriber.config import Config
from transcriber.engine import EngineConfig
from transcriber.server import Service


OPERATOR = "test-only-operator-" + "a" * 32
USER = "test-only-source-user"


def main():
    path = Path(sys.argv[1])
    audio = path.read_bytes()
    with wave.open(str(path), "rb") as stream:
        duration_ms = stream.getnframes() * 1000 // stream.getframerate()
    item = {
        "id": "ci-book", "mediaType": "book",
        "media": {"tracks": [{
            "ino": "1", "startOffset": 0, "duration": duration_ms / 1000,
            "contentUrl": "/api/items/ci-book/file/1",
            "metadata": {"size": len(audio), "mtimeMs": 1, "ctimeMs": 1},
        }]},
    }

    class SourceHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer " + USER:
                status, body, mime = 403, b"denied", "text/plain"
            elif self.path == "/api/items/ci-book?expanded=1":
                status, body, mime = 200, json.dumps(item).encode(), "application/json"
            elif self.path == "/api/items/ci-book/file/1":
                status, body, mime = 200, audio, "audio/wav"
            else:
                status, body, mime = 404, b"missing", "text/plain"
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    config = Config(OPERATOR, "127.0.0.1", 8765, Path("/data"),
                    SourceConfig("ci-source", f"http://127.0.0.1:{upstream.server_port}"),
                    EngineConfig(language="en"))
    server = Service(("127.0.0.1", 0), config)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def request(method, route, body=None, token=USER):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        headers = {"Authorization": "Bearer " + OPERATOR, "X-Audiobookshelf-Token": token}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        try:
            conn.request(method, route, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    try:
        assert request("GET", "/readyz")[0] == 200
        code, manifest = request("GET", "/v1/items/ci-book/manifest")
        assert code == 200, manifest
        body = {key: manifest[key] for key in ("source_id", "item_id", "audio_revision")}
        body.update(start_ms=0, end_ms=duration_ms)
        code, job = request("POST", "/v1/jobs", body)
        assert code in (200, 202), job
        route = "/v1/jobs/" + job["job_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            code, job = request("GET", route)
            assert code == 200, job
            if job["state"] == "ready":
                break
            assert job["state"] != "failed", job
            time.sleep(0.25)
        assert job["state"] == "ready", job
        code, result = request("GET", route + "/transcript")
        assert code == 200, result
        assert result["coverage"] == {"start_ms": 0, "end_ms": duration_ms}
        text = " ".join(cue["text"] for cue in result["cues"]).lower()
        assert any(word in text for word in ("morning", "sun", "shining", "audio", "test")), text
        assert request("POST", "/v1/jobs", body)[0] == 200
        assert request("GET", route + "/transcript", token="denied")[0] == 403
        print(json.dumps({"state": "ready", "cue_count": len(result["cues"]), "text": text}))
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


if __name__ == "__main__":
    main()
