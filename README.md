# FitBaus Transcriber

A standalone, self-hosted companion for timestamped audiobook narration. Designed for Audiobookshelf and any client that can consume a book-relative transcript.

FitBaus audiobook playback does not require this service. Transcription is an optional enhancement, not a prerequisite for listening.

## Development status

The initial service foundation is under development. This slice provides Docker deployment, configuration, authenticated capability discovery and a versioned transcript contract. It does **not** yet perform speech recognition, connect to Audiobookshelf, or serve transcripts. No published production image is available.

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

The container runs as a non-root user, with a read-only root filesystem and persistent data volume. The current CPU and memory limits are for the foundation only; transcription profiles will have independent resource requirements.

## API

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| GET /healthz | None | Process liveness |
| GET /readyz | None | Engine readiness; currently HTTP 503 |
| GET /v1/capabilities | Bearer token | Version and available functionality |

See [the transcript contract](docs/TRANSCRIPT-CONTRACT.md). The current token is an operator credential, not an Audiobookshelf user credential. No transcript access is enabled until source authorization is implemented.

## Development

```sh
python -m unittest discover -s tests -v
```

## Roadmap

1. Headless service foundation, deployment and transcript contract.
2. Audiobookshelf source authorization, audio identity and bounded audio ingestion.
3. CPU speech recognition engine, durable queue, incremental transcript generation and revision-aware cache.
4. Versioned transcript window API, container images and streamlined self-hosting documentation.
5. Optional FitBaus connection and on-demand Read Along.
6. GPU acceleration and optional remote workers.

No cloud service, web administration interface or FitBaus installation is required by the transcriber itself. Ordinary playback remains independent of transcription.

## License

AGPL-3.0. See [LICENSE](LICENSE).
