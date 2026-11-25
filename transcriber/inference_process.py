from dataclasses import asdict
from contextlib import contextmanager
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
from .diagnostics import ENGINE_FAILURES, EXCEPTION_TYPES, exception_type, safe_exit_status
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


@contextmanager
def request_input(raw):
    with os.fdopen(os.memfd_create("fitbaus-worker-input", os.MFD_CLOEXEC), "w+b") as stream:
        stream.write(raw)
        stream.flush()
        stream.seek(0)
        yield stream


def supervise(payload, timeout, cancelled, lock_fd, command=None, spawn=subprocess.Popen, audio_fd=None):
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
        request = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(request) > 32768:
            raise EngineError("invalid_worker_result")
        with request_input(request) as stream:
            process = spawn(command, stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True, pass_fds=(lock_fd,) if audio_fd is None else (lock_fd, audio_fd), env=env)
        while True:
            if cancelled():
                raise EngineError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineError("inference_timeout")
            try:
                output, _ = process.communicate(timeout=min(POLL_SECONDS, remaining))
                finished = True
                break
            except subprocess.TimeoutExpired:
                continue
        if cancelled():
            raise EngineError("cancelled")
        if time.monotonic() >= deadline:
            raise EngineError("inference_timeout")
        if process.returncode != 0:
            raise EngineError("worker_crashed", exit_status=process.returncode)
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
            if set(result) == {"error", "exception_type", "exit_status"}:
                code = result["error"]
                detail = result["exception_type"]
                status = result["exit_status"]
                if (not isinstance(code, str) or code not in ENGINE_FAILURES
                        or (detail is not None and (not isinstance(detail, str) or detail not in EXCEPTION_TYPES))
                        or (status is not None and safe_exit_status(status) is None)):
                    raise ValueError
                raise EngineError(code, exception_type=detail, exit_status=status)
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
    except OSError as error:
        raise EngineError("worker_unavailable", exception_type=exception_type(error)) from None
    finally:
        if process is not None:
            if not finished:
                terminate_group(process)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()


def supervised_window(source, identity, token, directory, engine, start_ms, end_ms, cancelled, lock_fd, spool=None):
    payload = {"source": asdict(source.config), "identity": asdict(identity), "token": token,
               "directory": str(directory), "engine": asdict(engine.config),
               "model": getattr(engine, "model_name", "tiny"),
               "start_ms": start_ms, "end_ms": end_ms}
    audio_fd = None
    if spool is not None:
        audio_fd, file_id, ready = spool
        payload["spool"] = {"fd": audio_fd, "file_id": file_id, "ready": ready}
    return supervise(payload, engine.config.inference_timeout, cancelled, lock_fd, audio_fd=audio_fd)


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


class SpoolSource(Audiobookshelf):
    def __init__(self, config, spool):
        super().__init__(config)
        self.spool = spool

    @contextmanager
    def materialize_track(self, identity, file_id, token, directory):
        if file_id != self.spool["file_id"]:
            with super().materialize_track(identity, file_id, token, directory) as target:
                yield target
            return
        with os.fdopen(os.dup(self.spool["fd"]), "r+b") as target:
            if self.spool["ready"]:
                current = self.manifest(identity.item_id, token)
                if current.identity != identity:
                    raise SourceError("stale_revision")
                track = next((t for t in current.tracks if t.file_id == file_id), None)
                if track is None or os.fstat(target.fileno()).st_size != track.size:
                    raise SourceError("incomplete_audio")
                target.seek(0)
                yield target
            else:
                with super().materialize_track(identity, file_id, token, directory, target=target) as audio:
                    yield audio


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
        source_config = SourceConfig(**request["source"])
        source = SpoolSource(source_config, request["spool"]) if "spool" in request else Audiobookshelf(source_config)
        identity = AudioIdentity(**request["identity"])
        engine = WhisperCpuEngine(Path(request["directory"]), EngineConfig(**request["engine"]),
                                  model_name=request.get("model", "tiny"))
        window = transcribe_window(source, identity, request["token"], Path(request["directory"]),
                                   engine, request["start_ms"], request["end_ms"])
        result = json.dumps(window.to_json(), ensure_ascii=True, separators=(",", ":"))
        response = '{"window":' + result + '}' if len(result.encode("ascii")) <= MAX_RESULT_BYTES else '{"error":"result_limit"}'
    except SourceError as error:
        response = json.dumps({"source_error": error.code if error.code in SOURCE_ERRORS else "source_failed"})
    except EngineError as error:
        code = str(error)
        response = json.dumps({
            "error": code if code in ENGINE_FAILURES else "engine_failed",
            "exception_type": error.exception_type if error.exception_type in EXCEPTION_TYPES else None,
            "exit_status": safe_exit_status(error.exit_status),
        })
    except ValueError as error:
        response = json.dumps({"error": "invalid_transcript",
                               "exception_type": exception_type(error), "exit_status": None})
    output.write(response)
    output.flush()


if __name__ == "__main__":
    worker(int(sys.argv[1]))
