import argparse
import getpass
import json
import sys
import tempfile

from .audiobookshelf import Audiobookshelf, SourceError, valid_bearer
from .config import Config
from .engine import EngineError, WhisperCpuEngine, transcribe_window
from .model import prepare_model
from .server import Service


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fitbaus-transcriber")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve")
    commands.add_parser("prepare-model")
    run = commands.add_parser("transcribe")
    run.add_argument("--item-id", required=True)
    run.add_argument("--start-ms", type=int, required=True)
    run.add_argument("--end-ms", type=int, required=True)
    run.add_argument("--token-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=config.data_dir):
            pass
        if args.command == "prepare-model":
            prepare_model(config.data_dir)
            print("Whisper CPU model prepared", file=sys.stderr)
        elif args.command == "transcribe":
            if config.source is None:
                raise ValueError("Audiobookshelf source not configured")
            token = (sys.stdin.readline(8194).rstrip("\r\n") if args.token_stdin
                     else getpass.getpass("Audiobookshelf access token or API key: "))
            if not valid_bearer(token):
                raise ValueError("Invalid Audiobookshelf credential")
            source = Audiobookshelf(config.source)
            identity = source.manifest(args.item_id, token).identity
            engine = WhisperCpuEngine(config.data_dir, config.engine)
            window = transcribe_window(source, identity, token, config.data_dir, engine,
                                       args.start_ms, args.end_ms)
            print(json.dumps(window.to_json(), ensure_ascii=True, separators=(",", ":")))
        else:
            with Service((config.bind, config.port), config) as server:
                print("FitBaus Transcriber listening", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
    except (SourceError, EngineError) as error:
        print("Transcription failed: " + str(error), file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError):
        print("Operation failed: check configuration, model, source and data directory", file=sys.stderr)
        return 1
    except Exception:
        print("Operation failed: model preparation or runtime error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
