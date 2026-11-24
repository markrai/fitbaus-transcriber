import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from .model import MODELS, MODEL_FILES, model_directory, model_present, model_spec

MIB = 1024 * 1024
MAX_DOWNLOAD_SECONDS = 1800
RESERVE_BYTES = 256 * MIB


class DownloadError(Exception):
    pass


def write_status(root, name, state, received=0, total=None, error=None):
    path = root / (name + ".status.json")
    temporary = root / (name + ".status.tmp")
    temporary.write_text(json.dumps({"state": state, "received_bytes": received,
                                     "total_bytes": total, "error": error}), encoding="utf-8")
    temporary.replace(path)


def read_status(root, name):
    try:
        return json.loads((root / (name + ".status.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "not_installed", "received_bytes": 0, "total_bytes": None, "error": None}


def optional_usage(data_dir):
    root = Path(data_dir) / "models"
    return sum(p.stat().st_size for p in root.glob("*/*")
               if p.is_file() and not p.parent.name.startswith("tiny-"))


def verify_file(path, size, digest, lfs):
    if path.stat().st_size != size:
        raise DownloadError("verification_failed")
    check = hashlib.sha256() if lfs else hashlib.sha1()
    if not lfs:
        check.update(f"blob {size}\0".encode())
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(MIB), b""):
            check.update(chunk)
    if check.hexdigest() != digest:
        raise DownloadError("verification_failed")


def fetch_model(data_dir, name, quota_bytes):
    from huggingface_hub import HfApi, hf_hub_url
    import urllib.request

    spec = model_spec(name)
    if spec.bundled:
        raise DownloadError("bundled_model")
    root = Path(data_dir) / "models"
    destination = model_directory(data_dir, name)
    staging = root / (".download-" + name)
    received = 0
    total = None
    try:
        if destination.exists():
            raise DownloadError("incomplete_installation")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(mode=0o700)
        info = HfApi(endpoint="https://huggingface.co", token=False).model_info(
            spec.repo, revision=spec.revision, files_metadata=True, token=False, timeout=30)
        if info.sha != spec.revision:
            raise DownloadError("verification_failed")
        files = {f.rfilename: f for f in info.siblings or []}
        manifest = []
        for filename in MODEL_FILES:
            entry = files.get(filename)
            if entry is None or type(entry.size) is not int or entry.size <= 0:
                raise DownloadError("verification_failed")
            lfs = entry.lfs is not None
            digest = entry.lfs.sha256 if lfs else entry.blob_id
            if not isinstance(digest, str) or not re.fullmatch(
                    r"[0-9a-f]{64}" if lfs else r"[0-9a-f]{40}", digest):
                raise DownloadError("verification_failed")
            manifest.append((filename, entry.size, digest, lfs))
        total = sum(size for _, size, _, _ in manifest)
        if total > spec.approximate_bytes + 16 * MIB or optional_usage(data_dir) + total > quota_bytes:
            raise DownloadError("model_quota_exceeded")
        if shutil.disk_usage(root).free < total + RESERVE_BYTES:
            raise DownloadError("insufficient_disk")
        write_status(root, name, "downloading", received, total)
        for filename, size, digest, lfs in manifest:
            url = hf_hub_url(spec.repo, filename, revision=spec.revision, endpoint="https://huggingface.co")
            path = staging / filename
            count = 0
            with urllib.request.urlopen(url, timeout=30) as response, path.open("xb") as output:
                if not response.geturl().startswith("https://"):
                    raise DownloadError("verification_failed")
                while True:
                    chunk = response.read(MIB)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > size:
                        raise DownloadError("verification_failed")
                    if shutil.disk_usage(root).free < RESERVE_BYTES + len(chunk):
                        raise DownloadError("insufficient_disk")
                    output.write(chunk)
                    received += len(chunk)
                    write_status(root, name, "downloading", received, total)
                output.flush()
                os.fsync(output.fileno())
            verify_file(path, size, digest, lfs)
        write_status(root, name, "verifying", received, total)
        if not model_present(staging):
            raise DownloadError("verification_failed")
        staging.rename(destination)
        write_status(root, name, "installed", received, total)
    except DownloadError as error:
        write_status(root, name, "failed", received, total, str(error))
        raise
    except Exception:
        write_status(root, name, "failed", received, total, "download_failed")
        raise DownloadError("download_failed") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


class ModelDownloads:
    def __init__(self, data_dir, quota_mib=1024, timeout=MAX_DOWNLOAD_SECONDS, spawn=subprocess.Popen):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "models"
        self.quota_bytes = quota_mib * MIB
        self.timeout = timeout
        self.spawn = spawn
        self.lock = threading.RLock()
        self.process = None
        self.thread = None
        self.active = None
        self.closed = False
        self.cancelled = False

    def inventory(self):
        with self.lock:
            result = []
            for spec in MODELS.values():
                installed = model_present(model_directory(self.data_dir, spec.name))
                status = read_status(self.root, spec.name)
                if installed:
                    status.update(state="installed", error=None)
                elif status["state"] in ("downloading", "verifying") and self.active != spec.name:
                    status.update(state="interrupted", error="download_interrupted")
                result.append({"id": spec.name, "revision": spec.revision, "bundled": spec.bundled,
                               "installed": installed, "approximate_bytes": spec.approximate_bytes,
                               "usage": spec.usage, **status})
            return result

    def start(self, name):
        spec = model_spec(name)
        with self.lock:
            if self.closed:
                raise DownloadError("download_unavailable")
            if model_present(model_directory(self.data_dir, name)):
                return False
            if spec.bundled:
                raise DownloadError("bundled_model")
            if self.active:
                if self.active == name:
                    return True
                raise DownloadError("download_busy")
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.root / ".download.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise DownloadError("download_busy") from None
                for item in MODELS.values():
                    if not item.bundled:
                        shutil.rmtree(self.root / (".download-" + item.name), ignore_errors=True)
                if optional_usage(self.data_dir) + spec.approximate_bytes > self.quota_bytes:
                    raise DownloadError("model_quota_exceeded")
                if shutil.disk_usage(self.root).free < spec.approximate_bytes + RESERVE_BYTES:
                    raise DownloadError("insufficient_disk")
                write_status(self.root, name, "downloading")
                self.process = self.spawn(
                    [sys.executable, "-m", "transcriber.model_downloads", str(self.data_dir), name, str(self.quota_bytes)],
                    pass_fds=(fd,), start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env={**os.environ, "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1"})
                self.active = name
                self.cancelled = False
                self.thread = threading.Thread(target=self._monitor, args=(name, self.process, fd), daemon=True)
                self.thread.start()
            except Exception:
                os.close(fd)
                raise
            return True

    @staticmethod
    def _terminate(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def _monitor(self, name, process, fd):
        timed_out = False
        try:
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
            with self.lock:
                if not model_present(model_directory(self.data_dir, name)):
                    status = read_status(self.root, name)
                    error = ("download_cancelled" if self.cancelled else "download_timeout" if timed_out
                             else status.get("error") or "download_failed")
                    write_status(self.root, name, "cancelled" if self.cancelled else "failed",
                                 status["received_bytes"], status["total_bytes"], error)
                shutil.rmtree(self.root / (".download-" + name), ignore_errors=True)
        finally:
            with self.lock:
                os.close(fd)
                self.active = None
                self.process = None

    def cancel(self, name):
        model_spec(name)
        with self.lock:
            if self.active != name:
                return False
            self.cancelled = True
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process = self.process
        self._terminate(process)
        return True

    def close(self):
        with self.lock:
            self.closed = True
            name = self.active
            thread = self.thread
        if name is not None:
            self.cancel(name)
        if thread is not None:
            thread.join(timeout=5)


if __name__ == "__main__":
    try:
        fetch_model(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]))
    except Exception:
        raise SystemExit(1)
