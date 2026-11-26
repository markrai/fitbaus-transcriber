import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

from transcriber.config import Config
from transcriber.server import Service
from transcriber.summary_provider import (
    MAX_INPUT_BYTES, MAX_RESPONSE_BYTES, OpenAIChatProvider, SummaryProviderConfig,
    SummaryProviderError, summary_capability,
)


class SummaryProviderTests(unittest.TestCase):
    def test_optional_environment_and_private_capability(self):
        base = {"TRANSCRIBER_TOKEN": "a" * 64}
        self.assertIsNone(Config.from_env(base).summary_provider)
        env = dict(base, TRANSCRIBER_SUMMARY_BASE_URL="https://example.test/v1/",
                   TRANSCRIBER_SUMMARY_MODEL="qwen3:14b", TRANSCRIBER_SUMMARY_API_KEY="private-key")
        config = Config.from_env(env).summary_provider
        self.assertEqual(config.base_url, "https://example.test/v1")
        self.assertNotIn("private-key", repr(config))
        self.assertEqual(summary_capability(config),
                         {"configured": True, "protocol": "openai-chat/v1", "model": "qwen3:14b"})
        for key in ("TRANSCRIBER_SUMMARY_MODEL", "TRANSCRIBER_SUMMARY_BASE_URL"):
            with self.assertRaises(ValueError):
                Config.from_env({**env, key: ""})

    def test_custom_ca_is_https_only_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.pem"
            invalid.write_text("not a certificate")
            for url, path in (("https://host/v1", str(invalid)),
                              ("https://host/v1", "relative.pem"),
                              ("https://host/v1", str(invalid) + ".missing"),
                              ("http://host/v1", str(invalid))):
                with self.subTest(url=url, path=path), self.assertRaises(ValueError):
                    SummaryProviderConfig(url, "model", allow_http=True, ca_file=path)

    def test_rejects_unsafe_configuration(self):
        for url in ("http://host/v1", "https://user:secret@host/v1", "https://host/v1?key=x",
                    "https://host/v1#fragment", "https://host/v1/chat/completions", "https://host/../v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                SummaryProviderConfig(url, "model")
        for kwargs in ({"model": "bad model"}, {"api_key": "key\r\nheader"}, {"timeout": 121}):
            with self.assertRaises(ValueError):
                SummaryProviderConfig("https://host/v1", **{"model": "model", **kwargs})
        self.assertEqual(SummaryProviderConfig("http://ollama:11434/v1", "model", allow_http=True).base_url,
                         "http://ollama:11434/v1")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.reply = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": " A summary. "}}]}).encode()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                owner.requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(owner.status)
                if owner.status == 302:
                    self.send_header("Location", "/other")
                self.send_header("Content-Length", str(len(owner.reply)))
                self.end_headers()
                self.wfile.write(owner.reply)

            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers), {}))
                self.send_response(owner.status)
                self.send_header("Content-Length", str(len(owner.reply)))
                self.end_headers()
                self.wfile.write(owner.reply)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = SummaryProviderConfig(f"http://127.0.0.1:{self.server.server_port}/v1", "model", "provider-key", True, 5)
        self.provider = OpenAIChatProvider(self.config)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_completion_uses_only_provider_credential_and_fixed_endpoint(self):
        self.assertEqual(self.provider.complete("System", "Narration"), "A summary.")
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer provider-key")
        self.assertEqual(body["model"], "model")
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"], [{"role": "system", "content": "System"}, {"role": "user", "content": "Narration"}])
        self.assertNotIn("X-Audiobookshelf-Token", headers)

    def test_models_uses_same_transport_and_returns_only_identifiers(self):
        self.reply = json.dumps({"data": [{"id": "lobo-nano", "secret": "private"},
                                          {"id": "other"}, {"id": "lobo-nano"}]}).encode()
        self.assertEqual(self.provider.models(), ["lobo-nano", "other"])
        path, headers, _ = self.requests[0]
        self.assertEqual(path, "/v1/models")
        self.assertEqual(headers["Authorization"], "Bearer provider-key")
        for reply in (b"not json", b'{"data":{}}', b'{"data":[{"id":"bad model"}]}'):
            self.reply = reply
            with self.assertRaisesRegex(SummaryProviderError, "invalid_response"):
                self.provider.models()

    def test_redirect_and_upstream_errors_are_sanitized(self):
        self.reply = b"provider-key private narration"
        for status, code in ((302, "provider_http_error"), (401, "provider_authentication"), (429, "provider_rate_limited"), (500, "provider_http_error")):
            self.status = status
            with self.assertRaisesRegex(SummaryProviderError, "^" + code + "$"):
                self.provider.complete("System", "Narration")
        self.assertEqual(len(self.requests), 4)

    def test_invalid_output_and_input_bounds(self):
        with self.assertRaises(ValueError):
            self.provider.complete("System", "x" * MAX_INPUT_BYTES)
        self.assertEqual(self.requests, [])
        for reply in (b"not json", b'{"choices":[]}', b'{"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}',
                      b'{"choices":[{"finish_reason":"stop","message":{"content":"", "tool_calls":[{}]}}]}'):
            self.reply = reply
            with self.assertRaisesRegex(SummaryProviderError, "invalid_completion"):
                self.provider.complete("System", "Narration")
        self.reply = b"x" * (MAX_RESPONSE_BYTES + 1)
        with self.assertRaisesRegex(SummaryProviderError, "response_too_large"):
            self.provider.complete("System", "Narration")


class CapabilityTests(unittest.TestCase):
    def test_configured_does_not_probe_or_reveal_provider_details(self):
        from http.client import HTTPConnection
        with tempfile.TemporaryDirectory() as directory:
            provider = SummaryProviderConfig("https://unreachable.example/v1", "model", "private-key")
            config = Config("a" * 64, "127.0.0.1", 8765, Path(directory), summary_provider=provider)
            server = Service(("127.0.0.1", 0), config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    client.request("GET", "/v1/capabilities", headers={"Authorization": "Bearer " + config.token})
                    response = client.getresponse()
                    body = response.read().decode()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(body)["summary_provider"], summary_capability(provider))
                    self.assertNotIn("private-key", body)
                    self.assertNotIn("unreachable.example", body)
                    self.assertFalse(json.loads(body)["transcription"])
                    self.assertTrue(json.loads(body)["summary_provider_selection_api"])
                    paired = server.pairing.redeem(server.pairing.issue())[1]["token"]
                    client.request("GET", "/v1/capabilities", headers={"Authorization": "Bearer " + paired})
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    body = response.read().decode()
                    self.assertNotIn("unreachable.example", body)
                    self.assertNotIn("private-key", body)
                    client.request("GET", "/v1/admin/summary-provider/selection",
                                   headers={"Authorization": "Bearer " + paired})
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    body = response.read().decode()
                    selection = json.loads(body)
                    self.assertEqual(selection["base_url"], provider.base_url)
                    self.assertEqual(selection["model"], provider.model)
                    self.assertTrue(selection["has_api_key"])
                    self.assertFalse(selection["admin_required"])
                    self.assertNotIn("private-key", body)
                    for token in (config.token, "invalid"):
                        client.request("GET", "/v1/admin/summary-provider/selection",
                                       headers={"Authorization": "Bearer " + token})
                        response = client.getresponse()
                        self.assertEqual(response.status, 401)
                        self.assertNotIn("unreachable.example", response.read().decode())
                finally:
                    client.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

class AdminProbeTests(unittest.TestCase):
    def test_admin_routes_do_not_grant_paired_devices_provider_administration(self):
        from http.client import HTTPConnection
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            config = Config("a" * 64, "127.0.0.1", 8765, Path(directory),
                            summary_provider=SummaryProviderConfig("https://provider.example/v1", "model", "secret"))
            with Service(("127.0.0.1", 0), config) as server:
                server.summary_provider = Mock()
                server.summary_provider.models.return_value = ["model"]
                server.summary_provider.complete.return_value = "Synthetic summary"
                server.pairing.authorized = lambda token: token == "paired"
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def call(method, path, token):
                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    try:
                        conn.request(method, path, headers={"Authorization": "Bearer " + token})
                        response = conn.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        conn.close()
                try:
                    root = "/v1/admin/summary-provider"
                    for method, path in (("GET", root), ("POST", root + "/models"), ("POST", root + "/test")):
                        self.assertEqual(call(method, path, "paired")[0], 401)
                    server.summary_provider.models.assert_not_called()
                    server.summary_provider.complete.assert_not_called()
                    status, data = call("GET", root, config.token)
                    self.assertEqual(status, 200)
                    self.assertTrue(data["has_api_key"])
                    self.assertNotIn("secret", json.dumps(data))
                    self.assertEqual(call("POST", root + "/models", config.token), (200, {"models": ["model"]}))
                    status, data = call("POST", root + "/test", config.token)
                    self.assertEqual(status, 200)
                    self.assertTrue(data["ok"])
                    self.assertNotIn("Synthetic summary", json.dumps(data))
                    server.summary_provider.complete.assert_called_once()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
