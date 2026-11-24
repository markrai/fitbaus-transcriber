import os
import sys
import tempfile

from .config import Config
from .server import Service


def main():
    try:
        config = Config.from_env()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=config.data_dir):
            pass
        with Service((config.bind, config.port), config) as server:
            print("FitBaus Transcriber foundation listening", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except (ValueError, OSError):
        print("Startup failed: check configuration, bind address and data directory", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
