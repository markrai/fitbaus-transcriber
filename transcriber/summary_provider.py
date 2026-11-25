from dataclasses import dataclass, field
from http.client import HTTPException
import json
import re
import ssl
import time
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from .audiobookshelf import NoRedirect


MAX_INPUT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_OUTPUT_CHARS = 8000
MAX_OUTPUT_TOKENS = 1024
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}")


@dataclass(frozen=True)
class SummaryProviderConfig:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    allow_http: bool = False
    timeout: int = 60
    cache_epoch: str = "1"
    ca_file: str = ""
    ca_pem: str = field(default="", repr=False)

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (
            url.scheme not in ("http", "https") or not url.hostname
            or url.username is not None or url.password is not None
            or "?" in self.base_url or "#" in self.base_url
            or any(c.isspace() or ord(c) < 32 or c == "\\" for c in self.base_url)
            or (url.port is not None and not 1 <= url.port <= 65535)
            or not url.path.rstrip("/").endswith("/v1")
            or any(part in (".", "..") for part in url.path.split("/"))
            or len(self.base_url) > 2048
        ):
            raise ValueError("Invalid summary provider base URL; expected a /v1 endpoint")
        if url.scheme == "http" and not self.allow_http:
            raise ValueError("HTTP summary provider requires explicit opt-in")
        if not MODEL_PATTERN.fullmatch(self.model):
            raise ValueError("Invalid summary provider model")
        if len(self.api_key) > 8192 or any(not 33 <= ord(c) <= 126 for c in self.api_key):
            raise ValueError("Invalid summary provider API key")
        if not isinstance(self.cache_epoch, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.cache_epoch):
            raise ValueError("Invalid summary cache epoch")
        if type(self.timeout) is not int or not 5 <= self.timeout <= 120:
            raise ValueError("Summary provider timeout must be between 5 and 120 seconds")
        if not isinstance(self.ca_file, str) or (self.ca_file and (
            url.scheme != "https" or not Path(self.ca_file).is_absolute()
        )):
            raise ValueError("Summary CA requires HTTPS and an absolute certificate path")
        if not isinstance(self.ca_pem, str) or (self.ca_pem and (
            url.scheme != "https" or self.ca_file or len(self.ca_pem) > 16384
        )):
            raise ValueError("Invalid pinned summary provider CA")
        if self.ca_file or self.ca_pem:
            self.tls_context()
        object.__setattr__(self, "base_url", urlunsplit((url.scheme, url.netloc, url.path.rstrip("/"), "", "")))

    def tls_context(self):
        if self.ca_pem:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            try:
                context.load_verify_locations(cadata=self.ca_pem)
            except (OSError, ValueError):
                raise ValueError("Invalid pinned summary provider CA") from None
            return context
        context = ssl.create_default_context()
        if self.ca_file:
            try:
                context.load_verify_locations(cafile=self.ca_file)
            except (OSError, ValueError):
                raise ValueError("Invalid summary provider CA certificate") from None
        return context

    @classmethod
    def from_env(cls, env):
        base_url = env.get("TRANSCRIBER_SUMMARY_BASE_URL", "")
        model = env.get("TRANSCRIBER_SUMMARY_MODEL", "")
        key = env.get("TRANSCRIBER_SUMMARY_API_KEY", "")
        allow_http = env.get("TRANSCRIBER_SUMMARY_ALLOW_HTTP", "false")
        if allow_http not in ("true", "false"):
            raise ValueError("Summary HTTP opt-in must be true or false")
        ca_file = env.get("TRANSCRIBER_SUMMARY_CA_FILE", "")
        if not base_url and not model and not key and not ca_file:
            return None
        if not base_url or not model:
            raise ValueError("Configure both summary provider URL and model")
        return cls(base_url, model, key, allow_http == "true", int(env.get("TRANSCRIBER_SUMMARY_TIMEOUT", "60")),
                   env.get("TRANSCRIBER_SUMMARY_CACHE_EPOCH", "1"), ca_file)


def summary_capability(config):
    if config is None:
        return {"configured": False}
    return {"configured": True, "protocol": "openai-chat/v1", "model": config.model}


class SummaryProviderError(Exception):
    pass


class SummaryProvider(Protocol):
    def complete(self, system: str, text: str) -> str: ...


class OpenAIChatProvider:
    def __init__(self, config: SummaryProviderConfig):
        self.config = config
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=config.tls_context()))

    def complete(self, system: str, text: str) -> str:
        if not isinstance(system, str) or not isinstance(text, str) or not system.strip() or not text.strip():
            raise ValueError("Summary provider requires nonempty text")
        if len(system.encode("utf-8")) + len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("Summary provider input exceeds limit")
        payload = json.dumps({
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
            "stream": False,
            "max_tokens": MAX_OUTPUT_TOKENS,
        }, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        request = Request(self.config.base_url + "/chat/completions", data=payload, headers=headers, method="POST")
        body = self._read(request)
        try:
            choice = json.loads(body)["choices"][0]
            message = choice["message"]
            content = message["content"]
            if choice.get("finish_reason") != "stop" or message.get("tool_calls") or not isinstance(content, str):
                raise ValueError()
            content = content.strip()
            if not content or len(content) > MAX_OUTPUT_CHARS:
                raise ValueError()
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
            raise SummaryProviderError("invalid_completion") from None
        return content

    def models(self):
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        body = self._read(Request(self.config.base_url + "/models", headers=headers, method="GET"))
        try:
            models = json.loads(body)["data"]
            if not isinstance(models, list) or len(models) > 512:
                raise ValueError()
            result = set()
            for model in models:
                name = model["id"]
                if not isinstance(name, str) or not MODEL_PATTERN.fullmatch(name):
                    raise ValueError()
                result.add(name)
            return sorted(result)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
            raise SummaryProviderError("invalid_response") from None

    def _read(self, request):
        deadline = time.monotonic() + self.config.timeout
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                if response.status != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise SummaryProviderError("invalid_response")
                body = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise SummaryProviderError("provider_timeout")
                    chunk = response.read1(min(16384, MAX_RESPONSE_BYTES - len(body) + 1))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise SummaryProviderError("response_too_large")
        except HTTPError as error:
            status = error.code
            error.close()
            code = {401: "provider_authentication", 403: "provider_authentication", 429: "provider_rate_limited"}.get(status, "provider_http_error")
            raise SummaryProviderError(code) from None
        except (OSError, URLError, HTTPException):
            raise SummaryProviderError("provider_unavailable") from None
        return body
