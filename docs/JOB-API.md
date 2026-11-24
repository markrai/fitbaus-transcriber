# Asynchronous job API v1

All job and manifest requests require two headers: `Authorization: Bearer <transcriber-token>` and `X-Audiobookshelf-Token: <caller-access-token-or-api-key>`. Use HTTPS for remote connections; unencrypted HTTP is intended only for trusted private networks. Do not put either credential in a URL or log. Access is rechecked with Audiobookshelf for each request, including cache hits.

## Client workflow

1. Check `GET /v1/capabilities` and `GET /readyz`. Readiness requires a configured source, prepared model files and live dispatcher. It does not perform inference or prove upstream reachability.
2. Fetch `GET /v1/items/{item_id}/manifest` for the source ID, audio revision, duration and ordered track timeline.
3. Submit a window through `POST /v1/jobs`, with JSON content type and these fields:

```json
{
  "source_id": "home-library",
  "item_id": "book-1",
  "audio_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "start_ms": 60000,
  "end_ms": 120000
}
```

Times are integer book-relative milliseconds. Maximum window length is 300,000 ms; the end cannot exceed the manifest duration. Requests are limited to 4 KiB. No arbitrary audio URL, paths, inference options or source credentials are accepted in the body. The exact source, item, revision, window and configured engine/language profile determine the job ID.

HTTP 202 means scheduled or already processing. HTTP 200 returns an existing ready or failed job without scheduling new work. The response is a snapshot: poll for authoritative current status.

```json
{
  "job_id": "<64 lowercase hex characters>",
  "source_id": "home-library",
  "item_id": "book-1",
  "audio_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "start_ms": 60000,
  "end_ms": 120000,
  "profile": "<engine profile>",
  "state": "queued",
  "dispatch": "scheduled",
  "attempts": 0,
  "max_attempts": 3,
  "error": null
}
```

4. Poll `GET /v1/jobs/{job_id}` every 2–5 seconds while waiting, backing off on overload. Do not submit on every playback tick.
5. When state is `ready`, fetch `GET /v1/jobs/{job_id}/transcript` for the [transcript window](TRANSCRIPT-CONTRACT.md). Confirm its source, item, revision and coverage match the active audiobook before rendering. A ready empty cue list represents processed silence; an unfinished or absent job is not silence.

## State and recovery

Persistent states: queued, running, ready, failed. Dispatch states: scheduled, processing, awaiting_credentials, external_or_interrupted, idle. A failed job may temporarily be scheduled during an explicit retry. One inference worker runs per shared data directory. Scheduled jobs are processed FIFO; duplicate pending submissions refresh the memory-only credential without changing queue order.

Queued credentials expire for dispatch after ten minutes. The server never persists them and does not refresh Audiobookshelf tokens automatically. Resubmit the exact window using a fresh credential to resume queued work. After a restart, interrupted running jobs recover when the exclusive worker lock is acquired; resubmission is required to dispatch them. If a separate CLI worker holds the lock, resubmit after it finishes. A credential rejected before claim can leave a job queued awaiting fresh credentials.

For failed jobs, explicitly include `"retry": true` in the same submission to schedule another attempt; ordinary polling and duplicate submissions do not retry failures. Three total attempts are allowed. A stale revision or changed profile requires a fresh manifest and new job identity. An evicted result must be submitted again.

`DELETE /v1/jobs/{job_id}` discards queued or failed jobs after checking current item access. It cannot interrupt active inference or remove completed cache entries. Cancellation of an obsolete job is permitted for a caller who still has access to the current item. For deleted items, the local operator may use `discard-job` when the inference worker is not active.

## Errors

| HTTP | Meaning |
| --- | --- |
| 400 | Invalid body, identity, or window |
| 401 | Missing or rejected credential |
| 403 | Audiobookshelf denies access |
| 404 | Unknown job, source or item |
| 408 | Request body timeout |
| 409 | Stale revision, changed profile, transcript not ready, or cancellation not allowed |
| 429 | Job or dispatcher capacity reached |
| 502 | Upstream failure or invalid source response |
| 503 | Source/model/worker or storage not ready |

Errors are JSON objects with an `error` code. No credentials or source paths are returned. Status and result access always revalidate the item. Transcription errors must never interrupt the independent audiobook player.

## Current limits

The API processes exact windows rather than automatically splitting entire books. Clients should use stable adjacent windows and request a modest lookahead. Large single tracks still require complete bounded download for each uncached job. Running inference has no hard deadline or cancellation. Credentials cannot be refreshed mid-job; subsequent authorized requests may retry a failure within the attempt budget. Model performance and resource usage on the target server remain to be measured.
