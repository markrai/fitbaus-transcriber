from dataclasses import asdict
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .audiobookshelf import Audiobookshelf, SourceConfig, SourceError, valid_bearer
from .contract import AudioIdentity, Cue, SCHEMA, TranscriptWindow
from .engine import EngineConfig, EngineError, WhisperCpuEngine, transcribe_window
from .jobs import MAX_RESULT_BYTES, QueueError

POLL_SECONDS = 0.1
TERMINATION_GRACE = 2
SOURCE_ERRORS = frozenset(("unauthorized", "forbidden", "not_found", "stale_revision",
                           "source_failed", "upstream_unavailable", "invalid_manifest",
                           "invalid_response", "transfer_timeout", "size_limit", "incomplete_audio"))


def terminate_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=TERMINATION_GRACE)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def supervise(payload, timeout, cancelled, lock_fd, command=None, spawn=subprocess.Popen):
    if cancelled():
        raise EngineError("cancelled")
    command = command or [sys.executable, "-m", "transcriber.inference_process", str(os.getpid())]
    env = {key: value for key, value in os.environ.items()
           if key not in ("TRANSCRIBER_TOKEN", "TRANSCRIBER_TOKEN_FILE", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")}
    env.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
    deadline = time.monotonic() + timeout
    process = None
    finished = False
    try:
        process = spawn(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        start_new_session=True, pass_fds=(lock_fd,), env=env)
        request = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        while True:
            if cancelled():
                raise EngineError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineError("inference_timeout")
            try:
                output, _ = process.communicate(input=request, timeout=min(POLL_SECONDS, remaining))
                finished = True
                break
            except subprocess.TimeoutExpired:
                request = None
        if cancelled():
            raise EngineError("cancelled")
        if time.monotonic() >= deadline:
            raise EngineError("inference_timeout")
        if process.returncode != 0:
            raise EngineError("worker_crashed")
        if len(output) > MAX_RESULT_BYTES + 1024:
            raise QueueError("result_limit")
        try:
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError
            if set(result) == {"source_error"}:
                code = result["source_error"]
                raise SourceError(code if code in SOURCE_ERRORS else "source_failed")
            if result == {"error": "result_limit"}:
                raise QueueError("result_limit")
            if result == {"error": "engine_failed"}:
                raise EngineError("inference_failed")
            if set(result) != {"window"}:
                raise ValueError
            value = result["window"]
            if value["schema"] != SCHEMA:
                raise ValueError
            identity = AudioIdentity(value["source_id"], value["item_id"], value["audio_revision"])
            coverage = value["coverage"]
            window = TranscriptWindow(identity, coverage["start_ms"], coverage["end_ms"],
                                      tuple(Cue(c["start_ms"], c["end_ms"], c["text"]) for c in value["cues"]))
            expected = payload["identity"]
            if (asdict(window.identity), window.start_ms, window.end_ms) != (
                    expected, payload["start_ms"], payload["end_ms"]):
                raise ValueError
            return window
        except (KeyError, TypeError, ValueError, RecursionError):
            raise EngineError("invalid_worker_result") from None
    except OSError:
        raise EngineError("worker_unavailable") from None
    finally:
        if process is not None:
            if not finished:
                terminate_group(process)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()


def supervised_window(source, identity, token, directory, engine, start_ms, end_ms, cancelled, lock_fd):
    payload = {"source": asdict(source.config), "identity": asdict(identity), "token": token,
               "directory": str(directory), "engine": asdict(engine.config),
               "model": getattr(engine, "model_name", "tiny"),
               "start_ms": start_ms, "end_ms": end_ms}
    return supervise(payload, engine.config.inference_timeout, cancelled, lock_fd)


def parent_death_guard(parent):
    if os.getpgrp() != os.getpid():
        raise RuntimeError("Worker must have its own process group")
    def on_parent_death(signum, frame):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    signal.signal(signal.SIGTERM, on_parent_death)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot set parent death signal")
    if os.getppid() != parent:
        on_parent_death(None, None)


def worker(parent):
    parent_death_guard(parent)
    raw = sys.stdin.buffer.read(32769)
    if len(raw) > 32768:
        raise ValueError("Invalid worker request")
    request = json.loads(raw)
    if not valid_bearer(request["token"]):
        raise ValueError("Invalid source credential")
    output = sys.stdout
    sys.stdout = sys.stderr
    try:
        source = Audiobookshelf(SourceConfig(**request["source"]))
        identity = AudioIdentity(**request["identity"])
        engine = WhisperCpuEngine(Path(request["directory"]), EngineConfig(**request["engine"]),
                                  model_name=request.get("model", "tiny"))
        window = transcribe_window(source, identity, request["token"], Path(request["directory"]),
                                   engine, request["start_ms"], request["end_ms"])
        result = json.dumps(window.to_json(), ensure_ascii=True, separators=(",", ":"))
        response = '{"window":' + result + '}' if len(result.encode("ascii")) <= MAX_RESULT_BYTES else '{"error":"result_limit"}'
    except SourceError as error:
        response = json.dumps({"source_error": error.code if error.code in SOURCE_ERRORS else "source_failed"})
    except (EngineError, ValueError):
        response = '{"error":"engine_failed"}'
    output.write(response)
    output.flush()


if __name__ == "__main__":
    worker(int(sys.argv[1]))
