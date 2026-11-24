# FitBaus Transcriber

A standalone, self-hosted companion for timestamped audiobook narration. Designed for Audiobookshelf and any client that can consume a book-relative transcript.

FitBaus audiobook playback does not require this service. Transcription is an optional enhancement, not a prerequisite for listening.

## Development status

The foundation, Audiobookshelf source adapter, and CPU speech recognition engine are under development. An operator can now transcribe a bounded audiobook window locally using the CLI. The operator CLI now supports durable job submission, execution and persistent transcript caching. The service does **not** yet offer HTTP transcription jobs or transcript serving. No published production image is available.

## Run the foundation locally

Requirements: Docker and Docker Compose, or Python 3.12 for development.

```sh
git clone https://github.com/markrai/fitbaus-transcriber.git
cd fitbaus-transcriber
umask 077
printf 'TRANSCRIBER_TOKEN=%s\n' "$(openssl rand -hex 32)" > .env
docker compose up --build -d
curl http://127.0.0.1:8765/healthz
```

The default host binding is loopback. To use the service from another device on a trusted LAN, set `TRANSCRIBER_LAN_BIND` to the server's LAN address in `.env` and recreate the container. For remote use, put the service behind HTTPS with appropriate access controls. Never expose the unencrypted bearer-token endpoint publicly.

```sh
set -a
. ./.env
set +a
curl -H "Authorization: Bearer $TRANSCRIBER_TOKEN" http://127.0.0.1:8765/v1/capabilities
unset TRANSCRIBER_TOKEN
```

Keep `.env` private. Environment variables are visible to Docker administrators. The process also supports `TRANSCRIBER_TOKEN_FILE` instead of `TRANSCRIBER_TOKEN` for file-based secret management. The Compose example uses the environment variable.

The container runs as a non-root user, with a read-only root filesystem and persistent data volume. The CPU profile defaults to two CPU threads, a two-core CPU quota and a 2 GiB memory limit. These are initial configuration defaults, not measured minimum requirements.

## Configure Audiobookshelf

Set these optional variables in `.env`:

```dotenv
AUDIOBOOKSHELF_URL=http://192.168.1.10:13378
AUDIOBOOKSHELF_SOURCE_ID=home-library
```

Use the address reachable from inside the container. A Docker service name on a shared network also works. HTTPS and the standard `/audiobookshelf` subpath are supported. For HTTP, use only a trusted private network. Assign a new source ID if you replace the underlying server with a different library. Restart with `docker compose up -d` after configuration changes.

The transcriber does not store an Audiobookshelf admin key. Each manifest request requires both the transcriber access token and the caller's own Audiobookshelf access token or API key. The source validates the caller's permission to access that exact item. Unauthorized responses are not bypassed by cache hits. Source credentials must not be put in URLs or logs.

A client can now request `GET /v1/items/{item_id}/manifest` with `Authorization: Bearer <transcriber-token>` and `X-Audiobookshelf-Token: <audiobookshelf-token>`. No arbitrary source URL or audio path can be supplied by a client. Audio is not exposed through a public download endpoint; ingestion is an internal adapter for the forthcoming engine.

## CPU transcription qualification

After configuring Audiobookshelf, provision the model once:

```sh
docker compose build
docker compose run --rm --no-deps transcriber prepare-model
```

This explicitly downloads a pinned multilingual Faster-Whisper tiny checkpoint into the persistent data volume. Subsequent inference loads it locally using CTranslate2 CPU INT8; audiobook audio is not sent to any third-party service. The model provisioning step needs internet access.

Run a 60-second test on a book (substitute its library item ID):

```sh
docker compose run --rm --no-deps transcriber transcribe --item-id YOUR_BOOK_ID --start-ms 0 --end-ms 60000
```

Enter the Audiobookshelf user's API key or access token at the hidden prompt. For scripted use, `--token-stdin` reads the credential from standard input; never put it in command-line arguments. The CLI returns a JSON transcript window to standard output. If the book is shorter than 60 seconds, reduce the end time. A failed operation produces no ready transcript. Each invocation processes at most five minutes of book time. Audio ingestion still downloads the relevant complete source track(s), subject to the 512 MiB per-track limit; partial remote audio reads are a future optimization.

Optional environment settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| TRANSCRIBER_CPU_THREADS | 2 | Inference threads (1–8) |
| TRANSCRIBER_CPU_LIMIT | 2.0 | Docker CPU quota |
| TRANSCRIBER_MEMORY_LIMIT | 2g | Docker memory cap |
| TRANSCRIBER_LANGUAGE | auto | Detect language or set a lowercase two-letter code such as en |
| TRANSCRIBER_DECODE_TIMEOUT | 120 | FFmpeg timeout per track slice, in seconds (10–600) |

This slice targets Linux containers with a seekable local audio file, FFmpeg and CPU wheels. Only one inference worker is admitted per shared data directory. The job runner does not yet enforce a hard inference deadline. The default model is deliberately small for an initial CPU baseline; accuracy and throughput need measurement on the actual server before selecting production profiles.

## Durable jobs and cache

The existing `transcribe` command now submits an idempotent job, runs it, and returns its transcript. Repeating the same authorized request reuses the cached result if available.

For separate submission and execution:

```sh
docker compose run --rm --no-deps transcriber submit --item-id YOUR_BOOK_ID --start-ms 0 --end-ms 60000
docker compose run --rm --no-deps transcriber run-job --job-id YOUR_JOB_ID
docker compose run --rm --no-deps transcriber read-job --job-id YOUR_JOB_ID
```

Each command prompts for an Audiobookshelf credential (or accepts `--token-stdin`). The queue stores job identities, state, attempt counts and results in SQLite under the persistent data volume. It never persists upstream credentials. Access and revision are checked before execution and before returning any cached transcript. Changing the engine profile, language, audio revision or window produces a different job ID.

An exclusive Linux file lock prevents concurrent inference against the same data directory. On acquiring the worker lock, interrupted running jobs are recovered as queued unless their three-attempt budget is exhausted. Resume explicitly using `run-job` with a fresh credential; the queue does not automatically execute jobs after restart. A failed job can be retried up to three total attempts.

| Variable | Default |
| --- | --- |
| TRANSCRIBER_MAX_JOBS | 512 |
| TRANSCRIBER_MAX_ACTIVE_JOBS | 64 |
| TRANSCRIBER_CACHE_MIB | 128 |

Completed and failed jobs are evicted by recency when admission or result storage reaches its limits. Queued and running jobs are never evicted. Each result is capped at 1 MiB. The cache limit counts serialized transcript payloads; SQLite metadata, transaction journal, model files and temporary audio are additional disk usage. SQLite retains allocated pages for reuse after eviction. Use the same queue limits for all invocations sharing the data directory. Do not run this experimental queue on a network filesystem without reliable Linux file locking.

No background dispatcher, HTTP job interface, cancellation or automatic credential renewal is provided yet. This slice is for operator qualification and the upcoming asynchronous API.

## API

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| GET /healthz | None | Process liveness |
| GET /readyz | None | HTTP transcription readiness; currently HTTP 503 |
| GET /v1/capabilities | Bearer token | Version, source ID and available functionality |
| GET /v1/items/{item_id}/manifest | Both credentials | Authorized audio identity and timeline |

See [the transcript contract](docs/TRANSCRIPT-CONTRACT.md). The transcriber token is not an Audiobookshelf user credential. No HTTP transcript access is enabled until the authorization-preserving job API is implemented. The capability response reports model file presence separately from API readiness.

## Development

```sh
python -m unittest discover -s tests -v
```

## Roadmap

1. Headless service foundation, deployment and transcript contract.
2. Audiobookshelf source authorization, audio identity and bounded audio ingestion.
3. CPU speech recognition engine and durable revision/profile-aware job cache (implemented); asynchronous incremental transcript API (next).
4. Versioned transcript window API, container images and streamlined self-hosting documentation.
5. Optional FitBaus connection and on-demand Read Along.
6. GPU acceleration and optional remote workers.

No cloud service, web administration interface or FitBaus installation is required by the transcriber itself. Ordinary playback remains independent of transcription.

## License

AGPL-3.0. See [LICENSE](LICENSE).
