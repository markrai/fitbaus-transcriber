from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import threading
import time

from .audiobookshelf import SourceError, MAX_TRACK_BYTES
from .contract import AudioIdentity, Cue, MAX_WINDOW_MS, SCHEMA, TranscriptWindow
from .engine import EngineError, WhisperCpuEngine
from .inference_process import supervised_window
from .jobs import Job, JobSpec, MAX_RESULT_BYTES, QueueError
from .model import model_directory, model_present, model_spec
from .qualification import load_record
from .resources import admission, snapshot

MAX_PARENTS = 32
MAX_BOOK_MS = 100 * 60 * 60 * 1000
MAX_STAGE_BYTES = 128 * 1024 * 1024
CREDENTIAL_TTL = 600


def profile(name, config):
    return model_spec(name).engine_id + "-segments-v1:" + (config.language or "auto")


@dataclass(frozen=True)
class BookJob:
    identity: AudioIdentity
    model: str
    profile: str
    duration_ms: int
    start_ms: int
    cursor_ms: int
    state: str
    error: str | None

    @property
    def id(self):
        data = ["fitbaus-background-v1", self.identity.source_id, self.identity.item_id,
                self.identity.audio_revision, self.profile, self.start_ms, self.duration_ms]
        return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode("ascii")).hexdigest()

    def status(self, state=None):
        return {"job_id": self.id, "source_id": self.identity.source_id,
                "item_id": self.identity.item_id, "audio_revision": self.identity.audio_revision,
                "model": self.model, "profile": self.profile, "start_ms": self.start_ms,
                "end_ms": self.duration_ms, "processed_ms": self.cursor_ms - self.start_ms,
                "total_ms": self.duration_ms - self.start_ms, "next_ms": self.cursor_ms,
                "state": state or self.state, "error": self.error}


@dataclass(frozen=True)
class PreparedSpec(JobSpec):
    parent_id: str

    @property
    def id(self):
        return prepared_id(self.parent_id, self.start_ms, self.end_ms)


def prepared_id(parent_id, start, end):
    return hashlib.sha256(json.dumps(["fitbaus-prepared-v1", parent_id, start, end],
                                    separators=(",", ":")).encode("ascii")).hexdigest()


class BackgroundStore:
    def __init__(self, directory, max_parents=MAX_PARENTS, max_bytes=MAX_STAGE_BYTES):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "background.sqlite3"
        self.max_parents = max_parents
        self.max_bytes = max_bytes
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS books (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, item_id TEXT NOT NULL,
                revision TEXT NOT NULL, model TEXT NOT NULL, profile TEXT NOT NULL,
                duration_ms INTEGER NOT NULL, start_ms INTEGER NOT NULL, cursor_ms INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','paused','failed','completed','cancelled')),
                error TEXT, touched INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS windows (
                parent_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                result TEXT NOT NULL, result_bytes INTEGER NOT NULL,
                PRIMARY KEY(parent_id,start_ms))""")
            if "prepared_id" not in {row["name"] for row in db.execute("PRAGMA table_info(windows)")}:
                db.execute("ALTER TABLE windows ADD COLUMN prepared_id TEXT")
            for row in db.execute("SELECT parent_id,start_ms,end_ms FROM windows WHERE prepared_id IS NULL").fetchall():
                db.execute("UPDATE windows SET prepared_id=? WHERE parent_id=? AND start_ms=?",
                           (prepared_id(row["parent_id"], row["start_ms"], row["end_ms"]), row["parent_id"], row["start_ms"]))
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS prepared_window_id ON windows(prepared_id)")
            db.execute("CREATE INDEX IF NOT EXISTS prepared_book_identity ON books(source_id,item_id,revision)")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={max(1024, (self.max_bytes * 2 + 8 * 1024 * 1024) // page_size)}")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            db.rollback()
            raise QueueError("background_storage_error") from None
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def row(value):
        if value is None:
            raise QueueError("background_not_found")
        job = BookJob(AudioIdentity(value["source_id"], value["item_id"], value["revision"]),
                      value["model"], value["profile"], value["duration_ms"], value["start_ms"],
                      value["cursor_ms"], value["state"], value["error"])
        if job.id != value["id"] or not job.start_ms <= job.cursor_ms <= job.duration_ms:
            raise QueueError("background_storage_error")
        return job

    def get(self, job_id):
        with self.transaction() as db:
            return self.row(db.execute("SELECT * FROM books WHERE id=?", (job_id,)).fetchone())

    def submit(self, identity, name, model_profile, duration, start=0):
        if type(start) is not int or type(duration) is not int or not 0 <= start < duration <= MAX_BOOK_MS:
            raise ValueError("Invalid book interval")
        if start % MAX_WINDOW_MS:
            raise ValueError("Start must be window aligned")
        job = BookJob(identity, name, model_profile, duration, start, start, "pending", None)
        with self.transaction() as db:
            previous = db.execute("SELECT * FROM books WHERE id=?", (job.id,)).fetchone()
            if previous is not None:
                return self.row(previous)
            if db.execute("SELECT count(*) FROM books").fetchone()[0] >= self.max_parents:
                raise QueueError("background_full")
            db.execute("INSERT INTO books VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,?)",
                       (job.id, identity.source_id, identity.item_id, identity.audio_revision,
                        name, model_profile, duration, start, start, time.time_ns()))
        return job

    def for_item(self, source_id, item_id):
        with self.transaction() as db:
            return [self.row(row) for row in db.execute(
                "SELECT * FROM books WHERE source_id=? AND item_id=? ORDER BY touched DESC",
                (source_id, item_id))]

    def transition(self, job_id, state, error=None):
        with self.transaction() as db:
            current = self.row(db.execute("SELECT * FROM books WHERE id=?", (job_id,)).fetchone())
            if current.state in ("completed", "cancelled"):
                return current
            db.execute("UPDATE books SET state=?,error=?,touched=? WHERE id=?",
                       (state, error, time.time_ns(), job_id))
            return self.row(db.execute("SELECT * FROM books WHERE id=?", (job_id,)).fetchone())

    def checkpoint(self, job, window):
        if not isinstance(window, TranscriptWindow) or (
                window.identity, window.start_ms, window.end_ms) != (
                job.identity, job.cursor_ms, min(job.cursor_ms + MAX_WINDOW_MS, job.duration_ms)):
            raise QueueError("invalid_result")
        result = json.dumps(window.to_json(), ensure_ascii=True, separators=(",", ":"))
        size = len(result.encode("ascii"))
        if size > MAX_RESULT_BYTES:
            raise QueueError("result_limit")
        with self.transaction() as db:
            current = self.row(db.execute("SELECT * FROM books WHERE id=?", (job.id,)).fetchone())
            if current.state != "pending" or current.cursor_ms != job.cursor_ms:
                return current
            total = db.execute("SELECT coalesce(sum(result_bytes),0) FROM windows").fetchone()[0]
            if total + size > self.max_bytes:
                raise QueueError("background_storage_full")
            db.execute("INSERT INTO windows(parent_id,start_ms,end_ms,result,result_bytes,prepared_id) VALUES(?,?,?,?,?,?)",
                       (job.id, window.start_ms, window.end_ms, result, size,
                        prepared_id(job.id, window.start_ms, window.end_ms)))
            state = "completed" if window.end_ms == job.duration_ms else "pending"
            db.execute("UPDATE books SET cursor_ms=?,state=?,error=NULL,touched=? WHERE id=?",
                       (window.end_ms, state, time.time_ns(), job.id))
            return self.row(db.execute("SELECT * FROM books WHERE id=?", (job.id,)).fetchone())

    @staticmethod
    def prepared(row):
        if row is None:
            return None
        try:
            spec = PreparedSpec(AudioIdentity(row["source_id"], row["item_id"], row["revision"]),
                                row["window_start"], row["window_end"], row["profile"], row["id"])
            raw = row["result"]
            if len(raw.encode("ascii")) > MAX_RESULT_BYTES or spec.id != row["prepared_id"]:
                raise ValueError
            value = json.loads(raw)
            identity = AudioIdentity(value["source_id"], value["item_id"], value["audio_revision"])
            coverage = value["coverage"]
            window = TranscriptWindow(identity, coverage["start_ms"], coverage["end_ms"],
                                      tuple(Cue(c["start_ms"], c["end_ms"], c["text"]) for c in value["cues"]))
            if value["schema"] != SCHEMA or (identity, window.start_ms, window.end_ms) != (
                    spec.identity, spec.start_ms, spec.end_ms):
                raise ValueError
            return Job(spec, "ready", 0, None, raw)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise QueueError("background_storage_error") from None

    def find_prepared(self, spec, config):
        profiles = tuple(profile(name, config) for name in ("small", "base", "tiny"))
        with self.transaction() as db:
            row = db.execute("""
                SELECT b.*,w.start_ms AS window_start,w.end_ms AS window_end,w.result,w.prepared_id
                FROM books b JOIN windows w ON w.parent_id=b.id
                WHERE b.source_id=? AND b.item_id=? AND b.revision=? AND w.start_ms=? AND w.end_ms=?
                    AND b.profile IN (?,?,?) AND w.end_ms<=b.cursor_ms
                ORDER BY CASE b.profile WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END,b.id LIMIT 1
            """, (spec.identity.source_id, spec.identity.item_id, spec.identity.audio_revision,
                  spec.start_ms, spec.end_ms, *profiles, profiles[0], profiles[1])).fetchone()
            return self.prepared(row)

    def get_prepared(self, job_id):
        with self.transaction() as db:
            row = db.execute("""
                SELECT b.*,w.start_ms AS window_start,w.end_ms AS window_end,w.result,w.prepared_id
                FROM windows w JOIN books b ON b.id=w.parent_id
                WHERE w.prepared_id=? AND w.end_ms<=b.cursor_ms
            """, (job_id,)).fetchone()
            return self.prepared(row)

    def discard(self, job_id):
        with self.transaction() as db:
            job = self.row(db.execute("SELECT * FROM books WHERE id=?", (job_id,)).fetchone())
            if job.state not in ("paused", "failed", "cancelled", "completed"):
                raise QueueError("background_not_discardable")
            db.execute("DELETE FROM books WHERE id=?", (job_id,))


class Background:
    def __init__(self, runner, store, clock=time.monotonic, execute=supervised_window):
        self.runner = runner
        self.store = store
        self.config = runner.engine.config
        self.clock = clock
        self.execute = execute
        self.lock = threading.RLock()
        self.pending = OrderedDict()
        self.current = None
        self.control = None
        self.closed = False
        self.defer_until = 0
        self.spool = None
        self.spool_key = None
        self.spool_ready = False

    def authorize(self, job, token, exact=True):
        manifest = self.runner.source.manifest(job.identity.item_id, token)
        if (manifest.identity.source_id, manifest.identity.item_id) != (
                job.identity.source_id, job.identity.item_id):
            raise SourceError("not_found")
        if exact and (manifest.identity != job.identity or manifest.duration_ms != job.duration_ms):
            raise SourceError("stale_revision")
        if exact and job.profile != profile(job.model, self.config):
            raise QueueError("profile_changed")
        return manifest

    def check_model(self, name, require_memory=True):
        model_spec(name)
        directory = self.runner.directory
        if not model_present(model_directory(directory, name)):
            raise QueueError("model_not_prepared")
        resources = snapshot()
        record = load_record(directory, name, self.config, resources)
        if name != "tiny" and record.get("state") not in ("live_candidate", "background_candidate"):
            raise QueueError("model_not_qualified")
        gate = admission(name, resources, record.get("peak_rss_bytes"))
        if require_memory and not gate["allowed"]:
            raise QueueError("resource_unavailable")

    def submit(self, identity, name, token, start=0):
        manifest = self.runner.source.manifest(identity.item_id, token)
        if manifest.identity != identity:
            raise SourceError("stale_revision")
        self.check_model(name, require_memory=False)
        with self.lock:
            if self.closed:
                raise QueueError("worker_unavailable")
            job = self.store.submit(identity, name, profile(name, self.config), manifest.duration_ms, start)
            if job.state == "pending":
                self.pending[job.id] = (token, self.clock() + CREDENTIAL_TTL)
            return self.status(job)

    def status(self, job):
        with self.lock:
            job = self.store.get(job.id)
            state = job.state
            if state == "pending":
                state = ("processing" if self.current == job.id else "queued"
                         if job.id in self.pending and self.pending[job.id][1] > self.clock()
                         else "waiting_credentials")
                if state == "queued" and self.clock() < self.defer_until:
                    state = "waiting_resources"
            return job.status(state)

    def action(self, job_id, token, action):
        job = self.store.get(job_id)
        self.authorize(job, token, exact=action == "resume")
        if action == "resume" and job.state not in ("completed", "cancelled"):
            self.check_model(job.model, require_memory=False)
        with self.lock:
            if self.closed:
                raise QueueError("worker_unavailable")
            job = self.store.get(job_id)
            if action == "resume":
                if job.state not in ("completed", "cancelled"):
                    job = self.store.transition(job_id, "pending")
                    self.pending[job_id] = (token, self.clock() + CREDENTIAL_TTL)
            else:
                if action == "discard":
                    if self.current == job_id:
                        raise QueueError("background_busy")
                    self.store.discard(job_id)
                    self.pending.pop(job_id, None)
                    return {"job_id": job_id, "discarded": True}
                if self.current == job_id and self.control is not None:
                    self.control.cancel()
                self.pending.pop(job_id, None)
                if self.current is None and self.spool_key and self.spool_key[0] == job_id:
                    self.drop_spool()
                job = self.store.transition(job_id, "paused" if action == "pause" else "cancelled")
            return self.status(job)

    def expire(self):
        with self.lock:
            for job_id, (_, deadline) in list(self.pending.items()):
                if deadline <= self.clock():
                    self.pending.pop(job_id, None)
            if self.current is None and self.spool_key and self.spool_key[0] not in self.pending:
                self.drop_spool()

    def take(self, control):
        with self.lock:
            if self.closed or self.clock() < self.defer_until:
                return None
            for job_id, (token, deadline) in list(self.pending.items()):
                if deadline <= self.clock():
                    self.pending.pop(job_id, None)
                    continue
                job = self.store.get(job_id)
                if job.state != "pending":
                    self.pending.pop(job_id, None)
                    continue
                self.current, self.control = job_id, control
                return job, token
            if self.current is None:
                self.drop_spool()
            return None

    def drop_spool(self):
        if self.spool is not None:
            self.spool.close()
        self.spool = None
        self.spool_key = None
        self.spool_ready = False

    def prepare_spool(self, job, manifest, end, token):
        track = next((t for t in manifest.tracks if t.offset_ms <= job.cursor_ms
                      and end <= t.offset_ms + t.duration_ms), None)
        if track is None or track.size > MAX_TRACK_BYTES:
            self.drop_spool()
            return None
        key = (job.id, track.file_id, hashlib.sha256(token.encode("ascii")).digest())
        if self.spool_key != key:
            self.drop_spool()
            if shutil.disk_usage(self.runner.directory).free < track.size + 256 * 1024 * 1024:
                raise QueueError("background_disk_full")
            self.spool = tempfile.TemporaryFile(dir=self.runner.directory)
            self.spool_key = key
        return self.spool.fileno(), track.file_id, self.spool_ready

    def preempt(self):
        with self.lock:
            if self.control is not None:
                self.control.cancel()

    def step(self, job, token, control):
        try:
            with self.runner.store.worker() as fd:
                if control.cancelled():
                    return
                manifest = self.authorize(job, token)
                self.check_model(job.model)
                engine = WhisperCpuEngine(self.runner.directory, self.config, model_name=job.model)
                end = min(job.cursor_ms + MAX_WINDOW_MS, job.duration_ms)
                spool = self.prepare_spool(job, manifest, end, token) if self.execute is supervised_window else None
                args = (self.runner.source, job.identity, token, self.runner.directory,
                        engine, job.cursor_ms, end, control.cancelled, fd)
                window = self.execute(*args, spool=spool) if spool is not None else self.execute(*args)
                if spool is not None:
                    self.spool_ready = True
                self.authorize(job, token)
                with self.lock:
                    if not control.cancelled():
                        current = self.store.checkpoint(job, window)
                        if current.state == "completed":
                            self.pending.pop(job.id, None)
        except (SourceError, EngineError, QueueError, OSError) as error:
            with self.lock:
                if not control.cancelled():
                    code = str(error) if isinstance(error, (SourceError, EngineError, QueueError)) else "background_unavailable"
                    if code in ("worker_busy", "resource_unavailable"):
                        self.defer_until = self.clock() + 5
                    elif code in ("unauthorized", "forbidden"):
                        pending = self.pending.get(job.id)
                        if pending is not None and pending[0] == token:
                            self.pending.pop(job.id, None)
                        self.drop_spool()
                    else:
                        self.store.transition(job.id, "failed", code)
                        self.pending.pop(job.id, None)
        except Exception:
            with self.lock:
                if not control.cancelled():
                    self.store.transition(job.id, "failed", "internal_error")
                    self.pending.pop(job.id, None)
        finally:
            with self.lock:
                self.current = None
                self.control = None
                if not self.spool_ready or job.id not in self.pending or self.closed:
                    self.drop_spool()
            with control.gate:
                control.finished = True

    def close(self):
        with self.lock:
            self.closed = True
            self.pending.clear()
            if self.control is not None:
                self.control.cancel()
            else:
                self.drop_spool()
