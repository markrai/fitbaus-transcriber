# Background jobs

A background job prepares a whole audiobook or its remaining aligned windows using Tiny or an installed, currently qualified Base or Small. Tiny remains the only live inference model. All routes require the transcriber bearer and the caller's Audiobookshelf credential, as for the existing job API. Item access is checked on every request; status and management are shared among callers authorized for that item, not private per-user queues.

## API

POST /v1/background accepts:

```json
{
  "source_id": "home-library",
  "item_id": "book-1",
  "audio_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "model": "small",
  "start_ms": 0
}
```

The server obtains duration from the authenticated manifest. The optional start defaults to zero and must be aligned to a 300000 ms window, less than the book duration. Maximum supported duration is 100 hours. The deterministic parent identity includes source, item, revision, model engine/language profile, start and duration. Duplicate submission does not create another job; paused or failed jobs need explicit resume, and cancelled jobs must be discarded before starting the same identity again.

GET /v1/items/{item_id}/background lists that authorized item's jobs. GET /v1/background/{job_id} returns the current status including processed_ms, total_ms, next_ms, model, profile, state and error. POST /v1/background/{job_id}/resume refreshes authorization and resumes paused, failed or pending work. POST /v1/background/{job_id}/pause stops further work and interrupts active inference; POST /v1/background/{job_id}/cancel permanently stops that job but retains completed windows. POST /v1/background/{job_id}/discard removes a paused, failed, cancelled or completed job and its staged windows; active or pending jobs must first be paused or cancelled. Discard can be retried after an active process has exited. Status and cleanup of obsolete revisions are permitted only with current item access. Resume requires the original exact revision and profile.

HTTP 202 indicates queued/processing or deferred admission; 200 returns a status or completed management operation. Poll for progress. HTTP 409 includes model_not_qualified, stale_revision, profile_changed, background_not_discardable, background_busy and background_storage_full. HTTP 429 indicates the parent limit. Storage failure is 503. The existing health, manifest, live jobs and pairing contracts are unchanged.

## Persistence and scheduling

The separate SQLite database stores up to 32 parent records and 128 MiB of serialized staged transcript payloads across all parents (not including database overhead). Each result is limited to 1 MiB. No unbounded child queue is created. The next window is selected from the committed cursor, at most 300000 ms long, and the final partial window ends at the manifest duration. Result and cursor advance atomically. No automatic eviction of completed progress. Discard explicitly frees logical staging capacity; allocated database pages may be retained for reuse. Committed windows are directly readable through the existing live job API, independent of the live LRU cache. Upgrades index existing windows in place. An exact source/item/revision/window and compatible language/engine profile match returns HTTP 200 and a ready prepared job with its original model profile, without scheduling inference. Small takes precedence over Base, then Tiny among eligible prepared windows. This is a deterministic policy, not an accuracy guarantee. Partial books, including paused or cancelled jobs, can supply their committed windows. Unprepared ranges fall back to normal Tiny live inference. Prepared IDs are distinct from live job IDs and survive restart; discarding a parent deletes its prepared windows. Every lookup rechecks source access and revision. No extra copies are made in the live cache.

One dispatcher and the existing exclusive inference lock serialize live and background work. Live jobs, including lookahead, always take priority. A new uncached live request signals cancellation of an active background worker; the interrupted window does not advance progress and is restarted later. Already committed windows are retained. Background jobs use FIFO ordering, and may starve while live work remains pending. Worker termination and source authorization calls can add latency; this is not a hard real-time guarantee. A temporarily busy worker or insufficient memory delays dispatch with a five-second backoff. Other failures stop the parent for explicit resume; preemption does not consume the live retry budget. A benchmark or external CLI worker shares the same lock and should be run only during maintenance.

Optional models must be installed and have a current live_candidate or background_candidate qualification. Memory admission is repeated before each window. All windows use supervised fresh workers with the existing configured deadline, offline model loading and exact model identity. Tiny's live cache profile is unchanged.

## Audio reuse

For consecutive windows wholly inside the same track, one parent-owned anonymous temporary file holds a verified complete track of at most 512 MiB. The child receives the descriptor, not a persisted path. It checks the current manifest before reuse and validates file size. Reuse is scoped to parent, track, revision and the same credential; a different credential or track starts a new spool. Cross-track windows use the original bounded materialization path. The spool is not durable, is closed when the job stops or becomes idle, and is reclaimed when the owning processes exit. A 256 MiB free-disk reserve is checked before creating the spool. The existing full-track transfer and decode limits still apply. There is no remote range decoding yet.

## Authorization lifecycle

Background source tokens are memory-only and remain valid for dispatch for the authorized job's lifetime, with no artificial ten-minute or 24-hour limit. They are never stored in the database or command arguments. Local CLI start and resume automatically request renewable credentials on username/password login. An upstream 401 triggers refresh when supported, rotating credentials in memory; otherwise the job waits for fresh authorization. API keys and legacy access tokens remain usable for as long as the source accepts them. Only the loopback operator may supply the refresh header. The former 24-hour lease header is accepted for compatibility but no longer limits job lifetime. Pause, cancel, completion, terminal failure and restart release credentials. Refresh rejection or permission denial stops dispatch until reauthorized. The job remains restricted to its original audio identity, though the upstream credential itself may grant broader account access. The service revalidates the manifest before and after each window. Restart restores committed progress but requires fresh authorization. The separate live job credential expiry and inference watchdog are unchanged.

The CLI supports background-start, background-list, background-status, background-resume, background-pause, background-cancel and background-discard. All support username/password login (default) or a hidden access token/API key prompt. Scripts retain --token-stdin. Start and resume request a refresh token when supported; neither passwords nor tokens are persisted. HTTP login requires explicit confirmation. No unattended flag is needed; the legacy CLI flag is silently accepted for compatibility. Background status reports authorization_active and authorization_renewable. authorization_remaining_seconds is null while authorized (no FitBaus scheduling expiry), or zero without authorization. This does not override upstream token expiry or revocation. The source and operator tokens are not printed.
