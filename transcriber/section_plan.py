from dataclasses import dataclass
import hashlib
import json

from .audiobookshelf import SourceError
from .contract import AudioIdentity, MAX_TIME_MS, MAX_WINDOW_MS
from .jobs import JobSpec, QueueError
from .runner import engine_profile
from .summaries import MAX_SECTION_MS, authorize_interval, validate_window


SCHEMA = "fitbaus.section-plan/v1"
ALGORITHM = "sentence-pause-v1"
SHIFT_MS = 30_000
MIN_NOMINAL_MS = 60_001
MAX_NOMINAL_MS = MAX_SECTION_MS - 2 * SHIFT_MS
MIN_PAUSE_MS = 1000
END_MARKS = ".!?…。！？؟"
CLOSERS = "\"'”’»）)]】"


@dataclass(frozen=True)
class SectionPlanRequest:
    identity: AudioIdentity
    boundaries: tuple[int, ...]

    def __post_init__(self):
        if (not isinstance(self.boundaries, tuple) or not 2 <= len(self.boundaries) <= 5
                or any(type(v) is not int or not 0 <= v <= MAX_TIME_MS for v in self.boundaries)
                or any(not 0 < b - a <= MAX_SECTION_MS for a, b in zip(self.boundaries, self.boundaries[1:]))):
            raise ValueError("Invalid section plan boundaries")

    @property
    def end_ms(self):
        return self.boundaries[-1]

    @classmethod
    def parse(cls, body):
        if not isinstance(body, dict) or set(body) != {"source_id", "item_id", "audio_revision", "boundaries_ms"}:
            raise ValueError("Invalid section plan request")
        boundaries = body["boundaries_ms"]
        if not isinstance(boundaries, list) or not 2 <= len(boundaries) <= 5:
            raise ValueError("Invalid section plan boundaries")
        return cls(AudioIdentity(body["source_id"], body["item_id"], body["audio_revision"]), tuple(boundaries))

    def movable(self):
        return tuple(i for i in range(1, len(self.boundaries) - 1)
                     if MIN_NOMINAL_MS <= self.boundaries[i] - self.boundaries[i - 1] <= MAX_NOMINAL_MS
                     and MIN_NOMINAL_MS <= self.boundaries[i + 1] - self.boundaries[i] <= MAX_NOMINAL_MS)

    def required_windows(self, duration):
        starts = set()
        for i in self.movable():
            cut = self.boundaries[i]
            low = max(0, cut - SHIFT_MS - MIN_PAUSE_MS)
            high = min(duration, cut + SHIFT_MS + MIN_PAUSE_MS + 1)
            starts.update(range(low // MAX_WINDOW_MS * MAX_WINDOW_MS, high, MAX_WINDOW_MS))
        return [(start, min(start + MAX_WINDOW_MS, duration)) for start in sorted(starts)]

    def response(self, boundaries, reason, digest=None):
        return {"schema": SCHEMA, "algorithm": ALGORITHM, "source_id": self.identity.source_id,
                "item_id": self.identity.item_id, "audio_revision": self.identity.audio_revision,
                "nominal_boundaries_ms": list(self.boundaries), "boundaries_ms": list(boundaries),
                "state": "refined" if tuple(boundaries) != self.boundaries else "nominal",
                "reason": reason, "transcript_digest": digest}


def ready_window(runner, identity, start, end):
    spec = JobSpec(identity, start, end, engine_profile(runner.engine.config))
    job = runner.prepared.find_prepared(spec, runner.engine.config) if runner.prepared is not None else None
    if job is None:
        try:
            job = runner.store.get(spec.id)
        except QueueError as error:
            if str(error) == "job_not_found":
                return None
            raise
    if job.state != "ready":
        return None
    return job.spec.profile, validate_window(job, identity, start, end)


def choose_cut(cut, cues):
    candidates = []
    for index, cue in enumerate(cues):
        point = cue.end_ms
        if abs(point - cut) > SHIFT_MS:
            continue
        sentence = cue.text.rstrip().rstrip(CLOSERS).rstrip().endswith(tuple(END_MARKS))
        pause = index + 1 < len(cues) and cues[index + 1].start_ms - point >= MIN_PAUSE_MS
        if sentence or pause:
            candidates.append((0 if sentence and pause else 1 if sentence else 2, abs(point - cut), point))
    return min(candidates)[2] if candidates else cut


def section_plan(request, token, runner, guard=None):
    manifest = authorize_interval(request, token, runner, guard)
    windows = request.required_windows(manifest.duration_ms)
    if not windows:
        return request.response(request.boundaries, "no_movable_boundary")
    snapshots = []
    for start, end in windows:
        if guard is not None and not guard():
            raise SourceError("unauthorized")
        snapshot = ready_window(runner, request.identity, start, end)
        if snapshot is None:
            authorize_interval(request, token, runner, guard)
            return request.response(request.boundaries, "missing_transcript")
        snapshots.append(snapshot)
    digest = hashlib.sha256()
    cues = []
    for profile, window in snapshots:
        digest.update(json.dumps([profile, window.to_json()], sort_keys=True, ensure_ascii=True,
                                 separators=(",", ":")).encode("ascii"))
        cues.extend(window.cues)
    boundaries = list(request.boundaries)
    for i in request.movable():
        cut = request.boundaries[i]
        nearby = [cue for cue in cues if cue.end_ms >= cut - SHIFT_MS - MIN_PAUSE_MS
                  and cue.start_ms <= cut + SHIFT_MS + MIN_PAUSE_MS]
        boundaries[i] = choose_cut(cut, nearby)
    authorize_interval(request, token, runner, guard)
    return request.response(boundaries, "sentence_or_pause" if tuple(boundaries) != request.boundaries
                            else "no_suitable_boundary", digest.hexdigest())
