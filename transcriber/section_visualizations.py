from dataclasses import dataclass
import hashlib
import hmac
import re

from .section_plan import ready_window
from .summaries import SummaryError, SummaryRequest, authorize_interval, section_snapshot, PROMPT_VERSION, SYSTEM_PROMPT
from .summary_cache import cache_key
from .summary_provider import MODEL_PATTERN, MAX_OUTPUT_TOKENS


DIGEST = re.compile(r"[0-9a-f]{64}")


def digest_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SectionVisualizationRequest:
    section: SummaryRequest
    transcript_digest: str
    summary_model: str
    summary_prompt_version: str
    summary_digest: str

    @classmethod
    def parse(cls, body):
        section_keys = {"source_id", "item_id", "audio_revision", "start_ms", "end_ms"}
        parent_keys = {"transcript_digest", "summary_model", "summary_prompt_version", "summary_digest"}
        if not isinstance(body, dict) or set(body) != section_keys | parent_keys:
            raise ValueError("Invalid diagram request")
        if (any(not isinstance(body[name], str) or not DIGEST.fullmatch(body[name])
                for name in ("transcript_digest", "summary_digest"))
                or not isinstance(body["summary_model"], str)
                or not MODEL_PATTERN.fullmatch(body["summary_model"])
                or body["summary_prompt_version"] != PROMPT_VERSION):
            raise ValueError("Invalid diagram parent")
        return cls(SummaryRequest.parse({key: body[key] for key in section_keys}),
                   body["transcript_digest"], body["summary_model"],
                   body["summary_prompt_version"], body["summary_digest"])

    def response(self, **fields):
        return self.section.response(transcript_digest=self.transcript_digest,
                                     summary_model=self.summary_model,
                                     summary_prompt_version=self.summary_prompt_version,
                                     summary_digest=self.summary_digest,
                                     **fields)


def resolve_parent(request, token, runner, config, guard, cache, source_url, error_code):
    section = request.section
    manifest = authorize_interval(section, token, runner, guard)
    if config.model != request.summary_model:
        raise SummaryError(error_code, 409)
    snapshots = []
    for start, end in section.windows(manifest.duration_ms):
        snapshot = ready_window(runner, section.identity, start, end)
        if snapshot is None:
            raise SummaryError(error_code, 409)
        snapshots.append(snapshot)
    digest, cues = section_snapshot(section, snapshots, SYSTEM_PROMPT)
    if not cues or not hmac.compare_digest(digest, request.transcript_digest):
        raise SummaryError(error_code, 409)
    key = cache_key(section, source_url, config, digest, PROMPT_VERSION, SYSTEM_PROMPT, MAX_OUTPUT_TOKENS)
    summary = cache.get(key)
    if summary is None or not hmac.compare_digest(digest_text(summary), request.summary_digest):
        raise SummaryError(error_code, 409)
    authorize_interval(section, token, runner, guard)
    return key, summary


