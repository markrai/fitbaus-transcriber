# Summary generation API (Phases 4A–4B)

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
- HTTP 200 `ready`: `summary` is plain text, `model` identifies the configured model, `prompt_version` identifies the prompt, and `transcript_digest` hashes exact validated transcript snapshots and their profiles. The digest alone is not a persistent summary cache key. The boolean `cache_hit` distinguishes a reused result from a newly generated one.
- HTTP 200 `empty`: no fully contained cues; summary is null and no provider request occurs. This can represent silence or narration entirely straddling section boundaries.

Only fully contained cues are sent, avoiding text outside the section when word-level timestamps are not available. A fixed system prompt treats narration as untrusted data, requests a concise factual summary in the narration language, and forbids extrapolation. It is not a guarantee against hallucinations or prompt injection. Oversized input is rejected rather than truncated.

Source permissions and revision and paired authorization are checked before dispatching the provider call and before returning its result. Revocation during an already-sent request cannot retract the excerpt from the provider, but suppresses the response to a revoked client.

## Operational bounds

- 400 invalid request, 401 missing/revoked credentials, 403 denied book, 404 unknown source/item, 409 stale revision or failed transcript, 413 oversized input, 429 busy/queue capacity, 503 disabled provider or transcription unavailable, 502 upstream/provider/invalid transcript, 504 provider timeout.
- One summary request at a time per service process. Other HTTP endpoints retain existing admission slots. Ready transcripts trigger a synchronous bounded provider call; future Trainer code must use a dedicated background request and suitable read timeout. No automatic LLM retries.
- Phase 4B adds persistent summary caching as described below. Phase 4C adds the Trainer client. No automatic summaries or user-facing controls.
- Provider socket timeouts and elapsed checks are not a hard process deadline. A co-located model may compete for external resources. Source credentials are never sent to the provider.

## Persistent cache (Phase 4B)

Successful, nonempty summaries are stored in the existing data volume at `summaries/summaries.sqlite3`. Each key covers the configured source URL, source/item/revision, exact section interval, validated transcript snapshots and profiles, provider protocol/URL/model, cache epoch, prompt content/version and output token limit. Only a SHA-256 key and generated summary with size/recency metadata are persisted; source and provider credentials, raw transcripts and URLs are not written to the summary database.

Every request, including a cache hit, revalidates current Audiobookshelf access, audio revision, device authorization and full transcript coverage. If transcript windows were evicted they must be recovered before a hit can be verified. Better prepared transcripts or changed text invalidate the old result. Failed or empty results are not cached. A provider changing weights behind an unchanged model name is not automatically detectable: change `TRANSCRIBER_SUMMARY_CACHE_EPOCH` to invalidate that namespace. Rotating only the bearer or changing the timeout does not invalidate content.

SQLite transactions and least-recently-used eviction bound the cache to 2,048 rows and 16 MiB of UTF-8 summary text; database overhead is separate, with a 36 MiB SQLite page limit. Old namespaces age out normally. A storage failure returns HTTP 503 `summary_cache_unavailable` rather than silently calling the provider again on a failed lookup. If persistence fails after inference, the request fails and a later retry may incur another call. One summary request is still admitted at a time per service process; no exactly-once guarantee across crashes or multiple service processes. The summary store is initialized lazily so a broken cache does not prevent ordinary transcription from starting. Summaries are private derived audiobook data; protect the data volume and its backups.

## Transcript-informed section planning (Phase 6A)

The companion advertises `section_plan_api: true`. See [section planning contract](SECTION-PLAN.md). This optional read-only endpoint refines nominal cut points using verified existing narration, without an LLM request or scheduling transcription. The summary API and cache identity are unchanged: summaries use their exact requested interval.
