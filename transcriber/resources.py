from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform

from .model import model_spec

MIB = 1024 * 1024
MODEL_FLOOR_MIB = {"tiny": 768, "base": 896, "small": 1536}
RESERVE_MIB = 128


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def numeric(value):
    try:
        n = int(value)
        return n if 0 <= n < 2**60 else None
    except (ValueError, TypeError):
        return None


def entries(value):
    result = {}
    for line in value.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result[parts[0].rstrip(":")] = numeric(parts[1])
    return result


@dataclass(frozen=True)
class Resources:
    memory_limit: int | None
    memory_used: int | None
    host_available: int | None
    cpu_quota: float | None
    affinity_cpus: int
    machine: str
    cpu_model: str

    @property
    def available(self):
        limits = []
        if self.host_available is not None:
            limits.append(self.host_available)
        if self.memory_limit is not None and self.memory_used is not None:
            limits.append(max(0, self.memory_limit - self.memory_used))
        return min(limits) if limits else None

    def to_json(self):
        return {**asdict(self), "available_bytes": self.available}


def snapshot(cgroup=Path("/sys/fs/cgroup"), proc=Path("/proc")):
    memory_limit = numeric(read_text(cgroup / "memory.max"))
    memory_used = numeric(read_text(cgroup / "memory.current"))
    stat = entries(read_text(cgroup / "memory.stat"))
    if memory_used is not None:
        memory_used = max(0, memory_used - (stat.get("inactive_file") or 0))
    if not (cgroup / "memory.max").exists():
        memory_limit = numeric(read_text(cgroup / "memory/memory.limit_in_bytes"))
        memory_used = numeric(read_text(cgroup / "memory/memory.usage_in_bytes"))
        stat = entries(read_text(cgroup / "memory/memory.stat"))
        if memory_used is not None:
            memory_used = max(0, memory_used - (stat.get("total_inactive_file") or 0))
    available = entries(read_text(proc / "meminfo")).get("MemAvailable")
    cpu = read_text(cgroup / "cpu.max").split()
    if len(cpu) == 2:
        quota, period = map(numeric, cpu)
    else:
        quota = numeric(read_text(cgroup / "cpu/cpu.cfs_quota_us"))
        period = numeric(read_text(cgroup / "cpu/cpu.cfs_period_us"))
    model = next((line.split(":", 1)[1].strip() for line in read_text(proc / "cpuinfo").splitlines()
                  if line.startswith("model name") and ":" in line), platform.processor())
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    return Resources(memory_limit, memory_used, available * 1024 if available is not None else None,
                     quota / period if quota is not None and period else None,
                     affinity, platform.machine(), model)


def required_bytes(name, peak_rss_bytes=None):
    model_spec(name)
    measured = (peak_rss_bytes * 5 + 3) // 4 if peak_rss_bytes is not None else 0
    return max(MODEL_FLOOR_MIB[name] * MIB, measured) + RESERVE_MIB * MIB


def admission(name, resources, peak_rss_bytes=None):
    needed = required_bytes(name, peak_rss_bytes)
    available = resources.available
    return {"allowed": available is not None and available >= needed,
            "required_bytes": needed, "available_bytes": available,
            "reason": "memory_unknown" if available is None else
                      "insufficient_memory" if available < needed else None}


def fingerprint(name, config, resources):
    versions = {}
    for package in ("faster-whisper", "ctranslate2", "numpy", "onnxruntime"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "missing"
    value = {"version": 1, "engine": model_spec(name).engine_id, "versions": versions,
             "cpu_threads": config.cpu_threads, "language": config.language,
             "inference_timeout": config.inference_timeout, "decode_timeout": config.decode_timeout,
             "machine": resources.machine, "cpu_model": resources.cpu_model,
             "affinity_cpus": resources.affinity_cpus, "cpu_quota": resources.cpu_quota,
             "memory_limit": resources.memory_limit}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
