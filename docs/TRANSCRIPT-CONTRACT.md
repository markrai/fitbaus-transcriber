# Transcript contract v1

The transcriber and its consumers share the following data model. The foundation validates and serializes this model; no transcript HTTP route is exposed yet.

## Audio identity

- `source_id`: stable operator-assigned Audiobookshelf source identifier; never an arbitrary URL supplied by a client.
- `item_id`: library item identifier.
- `audio_revision`: 64-character lowercase SHA-256 digest of a canonical, ordered audio manifest. The manifest algorithm is defined below. Do not use title, duration, or item ID alone as a revision.
- The cache namespace includes all three fields. Authentication and authorization must be checked independently of cache hits.

## Audiobookshelf manifest revision v1

The adapter reads `GET /api/items/{item_id}?expanded=1` using the caller's bearer credential. It uses the ordered `media.tracks` array, not the order of raw audio files or a playback session. No listening history is modified.

The revision is SHA-256 of compact ASCII JSON with no whitespace:

```json
["abs-manifest-v1",[["123",0,10125,123456,1700000000000125,1700000000000500]]]
```

Each row contains the file inode ID, book-relative offset in milliseconds, duration in milliseconds, size in bytes, modification time in microseconds, and change time in microseconds. Seconds and fractional millisecond file timestamps are rounded half-up to the respective integer unit. Only valid included tracks, contiguous within 1 ms of rounding error, are accepted. The first offset must be zero. Metadata-only title and cover changes do not affect the revision.

This revision fingerprints upstream file metadata, not audio bytes. It detects ordinary file and ordering changes after Audiobookshelf rescans; it cannot guarantee detection of a byte change hidden from the upstream manifest. A content digest may supplement it in a later cache phase.

The internal ingestion adapter checks authorization and revision before and after copying a track, verifies the expected byte count, and deletes temporary audio when its context closes. Maximum metadata response: 4 MiB; maximum track: 512 MiB; maximum tracks: 1,000. Transfer limits and timeouts bound individual ingestion; a later queue must bound aggregate disk and worker concurrency. No redirects or environment proxies are used.

## Ready window

```json
{
  "schema": "fitbaus.transcript/v1",
  "source_id": "home-library",
  "item_id": "book-1",
  "audio_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "coverage": {"start_ms": 60000, "end_ms": 65000},
  "cues": [
    {"start_ms": 61000, "end_ms": 64000, "text": "A new chapter begins."}
  ]
}
```

All timing is integer milliseconds from the start of the entire audiobook, regardless of the number of source tracks. Coverage is a half-open interval of at most 300,000 ms. Cues are ordered, non-overlapping, contained within coverage, and half-open. Gaps are valid. A ready window with zero cues means the interval was processed and contained no narration. Missing coverage does not mean silence.

Text is Unicode plain text, not HTML. A cue may contain at most 1,200 characters. The initial model admits up to 5,000 cues per window; the HTTP adapter must impose a serialized response limit before exposing any transcript route.

The renderer selects cues from the current audio position rather than a separate wall clock. Seek and playback speed therefore require no transcript retiming.

## Future transport

A later phase will expose authorized requests for bounded windows, distinguish pending, ready, failed, and stale revisions, and publish a client-facing API specification. Job submissions must be idempotent and bounded. The service must never accept an arbitrary media URL from a client or forward credentials to a redirected origin.

Do not enable access to cached transcripts merely because the caller knows an item ID. Each request must be scoped to the configured source and the caller's authorized library access. The operator token in the foundation is not a substitute for this authorization model.

Engine or network failure must not interfere with audiobook audio playback.
