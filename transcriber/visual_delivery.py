import base64
import hashlib
import json
import re
import struct
import zlib

from .audiobookshelf import SourceError, identifier
from .background import BookJob, HEAVY_MODEL, MAX_BOOK_MS, heavy_profile
from .contract import AudioIdentity, Cue, TranscriptWindow, MAX_WINDOW_MS
from .jobs import QueueError


SCHEMA = "fitbaus.epub-delivery-candidate/v1"
RENDER_POLICY = "static-rgba-png/v2"
TIMING_POLICY = "exact-anchor-bounds/v1"
MAX_CANDIDATE_BYTES = 32 * 1024 * 1024
MAX_STORE_BYTES = 64 * 1024 * 1024
MAX_CANDIDATES = 32
MAX_PNG = 1024 * 1024
MAX_WIDTH = 1024
MAX_HEIGHT = 600
MAX_PNGS = 128
MAX_TOTAL_PNG = 16 * 1024 * 1024
MAX_OCCURRENCES = 2048
MAX_INTERVAL_MS = 30000
DIGEST = re.compile(r"[0-9a-f]{64}")
METHODS = frozenset(("within_exact_cue", "between_adjacent_exact_cues"))


class DeliveryError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("ascii")


def require_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise DeliveryError("Invalid visual delivery fields")


def digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise DeliveryError("Invalid visual delivery digest")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise DeliveryError("Invalid visual delivery integer")
    return value


def png_dimensions(raw):
    if not 0 < len(raw) <= MAX_PNG or raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise DeliveryError("Invalid normalized PNG")
    offset, width, height = 8, 0, 0
    compressed = bytearray()
    while offset + 12 <= len(raw):
        length = struct.unpack_from(">I", raw, offset)[0]
        end = offset + 12 + length
        if end > len(raw):
            break
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:end - 4]
        if zlib.crc32(kind + data) != struct.unpack_from(">I", raw, end - 4)[0]:
            break
        if kind == b"IHDR" and offset == 8 and length == 13:
            width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", data)
            if (not 0 < width <= MAX_WIDTH or not 0 < height <= MAX_HEIGHT
                    or (depth, color, comp, filt, interlace) != (8, 6, 0, 0, 0)):
                break
        elif kind == b"IDAT" and width:
            compressed.extend(data)
        elif kind == b"IEND" and width and length == 0 and end == len(raw) and compressed:
            expected = height * (1 + 4 * width)
            try:
                decoder = zlib.decompressobj()
                pixels = decoder.decompress(compressed, expected + 1)
                if (len(pixels) == expected and decoder.eof and not decoder.unused_data
                        and not decoder.unconsumed_tail
                        and all(pixels[i] <= 4 for i in range(0, expected, 1 + 4 * width))):
                    return width, height
            except zlib.error:
                pass
            break
        else:
            break
        offset = end
    raise DeliveryError("Invalid normalized PNG structure")



def validate_candidate(raw, candidate_id, config):
    digest(candidate_id)
    if (not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CANDIDATE_BYTES
            or hashlib.sha256(raw).hexdigest() != candidate_id):
        raise DeliveryError("Visual delivery digest or size mismatch")
    try:
        value = json.loads(raw)
        require_keys(value, "schema render_policy source_artifact_id identity timing_policy "
                     "edition_verified render_qualified playback_qualified summary pngs occurrences")
        if (canonical(value) != raw or value["schema"] != SCHEMA
                or value["render_policy"] != RENDER_POLICY or value["timing_policy"] != TIMING_POLICY
                or any(value[key] is not False for key in
                       ("edition_verified", "render_qualified", "playback_qualified"))):
            raise DeliveryError("Unsupported visual delivery contract")
        digest(value["source_artifact_id"])
        provenance = value["identity"]
        require_keys(provenance, "source_id item_id file_id audio_revision profile duration_ms "
                     "epub_sha256 transcript_sha256")
        identity = AudioIdentity(identifier(provenance["source_id"]), identifier(provenance["item_id"]),
                                 digest(provenance["audio_revision"]))
        identifier(provenance["file_id"])
        digest(provenance["epub_sha256"])
        digest(provenance["transcript_sha256"])
        duration = integer(provenance["duration_ms"], 1, MAX_BOOK_MS)
        if provenance["profile"] != heavy_profile(config):
            raise DeliveryError("Invalid visual delivery profile")
        images = value["pngs"]
        if not isinstance(images, list) or len(images) > MAX_PNGS:
            raise DeliveryError("Invalid delivery PNG count")
        hashes, total = set(), 0
        for image in images:
            require_keys(image, "png_sha256 width height size payload_base64")
            key = digest(image["png_sha256"])
            size = integer(image["size"], 1, MAX_PNG)
            encoded = image["payload_base64"]
            if not isinstance(encoded, str) or len(encoded) != 4 * ((size + 2) // 3):
                raise DeliveryError("Invalid delivery PNG encoding")
            payload = base64.b64decode(encoded, validate=True)
            if (base64.b64encode(payload).decode("ascii") != encoded or len(payload) != size
                    or hashlib.sha256(payload).hexdigest() != key or key in hashes):
                raise DeliveryError("Invalid or duplicated delivery PNG")
            width, height = png_dimensions(payload)
            if (integer(image["width"], 1, MAX_WIDTH), integer(image["height"], 1, MAX_HEIGHT)) != (width, height):
                raise DeliveryError("Delivery PNG dimensions disagree")
            hashes.add(key)
            total += size
            if total > MAX_TOTAL_PNG:
                raise DeliveryError("Delivery PNG quota exceeded")
        if [image["png_sha256"] for image in images] != sorted(hashes):
            raise DeliveryError("Delivery PNG ordering is invalid")
        rows = value["occurrences"]
        if not isinstance(rows, list) or len(rows) > MAX_OCCURRENCES:
            raise DeliveryError("Invalid delivery occurrence count")
        previous, references, sources = -1, set(), {}
        for row in rows:
            require_keys(row, "occurrence_index source_asset_id png_sha256 earliest_ms latest_ms method")
            index = integer(row["occurrence_index"], 0, MAX_OCCURRENCES - 1)
            source_asset = digest(row["source_asset_id"])
            png = digest(row["png_sha256"])
            start = integer(row["earliest_ms"], 0, duration - 1)
            end = integer(row["latest_ms"], start + 1, duration)
            if (index <= previous or png not in hashes or end - start > MAX_INTERVAL_MS
                    or row["method"] not in METHODS or sources.get(source_asset, png) != png):
                raise DeliveryError("Invalid visual delivery occurrence")
            previous = index
            references.add(png)
            sources[source_asset] = png
        if references != hashes or len(sources) > MAX_PNGS:
            raise DeliveryError("Unreferenced delivery PNG or excessive sources")
        summary = value["summary"]
        require_keys(summary, "candidate_count unique_png_count withheld_count withheld_reasons")
        withheld = integer(summary["withheld_count"], 0, MAX_OCCURRENCES - len(rows))
        reasons = summary["withheld_reasons"]
        if (not isinstance(reasons, dict) or len(reasons) > MAX_OCCURRENCES
                or any(not re.fullmatch(r"[a-z_]{1,64}", reason) for reason in reasons)
                or sum(integer(count, 1, MAX_OCCURRENCES) for count in reasons.values()) != withheld
                or integer(summary["candidate_count"], 0, MAX_OCCURRENCES) != len(rows)
                or integer(summary["unique_png_count"], 0, MAX_PNGS) != len(images)
                or any(row["occurrence_index"] >= len(rows) + withheld for row in rows)):
            raise DeliveryError("Invalid visual delivery summary")
        return value, BookJob(identity, HEAVY_MODEL, provenance["profile"], duration, 0,
                              duration, "completed", None)
    except (TypeError, KeyError, AttributeError, OverflowError, RecursionError, UnicodeError) as error:
        raise DeliveryError("Invalid visual delivery contract") from None


def verify_transcript(db, job, value):
    current = db.execute("SELECT * FROM books WHERE id=?", (job.id,)).fetchone()
    if (current is None or current["model"] != HEAVY_MODEL or current["state"] != "completed"
            or current["cursor_ms"] != job.duration_ms or current["start_ms"] != 0
            or current["duration_ms"] != job.duration_ms or current["profile"] != job.profile
            or current["source_id"] != job.identity.source_id or current["item_id"] != job.identity.item_id
            or current["revision"] != job.identity.audio_revision):
        raise DeliveryError("Complete matching Large-v3 transcript required")
    digest_value = hashlib.sha256()
    expected_start = 0
    needed = {(row["earliest_ms"], row["latest_ms"], row["method"]) for row in value["occurrences"]}
    previous = None
    for row in db.execute("SELECT start_ms,end_ms,result FROM windows WHERE parent_id=? ORDER BY start_ms",
                          (job.id,)):
        end = min(expected_start + MAX_WINDOW_MS, job.duration_ms)
        if expected_start >= job.duration_ms or (row["start_ms"], row["end_ms"]) != (expected_start, end):
            raise DeliveryError("Incomplete imported transcript coverage")
        stored = json.loads(row["result"])
        window = TranscriptWindow(job.identity, expected_start, end, tuple(
            Cue(cue["start_ms"], cue["end_ms"], cue["text"]) for cue in stored["cues"]))
        if canonical(stored) != canonical(window.to_json()):
            raise DeliveryError("Invalid imported transcript window")
        digest_value.update(canonical(window.to_json()) + b"\n")
        for cue in window.cues:
            needed.discard((cue.start_ms, cue.end_ms, "within_exact_cue"))
            if previous is not None:
                needed.discard((previous.start_ms, cue.end_ms, "between_adjacent_exact_cues"))
            previous = cue
        expected_start = end
    if (expected_start != job.duration_ms or digest_value.hexdigest() != value["identity"]["transcript_sha256"]
            or needed):
        raise DeliveryError("Visual timing does not match imported transcript")


def import_delivery(store, source, config, token, raw, candidate_id):
    value, job = validate_candidate(raw, candidate_id, config)
    provenance = value["identity"]

    def authorize():
        manifest = source.manifest(job.identity.item_id, token)
        if manifest.identity != job.identity or manifest.duration_ms != job.duration_ms:
            raise SourceError("stale_revision")

    authorize()
    source.verify_epub(job.identity.item_id, provenance["file_id"], provenance["epub_sha256"], token)
    authorize()
    with store.transaction() as db:
        verify_transcript(db, job, value)
        existing = db.execute("SELECT candidate_id, payload FROM visual_candidates WHERE parent_id=?",
                              (job.id,)).fetchone()
        if existing is not None and existing["candidate_id"] == candidate_id:
            if existing["payload"] != raw:
                raise DeliveryError("Stored visual candidate is corrupt")
            reused = True
        else:
            reused = False
            count, total = db.execute(
                "SELECT count(*), coalesce(sum(payload_bytes),0) FROM visual_candidates WHERE parent_id<>?",
                (job.id,)).fetchone()
            if count >= MAX_CANDIDATES or total + len(raw) > MAX_STORE_BYTES:
                raise QueueError("visual_storage_full")
            db.execute("INSERT OR REPLACE INTO visual_candidates(parent_id,candidate_id,payload,payload_bytes) "
                       "VALUES(?,?,?,?)", (job.id, candidate_id, raw, len(raw)))
    return {"state": "staged", "candidate_id": candidate_id, "job_id": job.id,
            "audio_revision": job.identity.audio_revision, "reused": reused,
            "candidate_count": value["summary"]["candidate_count"], "playback_qualified": False}
