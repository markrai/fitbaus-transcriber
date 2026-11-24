from collections import OrderedDict
import threading
import time

from .jobs import MAX_ATTEMPTS, QueueError

CREDENTIAL_TTL_SECONDS = 600


class Dispatcher:
    def __init__(self, runner, capacity, credential_ttl=CREDENTIAL_TTL_SECONDS, clock=time.monotonic):
        self.runner = runner
        self.capacity = capacity
        self.credential_ttl = credential_ttl
        self.clock = clock
        self.pending = OrderedDict()
        self.current = None
        self.closed = False
        self.condition = threading.Condition()
        self.thread = threading.Thread(target=self._work, name="transcriber-worker", daemon=True)
        self.thread.start()

    def _expire(self):
        now = self.clock()
        for job_id, (_, deadline) in list(self.pending.items()):
            if deadline <= now:
                del self.pending[job_id]

    def schedule(self, job, token, retry=False):
        job_id = job.spec.id
        with self.condition:
            self._expire()
            if self.closed:
                raise QueueError("worker_unavailable")
            job = self.runner.store.get(job_id)
            if job.state == "ready" or job_id == self.current:
                return job
            if job.attempts >= MAX_ATTEMPTS:
                return job
            if job.state == "failed" and not retry:
                return job
            if job_id not in self.pending and len(self.pending) + int(self.current is not None) >= self.capacity:
                raise QueueError("queue_full")
            self.pending[job_id] = (token, self.clock() + self.credential_ttl)
            self.condition.notify()
            return job

    def dispatch_state(self, job):
        with self.condition:
            self._expire()
            if job.spec.id == self.current:
                return "processing"
            if job.spec.id in self.pending:
                return "scheduled"
            if job.state in ("queued", "running"):
                return "awaiting_credentials" if job.state == "queued" else "external_or_interrupted"
            return "idle"

    def cancel(self, job_id, token):
        job = self.runner.status(job_id, token)
        with self.condition:
            if job_id == self.current:
                raise QueueError("job_not_cancellable")
            self.runner.store.cancel(job.spec.id)
            self.pending.pop(job_id, None)

    def _work(self):
        while True:
            with self.condition:
                self._expire()
                while not self.closed and not self.pending:
                    self.condition.wait(timeout=1)
                    self._expire()
                if self.closed:
                    return
                job_id, (token, deadline) = self.pending.popitem(last=False)
                if deadline <= self.clock():
                    continue
                self.current = job_id
            try:
                self.runner.run(job_id, token)
            except Exception:
                pass
            finally:
                token = None
                with self.condition:
                    self.current = None

    def close(self):
        with self.condition:
            self.closed = True
            self.pending.clear()
            self.condition.notify_all()
        self.thread.join(timeout=2)
