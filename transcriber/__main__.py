import argparse
import getpass
import json
import sys
import signal
import tempfile
import http.client
import re
import warnings

from .audiobookshelf import Audiobookshelf, SourceError, valid_bearer, identifier
from .config import Config
from .engine import EngineError, WhisperCpuEngine
from .jobs import JobStore, QueueError
from .runner import JobRunner
from .background import BackgroundStore
from .model import MODELS, prepare_model, model_directory, model_present
from .qualification import MAX_WAV_BYTES, inventory, qualify
from .server import Service


def source_credential(config, token_stdin, renewable=False):
    refresh = None
    if config.source is None:
        raise ValueError("Audiobookshelf source not configured")
    if token_stdin:
        token = sys.stdin.readline(8194).rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            raise ValueError("Use --token-stdin for noninteractive credentials")
        print("Audiobookshelf authentication", file=sys.stderr)
        print("  1) Username and password (default)", file=sys.stderr)
        print("  2) Access token or API key", file=sys.stderr)
        choice = input("Choose [1/2]: ").strip()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            if choice in ("", "1"):
                if config.source.base_url.startswith("http://"):
                    print("Warning: credentials will travel over unencrypted HTTP to the configured Audiobookshelf server.", file=sys.stderr)
                    if input("Continue on your trusted private network? [y/N]: ").strip().lower() != "y":
                        raise ValueError("Login cancelled")
                username = input("Audiobookshelf username: ").strip()
                password = getpass.getpass("Audiobookshelf password: ")
                try:
                    source = Audiobookshelf(config.source)
                    if renewable:
                        token, refresh = source.login_session(username, password)
                    else:
                        token = source.login(username, password)
                finally:
                    del password
            elif choice == "2":
                token = getpass.getpass("Audiobookshelf access token or API key: ")
            else:
                raise ValueError("Invalid authentication choice")
    if not valid_bearer(token):
        raise ValueError("Invalid Audiobookshelf credential")
    return (token, refresh) if renewable else token


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fitbaus-transcriber")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve")
    commands.add_parser("check-config")
    commands.add_parser("doctor")
    source_check = commands.add_parser("check-source")
    source_check.add_argument("--token-stdin", action="store_true")
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
    commands.add_parser("summary-admin")
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
    for name in ("background-start", "background-list", "background-status", "background-resume",
                 "background-pause", "background-cancel", "background-discard"):
        command = commands.add_parser(name)
        if name in ("background-start", "background-list"):
            command.add_argument("--item-id", required=True)
        else:
            command.add_argument("--job-id", required=True)
        if name == "background-start":
            command.add_argument("--model", choices=tuple(MODELS), default="tiny")
            command.add_argument("--start-ms", type=int, default=0)
        command.add_argument("--token-stdin", action="store_true")
        if name in ("background-start", "background-resume"):
            command.add_argument("--unattended", action="store_true",
                                 help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=config.data_dir):
            pass
        if args.command == "check-config":
            if config.source is None:
                raise ValueError("Audiobookshelf source not configured")
            if not model_present(model_directory(config.data_dir)):
                raise ValueError("Bundled Tiny model is unavailable")
            print("Configuration, data directory and bundled model checks passed.")
        elif args.command == "doctor":
            client = http.client.HTTPConnection("127.0.0.1", config.port, timeout=5)
            try:
                client.request("GET", "/readyz")
                response = client.getresponse()
                ready = response.status == 200
                readiness = json.loads(response.read(4096))
            finally:
                client.close()
            print(json.dumps({"ready": ready, "readiness": readiness,
                              "source_configured": config.source is not None,
                              "models": inventory(config.data_dir, config.engine)["models"]}, indent=2))
            if not ready:
                return 1
        elif args.command == "check-source":
            token = source_credential(config, args.token_stdin)
            count = Audiobookshelf(config.source).check_access(token)
            print(json.dumps({"source_access": "ok", "book_libraries": count}))
            if count == 0:
                print("No accessible book libraries. Check Audiobookshelf permissions.", file=sys.stderr)
                return 1
        elif args.command in ("pair", "revoke-pairings", "summary-admin"):
            path = {"pair": "/v1/pairing/issue", "revoke-pairings": "/v1/pairing/revoke-all",
                    "summary-admin": "/v1/admin/summary-provider/issue"}[args.command]
            client = http.client.HTTPConnection("127.0.0.1", config.port, timeout=5)
            try:
                client.request("POST", path, headers={"Authorization": "Bearer " + config.token})
                response = client.getresponse()
                if response.status != 200:
                    raise ValueError("Pairing management failed")
                result = json.loads(response.read(4096))
                if args.command == "summary-admin":
                    print(result["code"])
                    print("Provider administration code: use only over a trusted private network. "
                          "The bike API may use unencrypted HTTP. Valid for 10 minutes; single use.", file=sys.stderr)
                elif args.command == "pair":
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
        elif args.command and args.command.startswith("background-"):
            renewable = args.command in ("background-start", "background-resume")
            refresh = None
            if renewable:
                print("Authorizing this background job until completion or stop. Restart requires login.", file=sys.stderr)
                token, refresh = source_credential(config, args.token_stdin, renewable=True)
            else:
                token = source_credential(config, args.token_stdin)
            headers = {"Authorization": "Bearer " + config.token, "X-Audiobookshelf-Token": token}
            def request(method, route, body=None):
                conn = http.client.HTTPConnection("127.0.0.1", config.port, timeout=60)
                try:
                    request_headers = dict(headers)
                    if renewable and method == "POST" and refresh is not None:
                        request_headers["X-FitBaus-Background-Refresh"] = refresh
                    if body is not None:
                        request_headers["Content-Type"] = "application/json"
                        body = json.dumps(body)
                    conn.request(method, route, body=body, headers=request_headers)
                    response = conn.getresponse()
                    value = json.loads(response.read(262144))
                    if response.status not in (200, 202):
                        raise QueueError(value.get("error", "background_unavailable"))
                    return value
                finally:
                    conn.close()
            action = args.command.removeprefix("background-")
            if action == "start":
                item = identifier(args.item_id)
                manifest = request("GET", "/v1/items/" + item + "/manifest")
                body = {key: manifest[key] for key in ("source_id", "item_id", "audio_revision")}
                body.update(model=args.model, start_ms=args.start_ms)
                result = request("POST", "/v1/background", body)
            elif action == "list":
                result = request("GET", "/v1/items/" + identifier(args.item_id) + "/background")
            else:
                if not re.fullmatch(r"[0-9a-f]{64}", args.job_id):
                    raise ValueError("Invalid job ID")
                route = "/v1/background/" + args.job_id
                result = request("GET", route) if action == "status" else request("POST", route + "/" + action)
            print(json.dumps(result, indent=2))
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
            token = source_credential(config, args.token_stdin)
            source = Audiobookshelf(config.source)
            engine = WhisperCpuEngine(config.data_dir, config.engine)
            store = JobStore(config.data_dir / "queue", config.queue)
            runner = JobRunner(store, source, engine, config.data_dir,
                               prepared=BackgroundStore(config.data_dir / "background"))
            if args.command in ("submit", "transcribe"):
                job = runner.submit(args.item_id, token, args.start_ms, args.end_ms)
                if args.command == "submit":
                    output = {"job_id": job.spec.id, "state": job.state, "profile": job.spec.profile,
                              "prepared": hasattr(job.spec, "parent_id")}
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
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Credential entry cancelled or hidden input unavailable", file=sys.stderr)
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
