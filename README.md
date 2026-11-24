# FitBaus Transcriber

A standalone, self-hosted companion for timestamped audiobook narration. Designed for Audiobookshelf and any client that can consume a book-relative transcript.

FitBaus audiobook playback does not require this service. Transcription is an optional enhancement, not a prerequisite for listening.

## Development status

The foundation, Audiobookshelf source adapter, and CPU speech recognition engine are under development. An operator can now transcribe a bounded audiobook window locally using the CLI. The operator CLI now supports durable job submission, execution and persistent transcript caching. The asynchronous REST API now accepts bounded transcription jobs, reports status and serves completed transcript windows. Versioned Linux AMD64 container publishing is configured for GitHub releases. The first public image requires a published release and successful publishing workflow.

## Install a published container image

Published releases are distributed through [GitHub Container Registry](https://github.com/markrai/fitbaus-transcriber/pkgs/container/fitbaus-transcriber). This path requires Docker with Compose v2 on Linux AMD64, not a local Python environment, Git checkout or Docker build. ARM64 is not yet qualified.

After a release and its container publishing workflow have succeeded, choose its exact tag from [Releases](https://github.com/markrai/fitbaus-transcriber/releases). The example below uses `v0.1.0` as an illustrative tag; replace it with an available release.

```sh
mkdir -p fitbaus-transcriber
cd fitbaus-transcriber
VERSION=v0.1.0
curl -fsSLo compose.release.yaml "https://raw.githubusercontent.com/markrai/fitbaus-transcriber/$VERSION/compose.release.yaml"
(umask 077; set -C; printf 'TRANSCRIBER_TOKEN=%s\nTRANSCRIBER_IMAGE=ghcr.io/markrai/fitbaus-transcriber:%s\n' "$(openssl rand -hex 32)" "$VERSION" > .env)
```

Set `AUDIOBOOKSHELF_URL`, `AUDIOBOOKSHELF_SOURCE_ID` and, for access from the bike on a trusted LAN, `TRANSCRIBER_LAN_BIND` in `.env` as described below. The default published port is 8765 and the default bind is loopback. Do not expose plain HTTP to the public internet.

```sh
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d --wait --wait-timeout 120
docker compose -f compose.release.yaml exec transcriber python -m transcriber pair
```

Tiny is bundled in a read-only image directory. No model preparation or runtime download is required. The queue, cached transcripts, pairing records and any separately provisioned models remain in the named Compose volume. Do not run `down -v` when upgrading.

For updates, back up the data volume, download the selected release's Compose file, change only `TRANSCRIBER_IMAGE` in your existing `.env` and repeat the pull and up commands. Keep the same project name and volume. To switch from a source-build installation, use the existing installation directory and project name, retain its `.env` and volume, and set the new image variable. Do not run source and release deployments concurrently against the same volume.

Pin an image digest (`ghcr.io/markrai/fitbaus-transcriber@sha256:...`) for content-addressed deployments. Version tags are intended never to be overwritten, but registry tags are not inherently immutable. No floating `latest` tag is published in this phase.

## Model policy

| Model | Distribution | Intended use |
| --- | --- | --- |
| Tiny | Bundled, default | Live Read Along |
| Base | Optional, not bundled | Candidate for live use after hardware qualification |
| Small | Optional, not bundled | Background recommended |

Only Tiny is executable in this phase. The authenticated capabilities response advertises the pinned model inventory and download progress. Optional model downloads are managed by the local server operator; live model activation, remote administration and prepared-transcript playback are later phases. Qualified models can be selected for background jobs. Hardware qualification is available through the local CLI. Medium and Large-v3 are not supported. Tiny retains its previous engine ID and language profile so existing cached transcripts remain compatible. Model weights are from [Systran's Faster-Whisper Tiny](https://huggingface.co/Systran/faster-whisper-tiny/tree/d90ca5fe260221311c53c58e660288d3deb8d356), a conversion of OpenAI Whisper under the MIT license. The corresponding [license notice](licenses/whisper-MIT.txt) is included in the image.

### Manage optional models

Start a download on the server using the operator CLI. For image-based installations, use `docker compose -f compose.release.yaml` instead of `docker compose`.

```sh
docker compose exec transcriber python -m transcriber download-model base
docker compose exec transcriber python -m transcriber models
docker compose exec transcriber python -m transcriber download-model small
docker compose exec transcriber python -m transcriber cancel-model-download small
```

The start command admits a download and returns immediately. Use `models` to inspect progress and the terminal result; a second model cannot start until the first has finished. Downloads use fixed upstream revisions, verify file sizes and upstream SHA-256 (large files) or Git blob hashes, and atomically install into the data volume. Interrupted or cancelled staging is discarded; explicitly start again to retry. There is no automatic retry or partial-download resumption. The default optional-model quota is 1024 MiB (`TRANSCRIBER_MODEL_QUOTA_MIB`, accepted range 128–4096); at least 256 MiB of free disk is reserved during downloads. A 30-minute deadline bounds each download. Tiny stays the active inference model. Installed models are not automatically activated or deleted. A download can share the server's CPU, disk and network with playback, so avoid large downloads during latency-sensitive reading.

Paired clients can inspect inventory but cannot initiate or cancel downloads yet. The local operator command uses the existing operator token internally over loopback. Do not copy that token to ordinary clients. See [model management API](docs/MODEL-MANAGEMENT.md).

### Benchmark installed models

Run during a maintenance interval with no active Read Along jobs. Use a representative five-minute 16 kHz mono PCM16 WAV file. The benchmark uses the same model revisions and inference configuration as the service, two fresh subprocesses per installed model, and the configured inference deadline. It does not download models, activate them or store audiobook text.

```sh
docker compose exec -T transcriber python -m transcriber benchmark --model all --wav-stdin < sample.wav
docker compose exec -T transcriber python -m transcriber qualification
```

See [hardware qualification](docs/QUALIFICATION.md) for the timing and memory admission policy. The benchmark shares the exclusive inference lock with playback, and should not be run during a workout. Tiny remains the default and only live inference model; qualified optional models can run background jobs.

### Prepare an audiobook in the background

```sh
docker compose exec transcriber python -m transcriber background-start --item-id BOOK_ID --model small
docker compose exec transcriber python -m transcriber background-status --job-id JOB_ID
docker compose exec transcriber python -m transcriber background-resume --job-id JOB_ID
```

Choose username/password (default) or an existing access token/API key at the interactive prompt. Passwords are exchanged directly with the configured Audiobookshelf server and are never saved. HTTPS is recommended; HTTP login requires explicit confirmation. Each job can process a whole book or start at an aligned five-minute offset using `--start-ms`. Live Read Along takes priority and can preempt background work. Completed windows and progress are stored together in a separate bounded staging database; these results are not yet used by Read Along. A restart or expired in-memory credential requires an explicit resume. Use `background-list --item-id BOOK_ID`, `background-pause`, `background-cancel` or `background-discard` as needed. See [background job API](docs/BACKGROUND-JOBS.md).

## Publishing releases

Maintainers publish a GitHub release with a tag such as `v0.1.0`. The container workflow validates the tag, tests source, builds the Linux AMD64 image, and runs the image-based Compose smoke test, pairing check and real offline CPU transcription probes before pushing the version tag. It checks for an existing tag and refuses an observed overwrite. The image includes the AGPL license and OCI labels identifying the exact source revision, source repository and version. Corresponding source is available in this repository at the labeled revision.

On the first successful publication, verify the package is public in GitHub package settings; a public source repository does not guarantee public package visibility. Confirm an unauthenticated pull from a clean machine before announcing availability. PR CI exercises the same image verification path without publishing or requiring package write access.

## Run the foundation locally

Requirements: Docker and Docker Compose, or Python 3.12 for development.

```sh
git clone https://github.com/markrai/fitbaus-transcriber.git
cd fitbaus-transcriber
(umask 077; printf 'TRANSCRIBER_TOKEN=%s\n' "$(openssl rand -hex 32)" > .env)
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

## Update an existing deployment

From your cloned repository:

```sh
git pull --ff-only
./deploy.sh
```

The deployment script builds the current checkout, recreates the service if needed, waits for Docker health, and reports transcription readiness separately. It retains the existing Compose data volume and does not change `.env`, download the model again, or pull Git changes automatically. A healthy service without a configured source or prepared model reports a readiness warning. A failed build or unhealthy container exits unsuccessfully. Run `bash deploy.sh` if your checkout does not preserve executable permissions.

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

The Docker image includes the pinned multilingual Faster-Whisper Tiny checkpoint. Inference uses local CTranslate2 CPU INT8; audiobook audio is not sent to any third-party service. The legacy `prepare-model` command remains available for non-container development and is a no-op when the bundled model is present. Existing downloaded Tiny files remain untouched.

Run a 60-second test on a book (substitute its library item ID):

```sh
docker compose run --rm --no-deps transcriber transcribe --item-id YOUR_BOOK_ID --start-ms 0 --end-ms 60000
```

Choose username/password or an existing access token/API key at the interactive prompt. For scripted use, `--token-stdin` reads the credential from standard input; never put it in command-line arguments. The CLI returns a JSON transcript window to standard output. If the book is shorter than 60 seconds, reduce the end time. A failed operation produces no ready transcript. Each invocation processes at most five minutes of book time. Audio ingestion still downloads the relevant complete source track(s), subject to the 512 MiB per-track limit; partial remote audio reads are a future optimization.

Optional environment settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| TRANSCRIBER_CPU_THREADS | 2 | Inference threads (1–8) |
| TRANSCRIBER_CPU_LIMIT | 2.0 | Docker CPU quota |
| TRANSCRIBER_MEMORY_LIMIT | 2g | Docker memory cap |
| TRANSCRIBER_LANGUAGE | auto | Detect language or set a lowercase two-letter code such as en |
| TRANSCRIBER_DECODE_TIMEOUT | 120 | FFmpeg timeout per track slice, in seconds (10–600) |

This slice targets Linux containers with a seekable local audio file, FFmpeg and CPU wheels. Only one inference worker is admitted per shared data directory. The service retains the queue lock until the worker exits; source credentials are passed through a private stdin pipe, not persisted or placed on the command line. Fresh workers reload Tiny for every uncached window; cache hits do not spawn a worker. The job runner supervises each uncached window in a fresh process group with a configurable 120-second deadline (`TRANSCRIBER_INFERENCE_TIMEOUT`, 10–600 seconds). The worker is terminated on timeout or cancellation, and a subsequent window can start a new process. This is process isolation, not a separate memory cgroup; container-wide OOM remains possible. The default model is deliberately small for an initial CPU baseline; accuracy and throughput need measurement on the actual server before selecting production profiles.

## Durable jobs and cache

The existing `transcribe` command now submits an idempotent job, runs it, and returns its transcript. Repeating the same authorized request reuses the cached result if available.

For separate submission and execution:

```sh
docker compose run --rm --no-deps transcriber submit --item-id YOUR_BOOK_ID --start-ms 0 --end-ms 60000
docker compose run --rm --no-deps transcriber run-job --job-id YOUR_JOB_ID
docker compose run --rm --no-deps transcriber read-job --job-id YOUR_JOB_ID
```

Each command prompts for an Audiobookshelf credential (or accepts `--token-stdin`). The queue stores job identities, state, attempt counts and results in SQLite under the persistent data volume. It never persists upstream credentials. Access and revision are checked before execution and before returning any cached transcript. Changing the engine profile, language, audio revision or window produces a different job ID.

An exclusive Linux file lock prevents concurrent inference against the same data directory. On acquiring the worker lock, interrupted running jobs are recovered as queued unless their three-attempt budget is exhausted. Resume explicitly using `run-job` or repeat an authorized HTTP submission with a fresh credential; credentials are never recovered from disk. A failed job can be retried up to three total attempts.

| Variable | Default |
| --- | --- |
| TRANSCRIBER_MAX_JOBS | 512 |
| TRANSCRIBER_MAX_ACTIVE_JOBS | 64 |
| TRANSCRIBER_CACHE_MIB | 128 |

Completed and failed jobs are evicted by recency when admission or result storage reaches its limits. Queued and running jobs are never evicted. Each result is capped at 1 MiB. The cache limit counts serialized transcript payloads; SQLite metadata, transaction journal, model files and temporary audio are additional disk usage. SQLite retains allocated pages for reuse after eviction. Use the same queue limits for all invocations sharing the data directory. Do not run this experimental queue on a network filesystem without reliable Linux file locking.

The HTTP dispatcher runs one job at a time, in submission order. Pending credentials are memory-only and expire for dispatch after ten minutes. No automatic credential renewal or cancellation of running inference is provided. Use `discard-job --job-id YOUR_JOB_ID` locally (when no worker owns the data directory) to remove abandoned queued or failed jobs for deleted or inaccessible items.

## API

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| GET /healthz | None | Process liveness |
| GET /readyz | None | HTTP job admission readiness |
| GET /v1/capabilities | Bearer token | Version, source ID and available functionality |
| GET /v1/items/{item_id}/manifest | Both credentials | Authorized audio identity and timeline |
| POST /v1/jobs | Both credentials | Submit or resume an exact transcript window |
| GET /v1/jobs/{job_id} | Both credentials | Job status and dispatch state |
| GET /v1/jobs/{job_id}/transcript | Both credentials | Completed transcript window |
| DELETE /v1/jobs/{job_id} | Both credentials | Discard queued or failed job |

See [the transcript contract](docs/TRANSCRIPT-CONTRACT.md). The transcriber token is not an Audiobookshelf user credential. See [the job API](docs/JOB-API.md) for request examples, retry rules and response semantics. Readiness means a configured source, present model files and live dispatcher; it is not a guarantee of upstream connectivity or successful model loading.

## Development

```sh
python -m unittest discover -s tests -v
```

## Roadmap

1. Headless service foundation, deployment and transcript contract.
2. Audiobookshelf source authorization, audio identity and bounded audio ingestion.
3. CPU speech recognition engine and durable revision/profile-aware job cache (implemented); asynchronous incremental transcript API (implemented).
4. Versioned container publishing and bundled Tiny (implemented; first public release pending), optional model management and streamlined self-hosting installer (next).
5. Optional FitBaus connection and on-demand Read Along.
6. GPU acceleration and optional remote workers.

No cloud service, web administration interface or FitBaus installation is required by the transcriber itself. Ordinary playback remains independent of transcription.

## License

AGPL-3.0. See [LICENSE](LICENSE).


## Pair a FitBaus bike

With the service running, use `docker compose exec transcriber python -m transcriber pair`. Enter the displayed XXXX-XXXX code in Settings → Transcriber on the bike. Codes expire after ten minutes, are single-use, and are invalidated after five incorrect attempts. Issuing another code replaces the previous code. Restarting the service invalidates outstanding codes, not paired credentials. The bike receives its own random 256-bit bearer credential; the operator token stays on the server. Device credential hashes persist in the data volume (maximum 32 credentials). To revoke all paired credentials, run `docker compose exec transcriber python -m transcriber revoke-pairings`; the operator token and cached transcripts remain unaffected. Disconnect on the bike removes its local credential; revoke-pairings also invalidates credentials from lost devices.

Use HTTPS for untrusted networks. Plain HTTP pairing and bearer traffic are not encrypted; bind only to a trusted LAN address for local testing and do not expose the port publicly. Pairing does not grant Audiobookshelf access: each item/job request still requires the caller's Audiobookshelf credential.
