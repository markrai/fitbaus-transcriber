from pathlib import Path
import threading
import time
import wave

from .engine import EngineError
from .jobs import QueueError
from .model import model_directory, model_present
from .qualification import AUTOMATIC_SAMPLE, MAX_WAV_BYTES, RUNS, inventory, load_record, qualify
from .resources import admission, snapshot


OPTIONAL_MODELS = ("base", "small")
QUALIFIED = ("live_candidate", "background_candidate")
AUTO_STATES = ("unqualified", "stale")


class AutomaticQualification:
    def __init__(self, runner, sample=AUTOMATIC_SAMPLE, clock=time.monotonic,
                 sampler=snapshot, execute=qualify, installed=None):
        self.runner = runner
        self.directory = runner.directory
        self.config = runner.engine.config
        self.sample = Path(sample)
        self.clock = clock
        self.sampler = sampler
        self.execute = execute
        self.installed = installed or (lambda name: model_present(model_directory(self.directory, name)))
        self.lock = threading.RLock()
        self.current = None
        self.control = None
        self.completed_runs = 0
        self.retries = set()
        self.errors = {}
        self.next_scan = 0
        self.closed = False

    @property
    def available(self):
        return not self.closed and self.sample.is_file()

    def retry(self, name):
        if name not in OPTIONAL_MODELS:
            raise ValueError("unsupported_model")
        with self.lock:
            if not self.available:
                raise QueueError("qualification_unavailable")
            if not self.installed(name):
                raise QueueError("model_not_prepared")
            record = load_record(self.directory, name, self.config, self.sampler())
            if record.get("state") in QUALIFIED:
                return False
            if self.current != name:
                self.errors.pop(name, None)
                self.retries.add(name)
                self.next_scan = 0
            return True

    def eligible(self, name, record):
        return self.installed(name) and record.get("state") not in QUALIFIED and (
            name in self.retries or (record.get("state") in AUTO_STATES and name not in self.errors))

    def inventory(self):
        resources = self.sampler()
        result = inventory(self.directory, self.config, resources)
        with self.lock:
            for model in result["models"]:
                name = model["id"]
                state = "idle"
                if name in OPTIONAL_MODELS and self.installed(name):
                    if self.current == name:
                        state = "running"
                    elif not self.available:
                        state = "unavailable"
                    elif self.eligible(name, model["qualification"]):
                        state = "queued" if model["admission"]["allowed"] else "waiting_resources"
                    elif model["qualification"].get("state") not in QUALIFIED:
                        state = "failed"
                model["automatic"] = {
                    "state": state,
                    "completed_runs": self.completed_runs if self.current == name else 0,
                    "total_runs": RUNS,
                    "error": self.errors.get(name) or model["qualification"].get("error"),
                }
        return result

    def take(self, control):
        with self.lock:
            if not self.available or self.clock() < self.next_scan:
                return None
            self.next_scan = self.clock() + 5
            resources = self.sampler()
            for name in OPTIONAL_MODELS:
                if not self.installed(name):
                    continue
                record = load_record(self.directory, name, self.config, resources)
                if not self.eligible(name, record):
                    continue
                if not admission(name, resources, record.get("peak_rss_bytes"))["allowed"]:
                    continue
                self.current = name
                self.control = control
                self.completed_runs = 0
                return name
            return None

    def progress(self, count):
        with self.lock:
            self.completed_runs = count

    def step(self, name, control):
        retry = False
        error = None
        try:
            with self.runner.store.worker() as fd:
                if control.cancelled():
                    retry = True
                    return
                resources = self.sampler()
                record = load_record(self.directory, name, self.config, resources)
                if record.get("state") in QUALIFIED or not self.installed(name):
                    return
                if not admission(name, resources, record.get("peak_rss_bytes"))["allowed"]:
                    retry = True
                    return
                with self.sample.open("rb") as file:
                    raw = file.read(MAX_WAV_BYTES + 1)
                self.execute(self.directory, name, self.config, raw, fd, sampler=self.sampler,
                             control=control, automatic=True, on_run=self.progress)
        except QueueError as exc:
            retry = str(exc) == "worker_busy"
            if not retry:
                error = "qualification_unavailable"
        except EngineError as exc:
            retry = control.cancelled() or str(exc) == "cancelled"
            if not retry:
                error = "qualification_failed"
        except (OSError, ValueError, wave.Error, EOFError):
            error = "qualification_sample_invalid"
        except Exception:
            error = "qualification_unavailable"
        finally:
            with self.lock:
                if not retry and not control.cancelled():
                    self.retries.discard(name)
                    if error:
                        self.errors[name] = error
                self.current = None
                self.control = None
                self.completed_runs = 0
                self.next_scan = self.clock() + (5 if retry else 0)
            with control.gate:
                control.finished = True

    def preempt(self):
        with self.lock:
            if self.control is not None:
                self.control.cancel()

    def close(self):
        with self.lock:
            self.closed = True
            self.retries.clear()
            self.preempt()
