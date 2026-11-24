from dataclasses import asdict
import hashlib
import io
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import wave

from .engine import EngineConfig, EngineError, WhisperCpuEngine
from .inference_process import parent_death_guard, terminate_group
from .model import MODELS, model_directory, model_present, model_spec
from .resources import admission, fingerprint, snapshot

MAX_WAV_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 4096
RUNS = 2
LIVE_WINDOW_SECONDS = 60


def parse_sample(raw, probe=False):
    if len(raw) > MAX_WAV_BYTES:
        raise ValueError("Sample too large")
    with wave.open(io.BytesIO(raw), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("Expected 16 kHz mono PCM16 WAV")
        frames = audio.getnframes()
        if not (16000 <= frames <= 4800000) or (not probe and frames != 4800000):
            raise ValueError("Qualification requires exactly 300 seconds of audio")
        pcm = audio.readframes(frames)
        if len(pcm) != frames * 2:
            raise ValueError("Truncated audio sample")
    return pcm, frames * 1000 // 16000


def measure(data_dir, name, config, raw, lock_fd, timeout=120):
    started = time.monotonic()
    env = {key: value for key, value in os.environ.items()
           if key not in ("TRANSCRIBER_TOKEN", "TRANSCRIBER_TOKEN_FILE", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")}
    env.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
    command = [sys.executable, "-m", "transcriber.qualification", str(os.getpid()),
               str(data_dir), name, json.dumps(asdict(config))]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True,
                               pass_fds=(lock_fd,), env=env)
    finished = False
    try:
        output, _ = process.communicate(input=raw, timeout=timeout)
        finished = True
        elapsed = time.monotonic() - started
        if process.returncode != 0:
            raise EngineError("benchmark_worker_failed")
        if len(output) > MAX_OUTPUT_BYTES:
            raise EngineError("invalid_benchmark_result")
        value = json.loads(output)
        for key in ("load_seconds", "inference_seconds", "peak_rss_bytes", "cue_count", "word_count", "cue_coverage_ms"):
            number = value[key]
            if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                raise EngineError("invalid_benchmark_result")
        return {**value, "worker_seconds": round(elapsed, 3)}
    except subprocess.TimeoutExpired:
        raise EngineError("benchmark_timeout") from None
    except (ValueError, TypeError, KeyError):
        raise EngineError("invalid_benchmark_result") from None
    finally:
        if not finished:
            terminate_group(process)
        process.stdin.close()
        process.stdout.close()


def classify(runs, duration_ms, probe=False):
    if probe or duration_ms != 300000 or len(runs) != RUNS:
        return "unqualified"
    if any(r["word_count"] < 100 or r["cue_coverage_ms"] < 120000 for r in runs):
        return "insufficient_speech"
    worst = max(r["worker_seconds"] for r in runs)
    return "live_candidate" if worst <= LIVE_WINDOW_SECONDS else "background_candidate" if worst < 300 else "slow"


def record_path(data_dir, name):
    return Path(data_dir) / "qualification" / (model_spec(name).name + ".json")


def load_record(data_dir, name, config, resources):
    try:
        record = json.loads(record_path(data_dir, name).read_text())
        if record["fingerprint"] != fingerprint(name, config, resources):
            return {"state": "stale"}
        return record
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unqualified"}


def inventory(data_dir, config, resources=None):
    resources = resources or snapshot()
    result = []
    for name in MODELS:
        record = load_record(data_dir, name, config, resources)
        if not model_present(model_directory(data_dir, name)):
            record = {"state": "not_installed"}
        peak = record.get("peak_rss_bytes")
        result.append({"id": name, "qualification": record,
                       "admission": admission(name, resources, peak)})
    return {"resources": resources.to_json(), "models": result}


def qualify(data_dir, name, config, raw, lock_fd, probe=False, sampler=snapshot, runner=measure):
    model_spec(name)
    _, duration = parse_sample(raw, probe)
    resources = sampler()
    record = {"id": name, "fingerprint": fingerprint(name, config, resources),
              "sample_sha256": hashlib.sha256(raw).hexdigest(), "audio_ms": duration,
              "measured_at": int(time.time()), "runs": [], "state": "unqualified"}
    if not model_present(model_directory(data_dir, name)):
        record.update(state="not_installed", error="model_not_prepared")
        return record
    try:
        for _ in range(1 if probe else RUNS):
            gate = admission(name, sampler())
            if not gate["allowed"]:
                raise EngineError(gate["reason"])
            record["runs"].append(runner(data_dir, name, config, raw, lock_fd, config.inference_timeout))
        record["peak_rss_bytes"] = max(run["peak_rss_bytes"] for run in record["runs"])
        record["state"] = classify(record["runs"], duration, probe)
        record["admission"] = admission(name, sampler(), record["peak_rss_bytes"])
        if not record["admission"]["allowed"] and not probe:
            record["state"] = "insufficient_memory"
    except (EngineError, OSError) as error:
        record.update(state="failed", error=str(error) if isinstance(error, EngineError) else "benchmark_unavailable")
    if not probe:
        path = record_path(data_dir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    return record


def worker(parent, data_dir, name, config):
    parent_death_guard(parent)
    raw = sys.stdin.buffer.read(MAX_WAV_BYTES + 1)
    pcm, duration = parse_sample(raw, probe=True)
    output = sys.stdout
    sys.stdout = sys.stderr
    started = time.monotonic()
    engine = WhisperCpuEngine(Path(data_dir), config, model_name=name)
    engine._load()
    loaded = time.monotonic()
    cues = engine.transcribe(pcm, 0, duration)
    elapsed = time.monotonic() - loaded
    output.write(json.dumps({"load_seconds": round(loaded - started, 3),
                             "inference_seconds": round(elapsed, 3),
                             "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                             "cue_count": len(cues), "word_count": sum(len(c.text.split()) for c in cues),
                             "cue_coverage_ms": sum(c.end_ms - c.start_ms for c in cues)}))
    output.flush()


if __name__ == "__main__":
    worker(int(sys.argv[1]), sys.argv[2], sys.argv[3], EngineConfig(**json.loads(sys.argv[4])))
