# Section planning (Phase 6A)

`POST /v1/section-plan` is an optional, authenticated read-only planning endpoint. Use the existing Transcriber bearer and Audiobookshelf access token in `X-Audiobookshelf-Token`. It works even when no summary LLM is configured. Capabilities advertise `section_plan_api: true`.

Request:

```json
{"source_id":"home","item_id":"book","audio_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","boundaries_ms":[0,480000,960000]}
```

Provide two to five strictly increasing integer boundaries, each adjacent interval no longer than ten minutes. Trainer owns chapter/gap metadata and supplies fixed outer anchors. Group up to four adjacent nominal sections per plan; never group across a chapter edge. The server checks identity, revision, book duration and current source/device authorization. It does not independently infer chapter boundaries.

Each interior cut may move at most 30 seconds, but only if both adjacent nominal intervals are 60,001–540,000 ms. This independent slack rule preserves ordered, nonoverlapping intervals no longer than ten minutes, even if adjacent cuts move in opposite directions. The algorithm prefers a cue ending with sentence punctuation and a pause of at least one second, then sentence punctuation alone, then pause alone; ties prefer the nearest end and then earlier time. Outer anchors and nonmovable cuts stay fixed.

All aligned transcript windows around movable cuts must already be ready and verified against the same audio revision. Prefer the highest available prepared profile over the existing live cache. A missing window returns the entire nominal plan, not a partial refinement. No new jobs, LLM inference, transcript text responses or persistent plan records are created. A single plan request is admitted concurrently per process.

HTTP 200 includes `schema: fitbaus.section-plan/v1`, `algorithm: sentence-pause-v1`, identity fields, original `nominal_boundaries_ms`, resulting `boundaries_ms`, `state: refined|nominal`, `reason: sentence_or_pause|no_suitable_boundary|no_movable_boundary|missing_transcript`, and a SHA-256 `transcript_digest` (null when no complete transcript snapshots were used). The same request and transcript snapshots yield the same plan. Changed snapshots can change the result.

Clients should validate all response fields and freeze the entire selected plan per playback session/block, including nominal fallback, rather than replan each individual section. Use approximately eight-minute nominal sections in long chapters to leave refinement slack. Older clients and servers continue to use the existing fixed intervals. Section summaries still use the existing summary endpoint and exact timestamps; the summary cache naturally distinguishes changed ranges.

This is a linguistic boundary heuristic, not a topic-change classifier or spoiler-free recap. Trainer adoption and physical integration are Phase 6B.
