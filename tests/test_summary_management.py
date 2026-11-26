import http.client
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from transcriber.config import Config
from transcriber.server import Service
from transcriber.summary_management import (
    ADMIN_HEADER, ROOT, ManagementError, SummaryManagement, SummarySettings, lobo_auth, lobo_ca,
)
from transcriber.summary_provider import SummaryProviderConfig, SummaryProviderError


class StorageTests(unittest.TestCase):
    def test_fallback_save_disable_and_file_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SummarySettings(directory)
            original = SummaryProviderConfig("https://example.test/v1", "old")
            updated = SummaryProviderConfig("https://new.test/v1", "new", "private")
            self.assertEqual(store.load(original), original)
            store.save(updated)
            self.assertEqual(store.load(original), updated)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            store.save(None)
            self.assertIsNone(store.load(original))

    def test_invalid_saved_state_does_not_fall_back_to_env(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config("a" * 64, "127.0.0.1", 8765, Path(directory),
                            summary_provider=SummaryProviderConfig("https://example.test/v1", "old"))
            SummarySettings(directory).path.write_text("corrupted")
            with Service(("127.0.0.1", 0), config) as server:
                self.assertTrue(server.summary_settings_error)
                self.assertIsNone(server.summary_provider)
                self.assertIsNone(server.config.summary_provider)

    def test_code_expiry_attempts_binding_and_reissue(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [0]
            manager = SummaryManagement(directory, lambda: now[0])
            code = manager.issue()["code"]
            for _ in range(5):
                with self.assertRaises(ManagementError):
                    manager.redeem("0000-0000", "device")
            with self.assertRaises(ManagementError):
                manager.redeem(code, "device")
            code = manager.issue()["code"]
            now[0] = 600
            with self.assertRaises(ManagementError):
                manager.redeem(code, "device")
            code = manager.issue()["code"]
            grant = manager.redeem(code, "device")["token"]
            manager.require(grant, "device", lambda: True)
            for device, guard in (("other", True), ("device", False)):
                with self.assertRaises(ManagementError):
                    manager.require(grant, device, lambda: guard)
            with self.assertRaises(ManagementError):
                manager.redeem(code, "device")
            manager.issue()
            with self.assertRaises(ManagementError):
                manager.require(grant, "device", lambda: True)

    def test_lobo_auth_pins_only_verified_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(path / "key.pem"), "-out", str(path / "ca.pem"),
                "-subj", "/CN=LOBO Test CA", "-days", "1"], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pem = (path / "ca.pem").read_text()
            auth = lobo_auth(pem)
            with patch("transcriber.summary_management.subprocess.run",
                       return_value=Mock(stdout=(pem + pem).encode())) as run:
                with self.assertRaisesRegex(ManagementError, "lobo_auth_mismatch"):
                    lobo_ca("https://192.168.1.200:7869/v1", "0000-0000")
                pinned = lobo_ca("https://192.168.1.200:7869/v1", auth)
                config = SummaryProviderConfig("https://192.168.1.200:7869/v1", "lobo-nano", ca_pem=pinned)
                context = config.tls_context()
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertEqual(context.get_ca_certs(binary_form=True), [ssl.PEM_cert_to_DER_cert(pem)])
                with self.assertRaises(ManagementError):
                    lobo_ca("https://127.0.0.1:7869/v1", auth)
                self.assertEqual(run.call_count, 2)


class LoboTrustRegistryTests(unittest.TestCase):
    def make_ca(self, directory, name):
        path = Path(directory)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(path / (name + ".key")), "-out", str(path / (name + ".pem")),
            "-subj", "/CN=" + name, "-days", "1",
            "-addext", "subjectAltName=IP:192.168.1.200"], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return (path / (name + ".pem")).read_text()

    def test_verified_saved_devices_survive_switch_disable_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            one = "https://192.168.1.200:7869/v1"
            two = "https://192.168.1.201:7869/v1"
            pem_one = self.make_ca(directory, "one")
            pem_two = self.make_ca(directory, "two")
            manager = SummaryManagement(directory)
            current = None
            def activate(config, provider):
                nonlocal current
                current = config
            def authorize():
                return manager.redeem(manager.issue()["code"], "device")["token"]
            def call(action, body, grant):
                return manager.execute(action, body, grant, "device", lambda: True, current, activate)
            def body(endpoint, auth=None):
                value = {"kind": "lobo", "base_url": endpoint, "allow_http": False}
                if auth is not None:
                    value["lobo_auth"] = auth
                return value
            provider = Mock()
            provider.models.return_value = ["lobo-nano"]
            provider.complete.return_value = "Synthetic result"
            with patch("transcriber.summary_management.OpenAIChatProvider", return_value=provider), \
                 patch("transcriber.summary_management.lobo_ca", side_effect=[pem_one, pem_two]) as verify:
                grant = authorize()
                with self.assertRaisesRegex(ManagementError, "lobo_identity_verification_required"):
                    call("discover", body(one), grant)
                call("discover", body(one, "ABCD-EFGH"), grant)
                self.assertEqual(call("status", {}, grant)["trusted_lobo_endpoints"], [])
                call("test", {"model": "lobo-nano"}, grant)
                self.assertEqual(call("status", {}, grant)["trusted_lobo_endpoints"], [])
                call("save", {"confirm_egress": True}, grant)
                grant = authorize()
                call("discover", body(two, "ABCD-EFGH"), grant)
                call("test", {"model": "lobo-nano"}, grant)
                call("save", {"confirm_egress": True}, grant)
                grant = authorize()
                self.assertEqual(call("status", {}, grant)["trusted_lobo_endpoints"], [one, two])
                call("disable", {"confirm": True}, grant)
                manager = SummaryManagement(directory)
                current = manager.settings.load(None)
                self.assertIsNone(current)
                grant = authorize()
                result = call("discover", body(one), grant)
                self.assertNotIn("BEGIN CERTIFICATE", json.dumps(result))
                self.assertEqual(manager.candidate.ca_pem, pem_one)
                self.assertEqual(manager.candidate.tls_context().get_ca_certs(binary_form=True),
                                 [ssl.PEM_cert_to_DER_cert(pem_one)])
                call("test", {"model": "lobo-nano"}, grant)
                call("save", {"confirm_egress": True}, grant)
                self.assertEqual(verify.call_count, 2)
                grant = authorize()
                with self.assertRaisesRegex(ManagementError, "lobo_trust_active"):
                    call("forget-lobo", {"base_url": one, "confirm": True}, grant)
                result = call("forget-lobo", {"base_url": two, "confirm": True}, grant)
                self.assertEqual(result["trusted_lobo_endpoints"], [one])
                with self.assertRaisesRegex(ManagementError, "lobo_identity_verification_required"):
                    call("discover", body(two), grant)
                self.assertEqual(manager.settings.path.stat().st_mode & 0o777, 0o600)

    def test_legacy_active_pin_migrates_and_failed_discovery_does_not_replace_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = "https://192.168.1.200:7869/v1"
            original = self.make_ca(directory, "original")
            replacement = self.make_ca(directory, "replacement")
            from dataclasses import asdict
            old = SummaryProviderConfig(endpoint, "lobo-nano", ca_pem=original)
            settings = SummarySettings(directory)
            settings.path.write_text(json.dumps({"version": 1, "provider": asdict(old)}))
            manager = SummaryManagement(directory)
            current = settings.load(None)
            grant = manager.redeem(manager.issue()["code"], "device")["token"]
            def call(action, body):
                return manager.execute(action, body, grant, "device", lambda: True, current, lambda *_: None)
            self.assertEqual(call("status", {})["trusted_lobo_endpoints"], [endpoint])
            provider = Mock()
            provider.models.side_effect = SummaryProviderError("provider_unavailable")
            with patch("transcriber.summary_management.OpenAIChatProvider", return_value=provider), \
                 patch("transcriber.summary_management.lobo_ca", return_value=replacement):
                with self.assertRaises(SummaryProviderError):
                    call("discover", {"kind": "lobo", "base_url": endpoint, "allow_http": False})
                self.assertIsNone(manager.candidate)
                with self.assertRaises(SummaryProviderError):
                    call("discover", {"kind": "lobo", "base_url": endpoint, "allow_http": False,
                                      "lobo_auth": "ABCD-EFGH"})
                self.assertEqual(settings.trusted(current)[endpoint], original)
            settings.save(None, previous=current)
            self.assertEqual(settings.trusted(None)[endpoint], original)
            self.assertEqual(json.loads(settings.path.read_text())["version"], 2)

    def test_remembered_pin_rejects_replaced_certificate_in_real_tls_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = "https://192.168.1.200:7869/v1"
            original = self.make_ca(directory, "old")
            self.make_ca(directory, "new")
            config = SummaryManagement.build_candidate(
                {"kind": "lobo", "base_url": endpoint, "allow_http": False},
                None, {endpoint: original})
            self.assertTrue(config.tls_context().check_hostname)
            self.assertEqual(config.tls_context().verify_mode, ssl.CERT_REQUIRED)

            def handshake(name):
                server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                server_context.load_cert_chain(str(Path(directory) / (name + ".pem")),
                                               str(Path(directory) / (name + ".key")))
                local, peer = socket.socketpair()
                local.settimeout(3)
                peer.settimeout(3)
                def serve():
                    try:
                        with server_context.wrap_socket(local, server_side=True):
                            pass
                    except (OSError, ssl.SSLError):
                        pass
                    finally:
                        local.close()
                worker = threading.Thread(target=serve)
                worker.start()
                try:
                    with config.tls_context().wrap_socket(peer, server_hostname="192.168.1.200"):
                        pass
                finally:
                    peer.close()
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
            handshake("old")
            with self.assertRaises(ssl.SSLCertVerificationError):
                handshake("new")

    def test_corrupted_registry_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SummarySettings(directory)
            store.path.write_text(json.dumps({"version": 2, "provider": None,
                "lobo_trust": {"https://192.168.1.200:7869/v1": "invalid"}}))
            with self.assertRaises(ValueError):
                store.load(None)
            self.assertEqual(store.trusted(None), {})
            with self.assertRaisesRegex(ManagementError, "lobo_identity_verification_required"):
                SummaryManagement.build_candidate({"kind": "lobo", "base_url": "https://192.168.1.200:7869/v1",
                                                   "allow_http": False}, None, store.trusted(None))

class ManagementApiTests(unittest.TestCase):
    def test_paired_admin_discover_test_save_restart_and_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config("a" * 64, "127.0.0.1", 8765, Path(directory),
                            summary_provider=SummaryProviderConfig("https://old.test/v1", "old"))
            with Service(("127.0.0.1", 0), config) as server:
                paired = server.pairing.redeem(server.pairing.issue())[1]["token"]
                other = server.pairing.redeem(server.pairing.issue())[1]["token"]
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                def call(method, suffix, device=None, grant=None, body=None):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    headers = {"Authorization": "Bearer " + (paired if device is None else device)}
                    if grant:
                        headers[ADMIN_HEADER] = grant
                    if body is not None:
                        headers["Content-Type"] = "application/json"
                    try:
                        conn.request(method, ROOT + suffix, json.dumps(body) if body is not None else None, headers)
                        response = conn.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        conn.close()
                def authorize():
                    status, data = call("POST", "/issue", device=config.token)
                    self.assertEqual(status, 200)
                    status, result = call("POST", "/redeem", body={"code": data["code"]})
                    self.assertEqual(status, 200)
                    return result["token"]
                try:
                    self.assertEqual(call("POST", "/issue")[0], 401)
                    self.assertEqual(call("GET", "/session")[0], 401)
                    grant = authorize()
                    self.assertEqual(call("GET", "/session", device=other, grant=grant)[0], 401)
                    self.assertEqual(call("GET", "/session", grant=grant)[1]["model"], "old")
                    self.assertEqual(call("GET", "/session", grant=grant)[1]["trusted_lobo_endpoints"], [])
                    self.assertEqual(call("POST", "/forget-lobo", body={"base_url": "https://192.168.1.200:7869/v1", "confirm": True})[0], 401)
                    self.assertEqual(call("POST", "/save", grant=grant, body={"confirm_egress": True})[0], 400)
                    candidate = {"kind": "custom", "base_url": "https://new.test/v1", "allow_http": False,
                                 "api_key": "private-key"}
                    provider = Mock()
                    provider.models.return_value = ["model"]
                    provider.complete.side_effect = SummaryProviderError("provider_unavailable")
                    with patch("transcriber.summary_management.OpenAIChatProvider", return_value=provider):
                        status, data = call("POST", "/discover", grant=grant, body=candidate)
                        self.assertEqual(status, 200)
                        self.assertEqual(data["models"], ["model"])
                        self.assertNotIn("private-key", json.dumps(data))
                        self.assertEqual(call("POST", "/test-candidate", grant=grant, body={"model": "model"})[0], 502)
                        self.assertEqual(call("POST", "/save", grant=grant, body={"confirm_egress": True})[0], 400)
                        self.assertEqual(server.config.summary_provider.model, "old")
                        provider.complete.side_effect = None
                        provider.complete.return_value = "Synthetic result"
                        self.assertEqual(call("POST", "/test-candidate", grant=grant, body={"model": "model"})[0], 200)
                        self.assertEqual(call("POST", "/save", grant=grant, body={"confirm_egress": False})[0], 400)
                        status, data = call("POST", "/save", grant=grant, body={"confirm_egress": True})
                        self.assertEqual(status, 200)
                        self.assertEqual(data["model"], "model")
                        self.assertNotIn("private-key", json.dumps(data))
                    self.assertEqual(server.config.summary_provider.model, "model")
                    self.assertNotEqual(server.config.summary_provider.cache_epoch, "1")
                    self.assertEqual(SummarySettings(directory).load(config.summary_provider), server.config.summary_provider)
                    self.assertEqual(call("GET", "/session", grant=grant)[0], 401)
                    grant = authorize()
                    server.pairing.revoke_all()
                    self.assertEqual(call("POST", "/disable", grant=grant, body={"confirm": True})[0], 401)
                    revoked = paired
                    paired = server.pairing.redeem(server.pairing.issue())[1]["token"]
                    self.assertNotEqual(paired, revoked)
                    self.assertEqual(call("GET", "/session", device=revoked, grant=grant)[0], 401)
                    grant = authorize()
                    self.assertEqual(call("GET", "/session", device=revoked, grant=grant)[0], 401)
                    self.assertEqual(call("POST", "/disable", grant=grant, body={"confirm": True})[0], 200)
                    self.assertIsNone(server.summary_provider)
                    self.assertIsNone(SummarySettings(directory).load(config.summary_provider))
                finally:
                    server.shutdown()
                    worker.join(timeout=5)
            with Service(("127.0.0.1", 0), config) as restored:
                self.assertIsNone(restored.summary_provider)
