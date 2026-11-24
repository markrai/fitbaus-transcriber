import argparse
import getpass
import json
import sys
import tempfile

from .audiobookshelf import Audiobookshelf, SourceError, valid_bearer
from .config import Config
from .engine import EngineError, WhisperCpuEngine
from .jobs import JobStore, QueueError
from .runner import JobRunner
from .model import prepare_model
from .server import Service


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fitbaus-transcriber")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve")
    commands.add_parser("prepare-model")
    for name in ("transcribe", "submit"):
        run = commands.add_parser(name)
        run.add_argument("--item-id", required=True)
        run.add_argument("--start-ms", type=int, required=True)
        run.add_argument("--end-ms", type=int, required=True)
        run.add_argument("--token-stdin", action="store_true")
    for name in ("run-job", "read-job"):
        run = commands.add_parser(name)
        run.add_argument("--job-id", required=True)
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
        elif args.command in ("transcribe", "submit", "run-job", "read-job"):
            if config.source is None:
                raise ValueError("Audiobookshelf source not configured")
            if not args.token_stdin and not sys.stdin.isatty():
                raise ValueError("Use --token-stdin for noninteractive credentials")
            token = (sys.stdin.readline(8194).rstrip("\r\n") if args.token_stdin
                     else getpass.getpass("Audiobookshelf access token or API key: "))
            if not valid_bearer(token):
                raise ValueError("Invalid Audiobookshelf credential")
            source = Audiobookshelf(config.source)
            engine = WhisperCpuEngine(config.data_dir, config.engine)
            store = JobStore(config.data_dir / "queue", config.queue)
            runner = JobRunner(store, source, engine, config.data_dir)
            if args.command in ("submit", "transcribe"):
                job = runner.submit(args.item_id, token, args.start_ms, args.end_ms)
                if args.command == "submit":
                    output = {"job_id": job.spec.id, "state": job.state}
                else:
                    runner.run(job.spec.id, token)
                    output = runner.result(job.spec.id, token)
            elif args.command == "run-job":
                runner.run(args.job_id, token)
                output = runner.result(args.job_id, token)
            else:
                output = runner.result(args.job_id, token)
            print(json.dumps(output, ensure_ascii=True, separators=(",", ":")))
        else:
            with Service((config.bind, config.port), config) as server:
                print("FitBaus Transcriber listening", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
    except (SourceError, EngineError, QueueError) as error:
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
