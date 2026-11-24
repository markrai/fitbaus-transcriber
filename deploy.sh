#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 022

docker compose config --quiet

printf '%s\n' 'Building and deploying FitBaus Transcriber...'
if ! docker compose up -d --build --wait --wait-timeout 120; then
    docker compose ps -a
    printf '%s\n' 'Deployment failed. Inspect docker compose logs --tail=100 transcriber.' >&2
    exit 1
fi

docker compose ps

if ! docker compose exec -T transcriber python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8765/readyz", timeout=5).close()' 2>/dev/null; then
    printf '%s\n' 'Service is running but transcription readiness is not confirmed. Check source configuration and model provisioning.' >&2
else
    printf '%s\n' 'FitBaus Transcriber is ready.'
fi
