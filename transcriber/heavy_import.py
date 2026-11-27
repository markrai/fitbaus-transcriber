import json
import time

from .background import BookJob, HEAVY_MODEL, MAX_BOOK_MS, heavy_profile, prepared_id
from .contract import AudioIdentity, Cue, MAX_WINDOW_MS, SCHEMA, TranscriptWindow
from .audiobookshelf import SourceError
from .jobs import MAX_RESULT_BYTES, QueueError

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
BUNDLE_SCHEMA = "fitbaus.heavy-transcript/v1"


def import_heavy(store, source, config, token, raw):
    if len(raw.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise ValueError("Bundle too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {
            "schema", "profile", "source_id", "item_id", "audio_revision", "duration_ms", "windows"}:
        raise ValueError("Invalid bundle")
    if data["schema"] != BUNDLE_SCHEMA or data["profile"] != heavy_profile(config):
        raise ValueError("Invalid heavy profile")
    identity = AudioIdentity(data["source_id"], data["item_id"], data["audio_revision"])
    duration = data["duration_ms"]
    if type(duration) is not int or not 0 < duration <= MAX_BOOK_MS:
        raise ValueError("Invalid duration")
    def authorize():
        manifest = source.manifest(identity.item_id, token)
        if manifest.identity != identity or manifest.duration_ms != duration:
            raise SourceError("stale_revision")

    authorize()
    windows = data["windows"]
    if not isinstance(windows, list) or len(windows) != (duration + MAX_WINDOW_MS - 1) // MAX_WINDOW_MS:
        raise ValueError("Incomplete coverage")
    job = BookJob(identity, HEAVY_MODEL, data["profile"], duration, 0, duration, "completed", None)
    rows = []
    for start, value in zip(range(0, duration, MAX_WINDOW_MS), windows):
        end = min(start + MAX_WINDOW_MS, duration)
        if not isinstance(value, dict) or set(value) != {
                "schema", "source_id", "item_id", "audio_revision", "coverage", "cues"}:
            raise ValueError("Invalid window")
        if (value["schema"] != SCHEMA or
                AudioIdentity(value["source_id"], value["item_id"], value["audio_revision"]) != identity or
                value["coverage"] != {"start_ms": start, "end_ms": end} or
                not isinstance(value["cues"], list)):
            raise ValueError("Invalid window identity or coverage")
        window = TranscriptWindow(identity, start, end, tuple(
            Cue(cue["start_ms"], cue["end_ms"], cue["text"]) for cue in value["cues"]))
        encoded = json.dumps(window.to_json(), ensure_ascii=True, separators=(",", ":"))
        size = len(encoded.encode("ascii"))
        if size > MAX_RESULT_BYTES:
            raise QueueError("result_limit")
        rows.append((job.id, start, end, encoded, size, prepared_id(job.id, start, end)))
    authorize()
    with store.transaction() as db:
        db.execute("DELETE FROM books WHERE id=?", (job.id,))
        if db.execute("SELECT count(*) FROM books").fetchone()[0] >= store.max_parents:
            raise QueueError("background_full")
        total = db.execute("SELECT coalesce(sum(result_bytes),0) FROM windows").fetchone()[0]
        if total + sum(row[4] for row in rows) > store.max_bytes:
            raise QueueError("background_storage_full")
        db.execute("INSERT INTO books VALUES(?,?,?,?,?,?,?,?,?,'completed',NULL,?)",
                   (job.id, identity.source_id, identity.item_id, identity.audio_revision,
                    job.model, job.profile, duration, 0, duration, time.time_ns()))
        db.executemany("INSERT INTO windows(parent_id,start_ms,end_ms,result,result_bytes,prepared_id) VALUES(?,?,?,?,?,?)",
                       rows)
    return {"job_id": job.id, "model": job.model, "state": "completed", "windows": len(rows),
            "audio_revision": identity.audio_revision}
