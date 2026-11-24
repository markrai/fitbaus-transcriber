from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import math
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .contract import AudioIdentity, MAX_TIME_MS

MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_TRACK_BYTES = 512 * 1024 * 1024
MAX_TRACKS = 1000
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class SourceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid source or item identifier")
    return value


def valid_bearer(token):
    return isinstance(token, str) and 1 <= len(token) <= 8192 and all(
        33 <= ord(c) <= 126 for c in token
    )


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    base_url: str

    def __post_init__(self):
        identifier(self.source_id)
        url = urlsplit(self.base_url)
        if (
            url.scheme not in ("http", "https") or not url.hostname
            or url.username is not None or url.password is not None
            or url.query or url.fragment or url.path.rstrip("/") not in ("", "/audiobookshelf")
            or any(c.isspace() or ord(c) < 32 or c == "\\" for c in self.base_url)
            or (url.port is not None and not 1 <= url.port <= 65535)
        ):
            raise ValueError("Invalid Audiobookshelf source URL")
        object.__setattr__(self, "base_url", urlunsplit(
            (url.scheme, url.netloc, url.path.rstrip("/"), "", "")
        ))


@dataclass(frozen=True)
class Track:
    file_id: str
    offset_ms: int
    duration_ms: int
    size: int
    mtime_us: int
    ctime_us: int

    def revision_row(self):
        return [self.file_id, self.offset_ms, self.duration_ms, self.size, self.mtime_us, self.ctime_us]


@dataclass(frozen=True)
class Manifest:
    identity: AudioIdentity
    tracks: tuple[Track, ...]

    @property
    def duration_ms(self):
        last = self.tracks[-1]
        return last.offset_ms + last.duration_ms

    def to_json(self):
        return {
            "source_id": self.identity.source_id,
            "item_id": self.identity.item_id,
            "audio_revision": self.identity.audio_revision,
            "duration_ms": self.duration_ms,
            "tracks": [
                {"file_id": t.file_id, "offset_ms": t.offset_ms, "duration_ms": t.duration_ms, "size": t.size}
                for t in self.tracks
            ],
        }


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ValueError("Invalid audio file metadata")
    return value


def _file_time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2**53 / 1000:
        raise ValueError("Invalid audio file timestamp")
    return math.floor(value * 1000 + 0.5)


def _milliseconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= MAX_TIME_MS / 1000:
        raise ValueError("Invalid audio timing")
    return math.floor(value * 1000 + 0.5)


def parse_manifest(config, item_id, payload):
    identifier(item_id)
    if payload.get("id") != item_id or payload.get("mediaType") != "book" or payload.get("isMissing") or payload.get("isInvalid"):
        raise ValueError("Unavailable audiobook")
    rows = payload["media"]["tracks"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_TRACKS:
        raise ValueError("Invalid audiobook tracks")
    tracks = []
    seen = set()
    end = 0
    for row in rows:
        file_id = identifier(row["ino"])
        if file_id in seen or row.get("exclude") or row.get("error"):
            raise ValueError("Invalid or duplicated audio track")
        seen.add(file_id)
        if row.get("contentUrl") != f"/api/items/{item_id}/file/{file_id}":
            raise ValueError("Unexpected audiobook file endpoint")
        offset = _milliseconds(row["startOffset"])
        duration = _milliseconds(row["duration"])
        if (not tracks and offset != 0) or abs(offset - end) > 1 or duration <= 0 or offset + duration > MAX_TIME_MS:
            raise ValueError("Invalid audio timeline")
        metadata = row["metadata"]
        track = Track(file_id, offset, duration, _integer(metadata["size"], 1),
                      _file_time(metadata["mtimeMs"]), _file_time(metadata["ctimeMs"]))
        tracks.append(track)
        end = offset + duration
    encoded = json.dumps(
        ["abs-manifest-v1", [track.revision_row() for track in tracks]],
        separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    identity = AudioIdentity(config.source_id, item_id, hashlib.sha256(encoded).hexdigest())
    return Manifest(identity, tuple(tracks))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Audiobookshelf:
    def __init__(self, config, timeout=10, max_track_bytes=MAX_TRACK_BYTES, transfer_seconds=300):
        self.config = config
        self.timeout = timeout
        self.max_track_bytes = max_track_bytes
        self.transfer_seconds = transfer_seconds
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    @contextmanager
    def _get(self, path, token):
        if not valid_bearer(token):
            raise SourceError("unauthorized")
        request = Request(self.config.base_url + path, headers={
            "Authorization": "Bearer " + token,
            "Accept-Encoding": "identity",
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise SourceError("invalid_response")
                yield response
        except HTTPError as error:
            status = error.code
            error.close()
            raise SourceError({401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(status, "upstream_error")) from None
        except (OSError, URLError, HTTPException):
            raise SourceError("upstream_unavailable") from None

    def _chunks(self, response, limit, deadline):
        total = 0
        while True:
            if time.monotonic() >= deadline:
                raise SourceError("transfer_timeout")
            chunk = response.read1(min(65536, limit - total + 1))
            if not chunk:
                return
            total += len(chunk)
            if total > limit:
                raise SourceError("size_limit")
            yield chunk

    def manifest(self, item_id, token):
        identifier(item_id)
        with self._get(f"/api/items/{item_id}?expanded=1", token) as response:
            if response.headers.get_content_type() != "application/json":
                raise SourceError("invalid_response")
            data = b"".join(self._chunks(response, MAX_METADATA_BYTES, time.monotonic() + 30))
        try:
            return parse_manifest(self.config, item_id, json.loads(data))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            raise SourceError("invalid_manifest") from None

    @contextmanager
    def materialize_track(self, identity, file_id, token, directory):
        if identity.source_id != self.config.source_id:
            raise SourceError("not_found")
        current = self.manifest(identity.item_id, token)
        if current.identity != identity:
            raise SourceError("stale_revision")
        track = next((t for t in current.tracks if t.file_id == file_id), None)
        if track is None:
            raise SourceError("not_found")
        if track.size > self.max_track_bytes:
            raise SourceError("size_limit")
        with tempfile.TemporaryFile(dir=directory) as target:
            path = f"/api/items/{identity.item_id}/file/{track.file_id}"
            with self._get(path, token) as response:
                length = response.headers.get("Content-Length")
                if length is not None and length != str(track.size):
                    raise SourceError("stale_revision")
                for chunk in self._chunks(response, track.size, time.monotonic() + self.transfer_seconds):
                    target.write(chunk)
            if target.tell() != track.size:
                raise SourceError("incomplete_audio")
            if self.manifest(identity.item_id, token).identity != identity:
                raise SourceError("stale_revision")
            target.seek(0)
            yield target
