from collections import OrderedDict
import threading
import time

from .jobs import MAX_ATTEMPTS, QueueError
from .runner import RunControl

CREDENTIAL_TTL_SECONDS = 600


class Dispatcher:
    def __init__(self, runner, capacity, credential_ttl=CREDENTIAL_TTL_SECONDS, clock=time.monotonic, background=None):
        self.runner = runner
        self.background = background
        self.capacity = capacity
        self.credential_ttl = credential_ttl
        self.clock = clock
        self.pending = OrderedDict()
        self.current = None
        self.control = None
        self.closed = False
        self.condition = threading.Condition()
        self.thread = threading.Thread(target=self._work, name="transcriber-worker", daemon=True)
        self.thread.start()

    def _expire(self):
        if self.background is not None:
            self.background.expire()
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
            if job.state == "ready":
                return job, False
            if job_id == self.current:
                return job, True
            if job.attempts >= MAX_ATTEMPTS:
                return job, False
            if job.state == "failed" and not retry:
                return job, False
            if job_id not in self.pending and len(self.pending) + int(self.current is not None) >= self.capacity:
                raise QueueError("queue_full")
            self.pending[job_id] = (token, self.clock() + self.credential_ttl)
            if self.background is not None:
                self.background.preempt()
            self.condition.notify()
            return job, True

    def dispatch_state(self, job):
        with self.condition:
            self._expire()
            if job.state == "ready":
                return "idle"
            if job.spec.id == self.current:
                return "processing"
            if job.spec.id in self.pending:
                return "scheduled"
            if job.state in ("queued", "running"):
                return "awaiting_credentials" if job.state == "queued" else "external_or_interrupted"
            return "idle"

    def cancel(self, job_id, token):
        job = self.runner.cancellable(job_id, token)
        with self.condition:
            if job_id == self.current:
                if self.control is None or not self.control.cancel():
                    raise QueueError("job_not_cancellable")
                return True
            self.runner.store.cancel(job.spec.id)
            self.pending.pop(job_id, None)
            return False

    def wake(self):
        with self.condition:
            self.condition.notify()

    def _work(self):
        while True:
            background_work = None
            with self.condition:
                while not self.closed:
                    self._expire()
                    if self.pending:
                        job_id, (token, deadline) = self.pending.popitem(last=False)
                        if deadline <= self.clock():
                            continue
                        self.current = job_id
                        self.control = RunControl()
                        control = self.control
                        break
                    if self.background is not None:
                        control = RunControl()
                        try:
                            background_work = self.background.take(control)
                        except Exception:
                            background_work = None
                        if background_work is not None:
                            break
                    self.condition.wait(timeout=1)
                if self.closed:
                    return
            if background_work is not None:
                job, token = background_work
                try:
                    self.background.step(job, token, control)
                except Exception:
                    pass
                finally:
                    token = None
                    background_work = None
                continue
            try:
                self.runner.run(job_id, token, control)
            except Exception:
                pass
            finally:
                token = None
                with self.condition:
                    self.current = None
                    self.control = None

    def close(self):
        with self.condition:
            self.closed = True
            self.pending.clear()
            if self.background is not None:
                self.background.close()
            if self.control is not None:
                self.control.cancel()
            self.condition.notify_all()
        self.thread.join(timeout=5)
