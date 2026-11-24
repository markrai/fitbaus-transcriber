from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .contract import AudioIdentity, MAX_TIME_MS, MAX_WINDOW_MS, TranscriptWindow

MAX_RESULT_BYTES = 1024 * 1024
MAX_ATTEMPTS = 3
_PROFILE = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


class QueueError(Exception):
    pass


@dataclass(frozen=True)
class QueueLimits:
    max_jobs: int = 512
    max_active: int = 64
    cache_bytes: int = 128 * 1024 * 1024

    def __post_init__(self):
        if not 1 <= self.max_active <= self.max_jobs <= 4096:
            raise ValueError("Invalid job limits")
        if not MAX_RESULT_BYTES <= self.cache_bytes <= 512 * 1024 * 1024:
            raise ValueError("Invalid cache byte limit")


@dataclass(frozen=True)
class JobSpec:
    identity: AudioIdentity
    start_ms: int
    end_ms: int
    profile: str

    def __post_init__(self):
        if (type(self.start_ms) is not int or type(self.end_ms) is not int
                or not 0 <= self.start_ms < self.end_ms <= MAX_TIME_MS
                or self.end_ms - self.start_ms > MAX_WINDOW_MS):
            raise ValueError("Invalid transcript window")
        if not isinstance(self.profile, str) or not _PROFILE.fullmatch(self.profile):
            raise ValueError("Invalid engine profile")

    @property
    def id(self):
        value = ["fitbaus-job-v1", self.identity.source_id, self.identity.item_id,
                 self.identity.audio_revision, self.start_ms, self.end_ms, self.profile]
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Job:
    spec: JobSpec
    state: str
    attempts: int
    error: str | None
    result: str | None


class JobStore:
    def __init__(self, directory, limits=QueueLimits()):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "jobs.sqlite3"
        self.limits = limits
        with self._transaction() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    profile TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','ready','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    result TEXT,
                    result_bytes INTEGER NOT NULL DEFAULT 0,
                    touched INTEGER NOT NULL,
                    CHECK((state = 'ready' AND result IS NOT NULL AND result_bytes > 0)
                        OR (state != 'ready' AND result IS NULL AND result_bytes = 0))
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS jobs_state_age ON jobs(state, touched, id)")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={(self.limits.cache_bytes * 2 + 16 * 1024 * 1024) // page_size}")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            db.rollback()
            raise QueueError("queue_storage_error") from None
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def worker(self):
        with (self.directory / "worker.lock").open("ab") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise QueueError("worker_busy") from None
            try:
                with self._transaction() as db:
                    db.execute("""
                        UPDATE jobs SET state=CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END,
                            error='interrupted', touched=? WHERE state='running'
                    """, (MAX_ATTEMPTS, time.time_ns()))
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _job(row):
        if row is None:
            raise QueueError("job_not_found")
        spec = JobSpec(AudioIdentity(row["source_id"], row["item_id"], row["revision"]),
                       row["start_ms"], row["end_ms"], row["profile"])
        if spec.id != row["id"]:
            raise QueueError("queue_storage_error")
        return Job(spec, row["state"], row["attempts"], row["error"], row["result"])

    def _get(self, db, job_id):
        return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def get(self, job_id):
        with self._transaction() as db:
            return self._get(db, job_id)

    def touch(self, job_id):
        with self._transaction() as db:
            db.execute("UPDATE jobs SET touched=? WHERE id=? AND state='ready'", (time.time_ns(), job_id))

    @staticmethod
    def _evict(db, exclude=None):
        row = db.execute("""
            SELECT id FROM jobs WHERE state IN ('ready','failed') AND (? IS NULL OR id != ?)
            ORDER BY touched, id LIMIT 1
        """, (exclude, exclude)).fetchone()
        if row is None:
            return False
        db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
        return True

    def submit(self, spec):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (spec.id,)).fetchone()
            if row is not None:
                db.execute("UPDATE jobs SET touched=? WHERE id=?", (time.time_ns(), spec.id))
                return self._job(row)
            active = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            if active >= self.limits.max_active:
                raise QueueError("queue_full")
            count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
            while count >= self.limits.max_jobs:
                if not self._evict(db):
                    raise QueueError("queue_full")
                count -= 1
            identity = spec.identity
            db.execute("""
                INSERT INTO jobs(id,source_id,item_id,revision,start_ms,end_ms,profile,state,touched)
                VALUES(?,?,?,?,?,?,?,'queued',?)
            """, (spec.id, identity.source_id, identity.item_id, identity.audio_revision,
                  spec.start_ms, spec.end_ms, spec.profile, time.time_ns()))
            return self._get(db, spec.id)

    def cancel(self, job_id):
        with self._transaction() as db:
            job = self._get(db, job_id)
            if job.state not in ("queued", "failed"):
                raise QueueError("job_not_cancellable")
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def claim(self, job_id):
        with self._transaction() as db:
            job = self._get(db, job_id)
            if job.state == "ready":
                db.execute("UPDATE jobs SET touched=? WHERE id=?", (time.time_ns(), job_id))
                return job
            if job.state not in ("queued", "failed") or job.attempts >= MAX_ATTEMPTS:
                raise QueueError("job_not_runnable")
            active = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            if job.state == "failed" and active >= self.limits.max_active:
                raise QueueError("queue_full")
            if db.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                raise QueueError("worker_busy")
            db.execute("""
                UPDATE jobs SET state='running', attempts=attempts+1,error=NULL,touched=? WHERE id=?
            """, (time.time_ns(), job_id))
            return self._get(db, job_id)

    def fail(self, job_id, error):
        if error not in ("source_failed", "engine_failed", "result_limit", "internal_error"):
            raise ValueError("Invalid failure classification")
        with self._transaction() as db:
            db.execute("UPDATE jobs SET state='failed',error=?,touched=? WHERE id=? AND state='running'",
                       (error, time.time_ns(), job_id))

    def complete(self, job_id, window):
        if not isinstance(window, TranscriptWindow):
            raise QueueError("invalid_result")
        result = json.dumps(window.to_json(), ensure_ascii=True, separators=(",", ":"))
        size = len(result.encode("utf-8"))
        if size > MAX_RESULT_BYTES:
            raise QueueError("result_limit")
        with self._transaction() as db:
            job = self._get(db, job_id)
            if job.state != "running" or (window.identity, window.start_ms, window.end_ms) != (
                job.spec.identity, job.spec.start_ms, job.spec.end_ms
            ):
                raise QueueError("invalid_result")
            total = db.execute("SELECT coalesce(sum(result_bytes),0) FROM jobs").fetchone()[0]
            while total + size > self.limits.cache_bytes:
                if not self._evict(db, job_id):
                    raise QueueError("result_limit")
                total = db.execute("SELECT coalesce(sum(result_bytes),0) FROM jobs").fetchone()[0]
            db.execute("""
                UPDATE jobs SET state='ready',result=?,result_bytes=?,error=NULL,touched=? WHERE id=?
            """, (result, size, time.time_ns(), job_id))
            return self._get(db, job_id)
