# Transcription failure diagnosis and retry

Live and background inference retain allowlisted engine failure codes across the worker boundary. Existing generic failures remain readable. Logs include the mode, model, audiobook-relative window, processing stage, safe exception classification, exit status when available, and elapsed time. They omit raw exception messages, subprocess stderr, tokens, audio, transcript text, source URLs and file paths.

`docker compose logs --tail=100 transcriber` displays terminal failures. The persisted job error distinguishes decode errors, incomplete PCM, model loading, invalid timestamps/text, inference exceptions, timeouts and process crashes. Diagnostics are not a claim that the underlying audio or model problem has been repaired.

The existing authenticated `POST /v1/jobs` retry flag now requeues failed live jobs before returning the accepted response. Below the three-attempt budget, attempts are retained; at exhaustion a new explicitly requested retry starts a fresh budget. Requests without retry do not reopen failed jobs. Duplicate requests while queued or running do not reset attempts. Source authorization, queue capacity, identity checks and ready results are preserved. The summary and recent recap endpoints use the same retry semantics. A background book resume still continues from its last committed window.

Older Trainer versions use the same retry flag for a bounded prefetch recovery as well as manual Retry narration, so the server cannot distinguish these origins. No automatic repeating retry loop is introduced by this change.
