#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${TRANSCRIBER_IMAGE:?Set TRANSCRIBER_IMAGE}"
: "${TRANSCRIBER_TOKEN:?Set TRANSCRIBER_TOKEN}"

export COMPOSE_PROJECT_NAME="fitbaus-release-test"
export TRANSCRIBER_LAN_BIND=127.0.0.1
export TRANSCRIBER_HOST_PORT=18765
export AUDIOBOOKSHELF_URL=
export AUDIOBOOKSHELF_SOURCE_ID=
compose=(docker compose -f compose.release.yaml)
scratch=$(mktemp -d)
cleanup() {
    "${compose[@]}" down -v --remove-orphans
    rm -rf -- "$scratch"
}
trap cleanup EXIT

"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120
curl -fsS http://127.0.0.1:18765/healthz
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18765/readyz)" = 503
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18765/v1/capabilities)" = 401
curl -fsS -H "Authorization: Bearer $TRANSCRIBER_TOKEN" http://127.0.0.1:18765/v1/capabilities |
    python -c 'import json,sys; assert json.load(sys.stdin)["transcription"] is False'
"${compose[@]}" exec -T transcriber python -m transcriber pair | grep -Ex '[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}'

espeak-ng -s 140 -w "$scratch/narration.wav" "Good morning. The sun is shining. This is a test of audiobook transcription."
chmod 644 "$scratch/narration.wav"
"${compose[@]}" run --rm --no-deps --pull never transcriber prepare-model
for probe in verify_cpu.py verify_api.py; do
    "${compose[@]}" run --rm --no-deps --pull never --entrypoint python \
        -e HF_HUB_OFFLINE=1 -e PYTHONPATH=/app \
        -v "$scratch/narration.wav:/narration.wav:ro" \
        -v "$PWD/tests/$probe:/probe.py:ro" transcriber /probe.py /narration.wav
done
