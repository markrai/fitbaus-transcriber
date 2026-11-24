from dataclasses import dataclass
import hashlib
import json
import re

SCHEMA = "fitbaus.transcript/v1"
MAX_TIME_MS = 31_536_000_000
MAX_WINDOW_MS = 300_000
MAX_CUE_CHARS = 1200
MAX_CUES = 5000
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _time(value):
    if type(value) is not int or not 0 <= value <= MAX_TIME_MS:
        raise ValueError("Invalid book-relative timestamp")
    return value


@dataclass(frozen=True)
class AudioIdentity:
    source_id: str
    item_id: str
    audio_revision: str

    def __post_init__(self):
        if not _ID.fullmatch(self.source_id) or not _ID.fullmatch(self.item_id):
            raise ValueError("Invalid source or item identity")
        if not _DIGEST.fullmatch(self.audio_revision):
            raise ValueError("Invalid audio revision")

    def cache_key(self):
        encoded = json.dumps(
            [self.source_id, self.item_id, self.audio_revision],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self):
        if _time(self.end_ms) <= _time(self.start_ms):
            raise ValueError("Invalid cue interval")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > MAX_CUE_CHARS:
            raise ValueError("Invalid cue text")


@dataclass(frozen=True)
class TranscriptWindow:
    identity: AudioIdentity
    start_ms: int
    end_ms: int
    cues: tuple[Cue, ...]

    def __post_init__(self):
        if not 0 < _time(self.end_ms) - _time(self.start_ms) <= MAX_WINDOW_MS:
            raise ValueError("Invalid transcript window")
        if not isinstance(self.cues, tuple) or len(self.cues) > MAX_CUES:
            raise ValueError("Invalid cue collection")
        previous_end = self.start_ms
        for cue in self.cues:
            if cue.start_ms < previous_end or cue.end_ms > self.end_ms:
                raise ValueError("Cues must be ordered and contained in the window")
            previous_end = cue.end_ms

    def to_json(self):
        return {
            "schema": SCHEMA,
            "source_id": self.identity.source_id,
            "item_id": self.identity.item_id,
            "audio_revision": self.identity.audio_revision,
            "coverage": {"start_ms": self.start_ms, "end_ms": self.end_ms},
            "cues": [
                {"start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text}
                for c in self.cues
            ],
        }
