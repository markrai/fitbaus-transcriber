import json
import threading

from .audiobookshelf import SourceError
from .engine import ENGINE_ID, EngineError
from .inference_process import supervised_window
from .jobs import JobSpec, QueueError
from .qualification import load_record
from .resources import admission, snapshot


def engine_profile(config):
    return ENGINE_ID + "-segments-v1:" + (config.language or "auto")


class RunControl:
    def __init__(self):
        self.gate = threading.Lock()
        self.event = threading.Event()
        self.finished = False

    def cancel(self):
        with self.gate:
            if self.finished:
                return False
            self.event.set()
            return True

    def cancelled(self):
        return self.event.is_set()


class JobRunner:
    def __init__(self, store, source, engine, directory, transcribe=None):
        self.store = store
        self.source = source
        self.engine = engine
        self.directory = directory
        self.transcribe = transcribe

    def submit(self, item_id, token, start_ms, end_ms, expected_revision=None, expected_source=None):
        manifest = self.source.manifest(item_id, token)
        if expected_source is not None and manifest.identity.source_id != expected_source:
            raise SourceError("not_found")
        if expected_revision is not None and manifest.identity.audio_revision != expected_revision:
            raise SourceError("stale_revision")
        spec = JobSpec(manifest.identity, start_ms, end_ms, engine_profile(self.engine.config))
        if end_ms > manifest.duration_ms:
            raise ValueError("Transcript window exceeds audiobook")
        return self.store.submit(spec)

    def _authorize(self, job, token):
        manifest = self.source.manifest(job.spec.identity.item_id, token)
        if manifest.identity != job.spec.identity:
            raise SourceError("stale_revision")
        if job.spec.profile != engine_profile(self.engine.config):
            raise QueueError("profile_changed")

    def run(self, job_id, token, control=None):
        control = control or RunControl()
        claimed = False
        try:
            with self.store.worker() as lock_fd:
                try:
                    job = self.store.get(job_id)
                    self._authorize(job, token)
                    with control.gate:
                        job = self.store.claim(job_id)
                        if job.state == "ready":
                            control.finished = True
                            return job
                        claimed = True
                        if control.cancelled():
                            raise EngineError("cancelled")
                    args = (self.source, job.spec.identity, token, self.directory,
                            self.engine, job.spec.start_ms, job.spec.end_ms)
                    if self.transcribe is None:
                        resources = snapshot()
                        record = load_record(self.directory, "tiny", self.engine.config, resources)
                        gate = admission("tiny", resources, record.get("peak_rss_bytes"))
                        if not gate["allowed"] and gate["reason"] != "memory_unknown":
                            raise EngineError("resource_unavailable")
                        window = supervised_window(*args, control.cancelled, lock_fd)
                    else:
                        window = self.transcribe(*args)
                    if control.cancelled():
                        raise EngineError("cancelled")
                    self._authorize(job, token)
                    with control.gate:
                        if control.cancelled():
                            raise EngineError("cancelled")
                        completed = self.store.complete(job_id, window)
                        control.finished = True
                        return completed
                except Exception as error:
                    with control.gate:
                        if claimed:
                            if control.cancelled():
                                code = "cancelled"
                            elif isinstance(error, SourceError):
                                code = "source_failed"
                            elif isinstance(error, EngineError):
                                code = str(error) if str(error) in (
                                    "inference_timeout", "worker_crashed", "worker_unavailable",
                                    "invalid_worker_result", "cancelled", "resource_unavailable") else "engine_failed"
                            elif isinstance(error, QueueError) and str(error) == "result_limit":
                                code = "result_limit"
                            else:
                                code = "internal_error"
                            self.store.fail(job_id, code)
                        elif control.cancelled():
                            try:
                                self.store.cancel(job_id)
                            except QueueError:
                                pass
                        control.finished = True
                    raise
        finally:
            with control.gate:
                control.finished = True

    def status(self, job_id, token):
        job = self.store.get(job_id)
        self._authorize(job, token)
        return job

    def cancellable(self, job_id, token):
        job = self.store.get(job_id)
        manifest = self.source.manifest(job.spec.identity.item_id, token)
        if (manifest.identity.source_id, manifest.identity.item_id) != (
            job.spec.identity.source_id, job.spec.identity.item_id
        ):
            raise SourceError("not_found")
        return job

    def result(self, job_id, token):
        job = self.status(job_id, token)
        if job.state != "ready":
            raise QueueError("job_not_ready")
        self.store.touch(job_id)
        return json.loads(job.result)
