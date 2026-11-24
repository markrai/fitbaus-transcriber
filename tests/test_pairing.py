import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from transcriber.config import Config
from transcriber.pairing import Pairing
from transcriber.server import Service


class PairingTests(unittest.TestCase):
    def test_expiry_attempts_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [0]
            pairing = Pairing(Path(directory), lambda: now[0])
            code = pairing.issue()
            self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
            now[0] = 600
            self.assertEqual(pairing.redeem(code)[0], 401)
            code = pairing.issue()
            for _ in range(5):
                self.assertEqual(pairing.redeem("0000-0000")[0], 401)
            self.assertEqual(pairing.redeem(code)[0], 401)
            code = pairing.issue()
            status, result = pairing.redeem(code.lower().replace("-", ""))
            self.assertEqual(status, 200)
            self.assertEqual(pairing.redeem(code)[0], 401)
            token = result["token"]
            restored = Pairing(Path(directory))
            self.assertTrue(restored.authorized(token))
            self.assertNotIn(token.encode(), restored.path.read_bytes())
            restored.revoke_all()
            self.assertFalse(restored.authorized(token))

    def test_http_exchange_does_not_grant_operator_access(self):
        with tempfile.TemporaryDirectory() as directory:
            operator = "a" * 64
            server = Service(("127.0.0.1", 0), Config(operator, "127.0.0.1", 8765, Path(directory)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(method, path, token=None, body=None):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                headers = {}
                if token:
                    headers["Authorization"] = "Bearer " + token
                if body is not None:
                    headers["Content-Type"] = "application/json"
                try:
                    client.request(method, path, json.dumps(body) if body is not None else None, headers)
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    client.close()

            try:
                self.assertEqual(request("POST", "/v1/pairing/issue")[0], 401)
                status, issued = request("POST", "/v1/pairing/issue", operator)
                self.assertEqual(status, 200)
                status, result = request("POST", "/v1/pairing/redeem", body={"code": issued["code"]})
                self.assertEqual(status, 200)
                token = result["token"]
                self.assertNotEqual(token, operator)
                self.assertEqual(request("GET", "/v1/capabilities", token)[0], 200)
                self.assertEqual(request("POST", "/v1/pairing/issue", token)[0], 401)
                self.assertEqual(request("POST", "/v1/pairing/revoke-all", token)[0], 401)
                self.assertEqual(request("POST", "/v1/pairing/redeem", body={"code": issued["code"]})[0], 401)
                self.assertEqual(request("POST", "/v1/pairing/revoke-all", operator)[0], 200)
                self.assertEqual(request("GET", "/v1/capabilities", token)[0], 401)
                self.assertEqual(request("GET", "/v1/capabilities", operator)[0], 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
