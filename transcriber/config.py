from dataclasses import dataclass
import os
from pathlib import Path

from .audiobookshelf import SourceConfig
from .engine import EngineConfig
from .jobs import QueueLimits
from .summary_provider import SummaryProviderConfig


@dataclass(frozen=True)
class Config:
    token: str
    bind: str
    port: int
    data_dir: Path
    source: SourceConfig | None = None
    engine: EngineConfig = EngineConfig()
    queue: QueueLimits = QueueLimits()
    model_quota_mib: int = 1024
    summary_provider: SummaryProviderConfig | None = None

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        token_file = env.get("TRANSCRIBER_TOKEN_FILE", "")
        token = env.get("TRANSCRIBER_TOKEN", "")
        if token_file and token:
            raise ValueError("Configure only one token source")
        if token_file:
            with open(token_file, encoding="utf-8") as source:
                token = source.read(257).strip()
        if not 32 <= len(token) <= 256 or not all(
            c.isascii() and (c.isalnum() or c in "-_") for c in token
        ):
            raise ValueError("Configure a random 32-256 character API token")
        port = int(env.get("TRANSCRIBER_PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid service port")
        data_dir = Path(env.get("TRANSCRIBER_DATA_DIR", "/data"))
        if not data_dir.is_absolute():
            raise ValueError("Data directory must be absolute")
        source_url = env.get("AUDIOBOOKSHELF_URL", "")
        source_id = env.get("AUDIOBOOKSHELF_SOURCE_ID", "")
        if bool(source_url) != bool(source_id):
            raise ValueError("Configure both Audiobookshelf URL and source ID")
        source = SourceConfig(source_id, source_url) if source_url else None
        language = env.get("TRANSCRIBER_LANGUAGE", "auto")
        engine = EngineConfig(
            cpu_threads=int(env.get("TRANSCRIBER_CPU_THREADS", "2")),
            language=None if language == "auto" else language,
            decode_timeout=int(env.get("TRANSCRIBER_DECODE_TIMEOUT", "120")),
            inference_timeout=int(env.get("TRANSCRIBER_INFERENCE_TIMEOUT", "120")),
        )
        queue = QueueLimits(
            max_jobs=int(env.get("TRANSCRIBER_MAX_JOBS", "512")),
            max_active=int(env.get("TRANSCRIBER_MAX_ACTIVE_JOBS", "64")),
            cache_bytes=int(env.get("TRANSCRIBER_CACHE_MIB", "128")) * 1024 * 1024,
        )
        model_quota = int(env.get("TRANSCRIBER_MODEL_QUOTA_MIB", "1024"))
        if not 128 <= model_quota <= 4096:
            raise ValueError("Optional model quota must be between 128 and 4096 MiB")
        return cls(token, env.get("TRANSCRIBER_BIND", "127.0.0.1"), port, data_dir, source, engine, queue, model_quota,
                   SummaryProviderConfig.from_env(env))
