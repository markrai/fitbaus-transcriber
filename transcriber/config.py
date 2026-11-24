from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Config:
    token: str
    bind: str
    port: int
    data_dir: Path

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
        return cls(token, env.get("TRANSCRIBER_BIND", "127.0.0.1"), port, data_dir)
