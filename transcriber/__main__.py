import argparse
import getpass
import json
import sys
import signal
import tempfile
import http.client

from .audiobookshelf import Audiobookshelf, SourceError, valid_bearer
from .config import Config
from .engine import EngineError, WhisperCpuEngine
from .jobs import JobStore, QueueError
from .runner import JobRunner
from .model import MODELS, prepare_model
from .qualification import MAX_WAV_BYTES, inventory, qualify
from .server import Service


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fitbaus-transcriber")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve")
    commands.add_parser("prepare-model")
    commands.add_parser("models")
    commands.add_parser("qualification")
    bench = commands.add_parser("benchmark")
    bench.add_argument("--model", choices=("all", *MODELS), default="all")
    bench.add_argument("--wav-stdin", action="store_true", required=True)
    bench.add_argument("--probe", action="store_true")
    for name in ("download-model", "cancel-model-download"):
        command = commands.add_parser(name)
        command.add_argument("model", choices=("base", "small"))
    commands.add_parser("pair")
    commands.add_parser("revoke-pairings")
    discard = commands.add_parser("discard-job")
    discard.add_argument("--job-id", required=True)
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
        if args.command in ("pair", "revoke-pairings"):
            path = "/v1/pairing/issue" if args.command == "pair" else "/v1/pairing/revoke-all"
            client = http.client.HTTPConnection("127.0.0.1", config.port, timeout=5)
            try:
                client.request("POST", path, headers={"Authorization": "Bearer " + config.token})
                response = client.getresponse()
                if response.status != 200:
                    raise ValueError("Pairing management failed")
                result = json.loads(response.read(4096))
                if args.command == "pair":
                    print(result["code"])
                    print("Valid for 10 minutes; single use. Keep private.", file=sys.stderr)
                else:
                    print("All paired device credentials revoked.")
            finally:
                client.close()
        elif args.command in ("models", "download-model", "cancel-model-download"):
            route = "/v1/models" if args.command == "models" else (
                "/v1/models/" + args.model + ("/download" if args.command == "download-model" else "/cancel"))
            client = http.client.HTTPConnection("127.0.0.1", config.port, timeout=10)
            try:
                client.request("GET" if args.command == "models" else "POST", route,
                               headers={"Authorization": "Bearer " + config.token})
                response = client.getresponse()
                result = json.loads(response.read(16384))
                print(json.dumps(result, indent=2))
                if response.status not in (200, 202):
                    return 1
            finally:
                client.close()
        elif args.command == "qualification":
            print(json.dumps(inventory(config.data_dir, config.engine), indent=2))
        elif args.command == "benchmark":
            raw = sys.stdin.buffer.read(MAX_WAV_BYTES + 1)
            store = JobStore(config.data_dir / "queue", config.queue)
            results = []
            with store.worker() as lock_fd:
                for name in MODELS if args.model == "all" else (args.model,):
                    print("Benchmarking " + name, file=sys.stderr, flush=True)
                    results.append(qualify(config.data_dir, name, config.engine, raw, lock_fd, args.probe))
            print(json.dumps({"models": results}, indent=2))
            if any(r["state"] in ("failed", "not_installed", "insufficient_memory") for r in results):
                return 1
        elif args.command == "prepare-model":
            prepare_model(config.data_dir)
            print("Whisper CPU model prepared", file=sys.stderr)
        elif args.command == "discard-job":
            store = JobStore(config.data_dir / "queue", config.queue)
            with store.worker():
                store.cancel(args.job_id)
            print(json.dumps({"job_id": args.job_id, "cancelled": True}))
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
                def stop(signum, frame):
                    raise KeyboardInterrupt
                previous = signal.signal(signal.SIGTERM, stop)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    signal.signal(signal.SIGTERM, previous)
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
