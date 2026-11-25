from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .summary_provider import MAX_OUTPUT_CHARS


MAX_ENTRIES = 2048
MAX_BYTES = 16 * 1024 * 1024
_KEY = re.compile(r"[0-9a-f]{64}")


class SummaryCacheError(Exception):
    pass


def valid_summary(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_OUTPUT_CHARS:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def cache_key(request, source_url, config, transcript_digest, prompt_version, prompt, output_tokens):
    identity = request.identity
    data = ["fitbaus-summary-cache-v1", source_url, identity.source_id, identity.item_id,
            identity.audio_revision, request.start_ms, request.end_ms, transcript_digest,
            "openai-chat/v1", config.base_url, config.model, config.cache_epoch,
            prompt_version, prompt, output_tokens]
    return hashlib.sha256(json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


class SummaryCache:
    def __init__(self, directory, max_entries=MAX_ENTRIES, max_bytes=MAX_BYTES):
        if not 1 <= max_entries <= MAX_ENTRIES or not 1 <= max_bytes <= MAX_BYTES:
            raise ValueError("Invalid summary cache limits")
        self.directory = Path(directory)
        self.path = self.directory / "summaries.sqlite3"
        self.max_entries = max_entries
        self.max_bytes = max_bytes

    @contextmanager
    def _transaction(self):
        db = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={(MAX_BYTES * 2 + 4 * 1024 * 1024) // page_size}")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS summaries (
                key TEXT PRIMARY KEY, summary TEXT NOT NULL,
                size INTEGER NOT NULL CHECK(size > 0), touched INTEGER NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS summary_age ON summaries(touched, key)")
            yield db
            db.commit()
        except (OSError, sqlite3.Error, UnicodeError):
            if db is not None:
                db.rollback()
            raise SummaryCacheError("summary_cache_unavailable") from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _validate_key(self, key):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("Invalid summary cache key")

    def get(self, key):
        self._validate_key(key)
        with self._transaction() as db:
            row = db.execute("SELECT summary,size FROM summaries WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            value, size = row
            if not valid_summary(value) or len(value.encode("utf-8")) != size or size > self.max_bytes:
                raise SummaryCacheError("summary_cache_unavailable")
            db.execute("UPDATE summaries SET touched=? WHERE key=?", (time.time_ns(), key))
            return value

    def put(self, key, value):
        self._validate_key(key)
        if not valid_summary(value):
            raise ValueError("Invalid summary cache value")
        size = len(value.encode("utf-8"))
        if size > self.max_bytes:
            return False
        with self._transaction() as db:
            db.execute("DELETE FROM summaries WHERE key=?", (key,))
            count, total = db.execute("SELECT count(*),coalesce(sum(size),0) FROM summaries").fetchone()
            while count >= self.max_entries or total + size > self.max_bytes:
                row = db.execute("SELECT key,size FROM summaries ORDER BY touched,key LIMIT 1").fetchone()
                if row is None:
                    raise SummaryCacheError("summary_cache_unavailable")
                db.execute("DELETE FROM summaries WHERE key=?", (row[0],))
                count -= 1
                total -= row[1]
            db.execute("INSERT INTO summaries(key,summary,size,touched) VALUES(?,?,?,?)",
                       (key, value, size, time.time_ns()))
        return True
