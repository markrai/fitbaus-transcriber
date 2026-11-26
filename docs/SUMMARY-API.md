# Summary and recap generation API

FitBaus Transcriber owns summary generation; Trainer supplies an audiobook identity and section interval, never raw text, a prompt, provider URL or credentials. No Trainer change is required.

## Endpoint

`POST /v1/summaries` requires the existing operator or paired device bearer and the current Audiobookshelf access bearer in `X-Audiobookshelf-Token`. The optional provider must be configured. `GET /v1/capabilities` advertises `summary_api: true` for API support independently of provider configuration.

```json
{"source_id":"home-library","item_id":"book-1","audio_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","start_ms":180000,"end_ms":540000}
```

The five fields are required; optional boolean `retry` defaults false and applies only to failed transcript windows. Timestamps are book-relative integer milliseconds, half-open, positive length, at most ten minutes, within the current manifest. This endpoint produces whole-section summaries; the separate recap endpoint below restricts context to the requested playback position. Trainer supplies chapter-aware boundaries; the server does not independently enforce chapter boundaries.

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

## Recent-listening recap (Phase 7A)

`POST /v1/recaps` requires the same existing device/operator and Audiobookshelf credentials, provider configuration and source authorization as the summary endpoint. `GET /v1/capabilities` advertises `recap_api: true` independently of provider configuration.

```json
{"source_id":"home-library","item_id":"book-1","audio_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","position_ms":735000}
```

The four fields are required; optional boolean `retry` applies only to failed transcript windows. Position is an exact book-relative integer millisecond timestamp from zero through the current manifest duration, inclusive. No caller-supplied interval or prompt is accepted. The service derives the half-open range `[max(0, position_ms - 600000), position_ms)`. This is a recent ten-minute recap, not a book-to-date summary or a record of which portions the user actually listened to after seeking.

Responses follow the summary state machine (202 pending, 200 ready or empty) with `schema: fitbaus.recap/v1`, echoed `position_ms`, and derived `start_ms` and `end_ms`. Ready/empty responses use `prompt_version: recap-v1`. Position zero is a valid empty response after authorization, with no transcription or provider call. As with summaries, all intersecting transcript windows must be verified before generation. Even if the final aligned window contains later narration, only cues wholly contained inside the derived recap interval are sent to the model. Cues crossing either edge are excluded because word-level timing is unavailable.

The recap prompt asks for context useful when resuming, prohibits outside knowledge and predictions, and uses the narration language. This prevents later transcript text from entering provider input, but cannot guarantee that a model never hallucinates or recalls later events from pretraining. The server trusts the authorized client's reported position; it does not assert that every prior segment was heard. Trainer must capture the real playback position on explicit user request and reject stale responses after seeking.

The same bounded persistent cache and shared single-request admission slot are used. Recap keys are separate from whole-section summaries by prompt/version and exact interval, and recap digests incorporate validated profile, aligned window start and contained cue timestamps/text only. Changes limited to later cue text do not invalidate an otherwise identical recap snapshot, but profile changes do. Exact positions are not rounded, so frequent distinct requests may generate distinct cache entries. No automatic generation or new worker is added.

Trainer integration is Phase 7B. Older Trainer clients and existing summary/section-plan requests are unchanged.

## Cumulative recap (Phase 7C.1)

`POST /v1/cumulative-recaps` is an optional API for a bounded book-to-position recap. It requires the same device/operator and Audiobookshelf credentials and provider configuration. Capabilities advertise `cumulative_recap_api: true` and `cumulative_recap_max_position_ms: 86400000`.

```json
{"source_id":"home-library","item_id":"book-1","audio_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","position_ms":735000}
```

The exact four fields are required; no retry, caller-supplied interval, prompt or job ID is accepted. Position zero through 24 hours (inclusive) is supported, within the current manifest duration. The covered interval is always [0, position_ms). Books longer than 24 hours are supported up to that position only; later positions must use the recent-recap endpoint. The initial cap bounds scanning, cache and reduction work. This is a recap of the book prefix, not a listening-history ledger.

All book-aligned five-minute transcript windows intersecting that prefix must already be available from the prepared or live cache. Each is verified against the current revision and preferred transcript profile. Missing coverage yields HTTP 409 with state `missing_transcript`, exact identity/position, `missing_window_count` and the first 16 `missing_windows` starts. No new transcription is scheduled. Prepare the audiobook separately before retrying. No model request is made when coverage is missing. Only fully contained cues are included, including in the final partial leaf; no later transcript text enters the model input.

Ten-minute leaves are reduced chronologically in groups of at most four, with singleton groups promoted unchanged. Each node is content-addressed by its exact interval, contained transcript/profile digest or ordered child digests, algorithm, prompts and configured provider identity. Successful intermediate results use the existing bounded persistent summary cache. Unchanged complete prefixes can be reused when the requested position advances; a changed audio revision, transcript/profile or inference identity invalidates affected nodes. Each node output is limited to 12 KiB UTF-8 and the existing provider limits; reduction input stays under 64 KiB. Silent nodes require no provider call.

One new provider call is allowed per HTTP request, sharing the existing single summary admission slot. The client explicitly polls the same position after HTTP 202 `pending_summary`; every request rechecks source/device authorization and current full coverage. Partial work is persisted as ordinary cached nodes; no background task, credentials, raw transcript, or separate progress database is retained. HTTP 200 `ready` returns a completed recap; `empty` returns null without model inference. For 200/202 responses, `schema: fitbaus.cumulative-recap/v1`, `algorithm: cumulative-v1`, `position_ms`, `start_ms: 0`, `end_ms`, identity, model, prompt_version, transcript_digest, completed_nodes and total_nodes identify the snapshot and progress. A ready response includes summary and cache_hit. Progress is for the current snapshot, not a durable job counter: changes to available profiles, cache eviction or other requests can reduce it. Provider/cache errors stop the current request; previously cached nodes remain reusable. No partial recap is returned as complete.

A cold long book may require many explicit polls and model calls; the hierarchy reduces repeated work, not initial preparation cost. Repeated compression can omit details. The model may hallucinate or introduce prior knowledge despite the prompts. No strict end-to-end deadline or automatic background recap generation is introduced. Trainer adoption is a separate phase; existing summary, section-plan and recent-recap clients remain unchanged.


## Section diagrams (Phase 1)

The optional authenticated capabilities fields `summary_diagram_api: true` and `summary_diagram_formats: ["flowchart"]` advertise the server contract independently of provider configuration. Existing Trainer builds, LOBO and other compatible providers require no changes.

`POST /v1/section-diagrams` uses the same device/operator bearer and current Audiobookshelf token as summaries. The exact JSON keys are:

```json
{
  "source_id": "home-library",
  "item_id": "book-1",
  "audio_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "start_ms": 180000,
  "end_ms": 540000,
  "transcript_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "summary_model": "lobo-nano",
  "summary_prompt_version": "section-v1",
  "summary_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
}
```

The summary digest is SHA-256 of the exact UTF-8 ready summary text. The server revalidates book/device authorization, exact audio revision and interval, preferred existing transcript snapshots, current provider namespace and parent summary cache entry. No client text, model override, retry option, URL or prompt is accepted. Missing/changed parent yields 409 `diagram_parent_unavailable`. No transcription scheduling or summary regeneration occurs. Only the authoritative parent summary (not additional transcript text or source credentials) reaches the provider with a fixed diagram prompt. It may contain the same spoilers or inaccuracies as its parent.

HTTP 200 returns `schema: "fitbaus.section-diagram/v1"`, echoed request identity, `diagram_prompt_version: "flowchart-v1"`, `parser_version: "flowchart-subset-v1"`, `state: "ready"`, `cache_hit`, canonical Mermaid source and a normalized graph. Example graph:

```json
{
  "direction": "TD",
  "nodes": [
    {"id": "A", "label": "First concept"},
    {"id": "B", "label": "Second concept"}
  ],
  "edges": [
    {"from": "A", "to": "B", "label": "leads to"}
  ]
}
```

The provider is prompted to emit a compact JSON object such as `{"nodes":["First concept","Second concept"],"edges":[[1,2]]}`. Concepts are plain strings; edges use one-based integer positions in the node list. An optional third edge element is a plain relationship label. Direction defaults to TD and can optionally be TD or LR. The server assigns short canonical IDs itself; the earlier named-object input form remains accepted with exact fields. This is an internal generation format; the response graph and canonical Mermaid export are unchanged.

The bounded extractor accepts one complete top-level object surrounded by ordinary prose or Markdown fences, honors quoted strings and escapes, and tolerates independently balanced square brackets in surrounding prose. It rejects competing/extra objects, array wrappers, unmatched brackets, duplicate JSON keys, invalid JSON constants, incomplete JSON and nesting beyond eight levels. It does not guess, repair invalid JSON or select among competing objects.

Exact schemas and raw cardinality bounds (2–24 node declarations, 1–48 edge declarations) are enforced before normalization. Both JSON input forms decode into the same intermediate graph; all supplied IDs, endpoint types and labels must be syntactically valid, including fields later discarded. Compact references must be positive integers; zero is rejected as an ambiguous index base and negative indices are invalid. The server never shifts indices or guesses a missing node. Positive out-of-range references and syntactically valid undeclared named endpoints are omitted, along with self-loops, identical duplicate edges, identical repeated named node declarations and isolated nodes. Remaining identities, labels, edge directions and declaration order are preserved. Conflicting node definitions and conflicting labels on surviving directed pairs are rejected. If there are no usable edges, the remaining graph is cyclic or disconnected, or the normalized graph exceeds the wire limits, the output is rejected rather than arbitrarily cutting meaningful relationships. No relationship is invented or reversed.

Canonical Mermaid passes the unchanged strict flowchart validator, which verifies connected, acyclic graphs of 2–10 unique nodes, 1–16 unique directed edges, and labels at most 64/24 Unicode code points respectively. No arbitrary Mermaid directives, HTML, entities, scripts, link handlers, styles, control characters, or executable rendering. Raw output and canonical source are bounded to 8 KiB each, normalized response to 32 KiB, and provider completion to existing output limits. Unrecoverable output fails as 502 `invalid_diagram`, without an additional model call or partial result. Clients independently validate the graph and render plain labels natively. Structural recovery does not establish that model-generated relationships are factually correct.

The internal generation namespace is `normalized-json-v3`; it is included in the diagram cache key. The prompt is unchanged, existing summary entries are unaffected, and wire-level `flowchart-v1` and `flowchart-subset-v1` identifiers stay unchanged. Older diagram cache rows remain subject to ordinary LRU eviction; previously successful sections may require one new diagram inference under the new namespace. No cache wipe or client upgrade is needed.

The server emits sanitized stderr events for diagram response status, allowlisted outcome, validation failure category and generation/validation elapsed milliseconds. Successful normalization additionally emits integer counts of input/output nodes and edges, removed duplicate nodes, unknown edges, duplicate edges, self-loops and isolated nodes. Neither raw model output, labels, summaries, identifiers nor credentials are logged. Provider-specific constrained JSON generation is intentionally not required: all configured OpenAI-compatible providers use the same prompt and validation path.

Diagrams use the shared provider admission coordinator. Busy diagrams return HTTP 202 `state: "deferred"`, `retry_after_seconds: 3` with echoed identity and versions, no graph or retained credentials. A busy response checks source/device access and interval, and defers expensive parent validation until admission. Foreground summary/recap/management requests have priority: one waiter may wait up to 120 seconds behind an admitted diagram; competing foreground requests still receive 429 `summary_busy`. A waiting foreground request suppresses speculative provider dispatch before inference starts and receives the next slot. Once started, an OpenAI-compatible provider call cannot necessarily be interrupted; the provider timeout is not a hard process deadline. Do not claim preemption. The client must bound deferred retries and discard obsolete responses. No server-side background queue or automatic retries.

Successful canonical Mermaid source is stored in `diagrams/diagrams.sqlite3`; normalized graph is reconstructed and validated on every hit. Separate LRU budget: 1,024 rows, 8 MiB payload, 24 MiB SQLite page cap. Cache key includes the full parent summary cache identity, exact summary digest, diagram prompt/version, internal generation namespace, parser version and output limit. Source permissions, current parent and provider identity are checked before inference and again before publication; stale or invalid results do not populate the cache. Cache errors return 503 `diagram_cache_unavailable`. Existing summary cache limits are unchanged. Protect diagram data and backups as private derived audiobook content.
