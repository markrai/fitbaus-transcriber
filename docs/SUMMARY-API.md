# Summary generation API (Phase 4A)

FitBaus Transcriber owns summary generation; Trainer supplies an audiobook identity and section interval, never raw text, a prompt, provider URL or credentials. No Trainer change is required.

## Endpoint

`POST /v1/summaries` requires the existing operator or paired device bearer and the current Audiobookshelf access bearer in `X-Audiobookshelf-Token`. The optional provider must be configured. `GET /v1/capabilities` advertises `summary_api: true` for API support independently of provider configuration.

```json
{"source_id":"home-library","item_id":"book-1","audio_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","start_ms":180000,"end_ms":540000}
```

The five fields are required; optional boolean `retry` defaults false and applies only to failed transcript windows. Timestamps are book-relative integer milliseconds, half-open, positive length, at most ten minutes, within the current manifest. Only section summaries are supported. Trainer supplies chapter-aware boundaries; the server does not independently enforce chapter boundaries.

The source manifest is checked against the requested identity. All intersecting book-aligned five-minute windows are resolved through the existing runner, preferring prepared transcripts then cached live results. Missing windows are scheduled through the existing bounded dispatcher and device guard. No new audio decoder or Whisper worker is introduced. Incomplete coverage never reaches the provider.

Responses use schema `fitbaus.summary/v1`, include the requested identity and interval, and one of:

- HTTP 202 `pending_transcript`: `pending_windows` contains outstanding window starts; retry the same request after a delay. Do not continually set retry=true.
- HTTP 200 `ready`: `summary` is plain text, `model` identifies the configured model, `prompt_version` identifies the prompt, and `transcript_digest` hashes exact validated transcript snapshots and their profiles. The digest alone is not a persistent summary cache key.
- HTTP 200 `empty`: no fully contained cues; summary is null and no provider request occurs. This can represent silence or narration entirely straddling section boundaries.

Only fully contained cues are sent, avoiding text outside the section when word-level timestamps are not available. A fixed system prompt treats narration as untrusted data, requests a concise factual summary in the narration language, and forbids extrapolation. It is not a guarantee against hallucinations or prompt injection. Oversized input is rejected rather than truncated.

Source permissions and revision and paired authorization are checked before dispatching the provider call and before returning its result. Revocation during an already-sent request cannot retract the excerpt from the provider, but suppresses the response to a revoked client.

## Operational bounds

- 400 invalid request, 401 missing/revoked credentials, 403 denied book, 404 unknown source/item, 409 stale revision or failed transcript, 413 oversized input, 429 busy/queue capacity, 503 disabled provider or transcription unavailable, 502 upstream/provider/invalid transcript, 504 provider timeout.
- One summary request at a time per service process. Other HTTP endpoints retain existing admission slots. Ready transcripts trigger a synchronous bounded provider call; future Trainer code must use a dedicated background request and suitable read timeout. No automatic LLM retries.
- No persistent summary cache or deduplication yet. Repeating a ready request may incur another provider call. Phase 4B adds caching; Phase 4C adds the Trainer client. No automatic summaries or user-facing controls.
- Provider socket timeouts and elapsed checks are not a hard process deadline. A co-located model may compete for external resources. Source credentials are never sent to the provider.
