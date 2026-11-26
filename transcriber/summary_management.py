from dataclasses import asdict, replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import tempfile
import threading
import time

from .lobo_trust import lobo_endpoint, read_registry, remember_provider, pinned_provider
from .pairing import ALPHABET, MAX_ATTEMPTS, TTL_SECONDS
from .summary_provider import OpenAIChatProvider, SummaryProviderConfig


ADMIN_HEADER = "X-FitBaus-Summary-Admin"
ROOT = "/v1/admin/summary-provider"
MAX_SETTINGS_BYTES = 65536
MAX_PERSISTED_SETTINGS_BYTES = 512 * 1024
LOBO_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PROBE_SYSTEM = "Summarize the supplied text in one sentence."
PROBE_TEXT = "Regular aerobic exercise improves cardiovascular fitness."


class ManagementError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.status = status


def lobo_auth(pem):
    der = ssl.PEM_cert_to_DER_cert(pem)
    value = int.from_bytes(hashlib.sha256(der).digest()[:5], "big")
    return "".join(LOBO_ALPHABET[(value >> shift) & 31] for shift in range(35, -1, -5))


def lobo_ca(base_url, auth):
    if not isinstance(auth, str) or not re.fullmatch(r"[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}", auth):
        raise ManagementError("invalid_lobo_auth")
    pem = capture_lobo_ca(base_url)
    if not hmac.compare_digest(lobo_auth(pem), auth.upper().replace("-", "")):
        raise ManagementError("lobo_auth_mismatch", 401)
    return pem


def capture_lobo_ca(base_url):
    try:
        endpoint = lobo_endpoint(base_url)
    except (ValueError, TypeError):
        raise ManagementError("lobo_private_ipv4_required") from None
    ip = endpoint.removeprefix("https://").split(":")[0]
    try:
        result = subprocess.run(
            ["openssl", "s_client", "-connect", f"{ip}:7869", "-showcerts", "-no_ign_eof"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
            check=False,
        )
        if len(result.stdout) > MAX_SETTINGS_BYTES:
            raise ValueError()
        certificates = re.findall(rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----",
                                  result.stdout, re.DOTALL)
        if len(certificates) != 2:
            raise ValueError()
        pem = certificates[1].decode("ascii")
        ssl.PEM_cert_to_DER_cert(pem)
        return pem
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ManagementError("lobo_certificate_unavailable", 502) from None


def describe(config):
    return {"configured": config is not None,
            "base_url": config.base_url if config else None,
            "model": config.model if config else None,
            "has_api_key": bool(config and config.api_key),
            "custom_ca": bool(config and (config.ca_file or config.ca_pem)),
            "allow_http": bool(config and config.allow_http),
            "timeout": config.timeout if config else 60}


class SummarySettings:
    def __init__(self, directory):
        self.path = Path(directory) / "summary-provider.json"

    def _read(self, fallback):
        if not self.path.exists():
            return fallback, {}
        with self.path.open("rb") as source:
            raw = source.read(MAX_PERSISTED_SETTINGS_BYTES + 1)
        if len(raw) > MAX_PERSISTED_SETTINGS_BYTES:
            raise ValueError("Invalid saved summary settings")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid saved summary settings")
        version = value.get("version")
        if type(version) is not int:
            raise ValueError("Invalid saved summary settings")
        if version == 1 and set(value) == {"version", "provider"}:
            registry = {}
        elif version == 2 and set(value) == {"version", "provider", "lobo_trust"}:
            registry = read_registry(value["lobo_trust"])
        else:
            raise ValueError("Invalid saved summary settings")
        provider = value["provider"]
        return (None if provider is None else SummaryProviderConfig(**provider)), registry

    def load(self, fallback):
        return self._read(fallback)[0]

    def trusted(self, current):
        try:
            _, registry = self._read(None)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return {}
        entry = pinned_provider(current)
        if entry is not None:
            registry[entry[0]] = entry[1]
        return registry

    def save(self, config, previous=None, forget=None):
        registry = self.trusted(previous)
        if forget is not None:
            registry.pop(forget, None)
        try:
            registry = remember_provider(registry, config)
            registry = read_registry(registry)
        except ValueError:
            raise ManagementError("lobo_trust_registry_full_or_invalid", 409) from None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = {"version": 1, "provider": asdict(config) if config else None}
        if registry:
            value.update(version=2, lobo_trust=registry)
        raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(raw) > MAX_PERSISTED_SETTINGS_BYTES:
            raise ValueError("Invalid saved summary settings")
        fd, name = tempfile.mkstemp(prefix=".summary-provider-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


class SummaryManagement:
    def __init__(self, directory, clock=time.monotonic):
        self.settings = SummarySettings(directory)
        self.clock = clock
        self.lock = threading.RLock()
        self.invalidate()

    def invalidate(self):
        with self.lock:
            self.code = None
            self.deadline = 0
            self.remaining = 0
            self.grant = None
            self.device = None
            self.candidate = None
            self.models = []
            self.tested = None

    def issue(self):
        with self.lock:
            self.invalidate()
            self.code = "".join(secrets.choice(ALPHABET) for _ in range(8))
            self.remaining = MAX_ATTEMPTS
            self.deadline = self.clock() + TTL_SECONDS
            return {"code": self.code[:4] + "-" + self.code[4:], "expires_in": TTL_SECONDS}

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode("utf-8")).digest()

    def redeem(self, code, device):
        with self.lock:
            if not self.code or self.clock() >= self.deadline or self.remaining <= 0:
                raise ManagementError("summary_admin_unavailable", 401)
            self.remaining -= 1
            if not isinstance(code, str) or not hmac.compare_digest(
                code.strip().upper().replace("-", "").encode("utf-8"), self.code.encode("ascii")
            ):
                raise ManagementError("invalid_summary_admin_code", 401)
            token = secrets.token_urlsafe(32)
            self.code = None
            self.grant = self.digest(token)
            self.device = self.digest(device)
            self.deadline = self.clock() + TTL_SECONDS
            return {"token": token, "expires_in": TTL_SECONDS}

    def require(self, token, device, guard):
        if (not self.grant or self.clock() >= self.deadline or not token
                or not hmac.compare_digest(self.grant, self.digest(token))
                or not hmac.compare_digest(self.device, self.digest(device)) or not guard()):
            raise ManagementError("summary_admin_unavailable", 401)

    def execute(self, action, body, token, device, guard, current, activate):
        with self.lock:
            self.require(token, device, guard)
            if action == "status":
                return {**describe(current), "trusted_lobo_endpoints": sorted(self.settings.trusted(current)), "lobo_tofu_supported": True}
            if action == "forget-lobo":
                if set(body) != {"base_url", "confirm"} or body["confirm"] is not True:
                    raise ManagementError("confirmation_required")
                endpoint = lobo_endpoint(body["base_url"])
                entry = pinned_provider(current)
                if entry is not None and entry[0] == endpoint:
                    raise ManagementError("lobo_trust_active", 409)
                if endpoint not in self.settings.trusted(current):
                    raise ManagementError("lobo_trust_not_found", 404)
                self.require(token, device, guard)
                self.settings.save(current, previous=current, forget=endpoint)
                self.candidate = None
                self.models = []
                self.tested = None
                return {**describe(current), "trusted_lobo_endpoints": sorted(self.settings.trusted(current)), "lobo_tofu_supported": True}
            if action == "cancel":
                self.invalidate()
                return {"cancelled": True}
            if action == "discover":
                self.candidate = None
                self.models = []
                self.tested = None
                if body.get("lobo_tofu") is True:
                    try:
                        self.settings._read(None)
                    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                        raise ManagementError("lobo_trust_storage_error", 409) from None
                candidate = self.build_candidate(body, current, self.settings.trusted(current))
                models = OpenAIChatProvider(candidate).models()
                self.require(token, device, guard)
                self.candidate = candidate
                self.models = models
                return {"models": models, "candidate": describe(candidate)}
            if action == "test":
                self.tested = None
                if self.candidate is None or set(body) != {"model"} or not isinstance(body["model"], str):
                    raise ManagementError("summary_candidate_required")
                config = replace(self.candidate, model=body["model"])
                if self.models and config.model not in self.models:
                    raise ManagementError("summary_model_not_discovered")
                provider = OpenAIChatProvider(config)
                start = time.monotonic()
                provider.complete(PROBE_SYSTEM, PROBE_TEXT)
                self.require(token, device, guard)
                self.tested = (config, provider)
                return {"ok": True, "model": config.model, "elapsed_ms": round((time.monotonic() - start) * 1000)}
            if action == "save":
                if body != {"confirm_egress": True} or body["confirm_egress"] is not True or self.tested is None:
                    raise ManagementError("tested_summary_candidate_required")
                config, provider = self.tested
                config = replace(config, cache_epoch=secrets.token_hex(16))
                provider = OpenAIChatProvider(config)
            elif action == "disable":
                if body != {"confirm": True} or body["confirm"] is not True:
                    raise ManagementError("confirmation_required")
                config, provider = None, None
            else:
                raise ManagementError("invalid_request")
            self.require(token, device, guard)
            self.settings.save(config, previous=current)
            activate(config, provider)
            self.invalidate()
            return describe(config)

    @staticmethod
    def build_candidate(body, current, trusted=None):
        if body == {"use_current": True} and body["use_current"] is True:
            if current is None:
                raise ManagementError("summary_provider_not_configured", 503)
            return current
        required = {"kind", "base_url", "allow_http"}
        if not required <= body.keys() or body.keys() - required - {"api_key", "lobo_auth", "lobo_tofu", "timeout"}:
            raise ManagementError("invalid_request")
        kind = body["kind"]
        if kind not in ("ollama", "lobo", "custom") or type(body["allow_http"]) is not bool:
            raise ManagementError("invalid_request")
        if not isinstance(body["base_url"], str) or not isinstance(body.get("api_key", ""), str):
            raise ManagementError("invalid_request")
        if len(body.get("api_key", "")) > 2048:
            raise ManagementError("invalid_request")
        config = SummaryProviderConfig(body["base_url"], "lobo-nano" if kind == "lobo" else "discovery",
            api_key=body.get("api_key", ""), allow_http=body["allow_http"], timeout=body.get("timeout", 120))
        if config.base_url.startswith("http://") and config.api_key:
            raise ManagementError("https_required_for_api_key")
        if "lobo_tofu" in body and (kind != "lobo" or body["lobo_tofu"] is not True or "lobo_auth" in body):
            raise ManagementError("invalid_request")
        if kind == "lobo":
            if config.allow_http or config.api_key:
                raise ManagementError("invalid_lobo_configuration")
            try:
                endpoint = lobo_endpoint(config.base_url)
            except (ValueError, TypeError):
                raise ManagementError("lobo_private_ipv4_required") from None
            if "lobo_auth" in body:
                pem = lobo_ca(endpoint, body["lobo_auth"])
            else:
                pem = (trusted or {}).get(endpoint)
                active = pinned_provider(current)
                if pem is None and active is not None and active[0] == endpoint:
                    pem = active[1]
                if body.get("lobo_tofu") is True:
                    if pem is not None:
                        raise ManagementError("lobo_already_remembered", 409)
                    pem = capture_lobo_ca(endpoint)
                elif pem is None:
                    raise ManagementError("lobo_identity_verification_required", 409)
            config = replace(config, ca_pem=pem)
        elif "lobo_auth" in body:
            raise ManagementError("invalid_request")
        return config
