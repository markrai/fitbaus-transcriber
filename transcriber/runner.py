import json

from .audiobookshelf import SourceError
from .engine import ENGINE_ID, EngineError, transcribe_window
from .jobs import JobSpec, QueueError


def engine_profile(config):
    return ENGINE_ID + "-segments-v1:" + (config.language or "auto")


class JobRunner:
    def __init__(self, store, source, engine, directory, transcribe=transcribe_window):
        self.store = store
        self.source = source
        self.engine = engine
        self.directory = directory
        self.transcribe = transcribe

    def submit(self, item_id, token, start_ms, end_ms):
        manifest = self.source.manifest(item_id, token)
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

    def run(self, job_id, token):
        with self.store.worker():
            job = self.store.get(job_id)
            self._authorize(job, token)
            job = self.store.claim(job_id)
            if job.state == "ready":
                return job
            try:
                window = self.transcribe(self.source, job.spec.identity, token, self.directory,
                                         self.engine, job.spec.start_ms, job.spec.end_ms)
                self._authorize(job, token)
                return self.store.complete(job_id, window)
            except SourceError:
                self.store.fail(job_id, "source_failed")
                raise
            except EngineError:
                self.store.fail(job_id, "engine_failed")
                raise
            except QueueError as error:
                self.store.fail(job_id, "result_limit" if str(error) == "result_limit" else "internal_error")
                raise
            except Exception:
                self.store.fail(job_id, "internal_error")
                raise

    def result(self, job_id, token):
        job = self.store.get(job_id)
        self._authorize(job, token)
        if job.state != "ready":
            raise QueueError("job_not_ready")
        self.store.touch(job_id)
        return json.loads(job.result)
