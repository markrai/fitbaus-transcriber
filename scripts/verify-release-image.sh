#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${TRANSCRIBER_IMAGE:?Set TRANSCRIBER_IMAGE}"
: "${TRANSCRIBER_TOKEN:?Set TRANSCRIBER_TOKEN}"

export COMPOSE_PROJECT_NAME="fitbaus-release-test-$$"
export TRANSCRIBER_LAN_BIND=127.0.0.1
export TRANSCRIBER_HOST_PORT=18765
export AUDIOBOOKSHELF_URL=
export AUDIOBOOKSHELF_SOURCE_ID=
compose=(docker compose --env-file /dev/null -f compose.release.yaml)
scratch=$(mktemp -d)
cleanup() {
    "${compose[@]}" down -v --remove-orphans || true
    rm -rf -- "$scratch"
}
trap cleanup EXIT

docker compose --env-file /dev/null -f compose.yaml config --format json > "$scratch/source.json"
"${compose[@]}" config --format json > "$scratch/release.json"
python - "$scratch/source.json" "$scratch/release.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as source_file, open(sys.argv[2]) as release_file:
    source = json.load(source_file)
    release = json.load(release_file)
assert source["services"]["transcriber"].pop("build")
assert release["services"]["transcriber"].pop("image")
assert source == release, "Source and release Compose configuration diverged"
PY

"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120
curl -fsS http://127.0.0.1:18765/healthz
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18765/readyz)" = 503
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18765/v1/capabilities)" = 401
curl -fsS -H "Authorization: Bearer $TRANSCRIBER_TOKEN" http://127.0.0.1:18765/v1/capabilities |
    python -c 'import json,sys; d=json.load(sys.stdin); assert d["transcription"] is False; assert d["model_present"] is True; assert d["default_model"] == "tiny"; assert [(m["id"],m["installed"]) for m in d["models"]] == [("tiny",True),("base",False),("small",False)]'
"${compose[@]}" exec -T transcriber python -m transcriber pair | grep -Ex '[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}'

if "${compose[@]}" exec -T transcriber python -m transcriber doctor > "$scratch/doctor.json"; then
    echo 'Doctor unexpectedly reported readiness without a source.' >&2
    exit 1
fi
python -c 'import json,sys; d=json.load(open(sys.argv[1])); assert not d["ready"]; assert d["readiness"]["reason"] == "source_not_configured"' "$scratch/doctor.json"
"${compose[@]}" run --rm --no-deps --pull never -T \
    -e AUDIOBOOKSHELF_URL=http://192.168.1.20:13378 -e AUDIOBOOKSHELF_SOURCE_ID=ci-source \
    transcriber check-config
"${compose[@]}" exec -T transcriber python -c 'from pathlib import Path; Path("/data/backup-probe").write_text("persistent")'
"${compose[@]}" stop -t 30 transcriber
"${compose[@]}" run --rm --no-deps --pull never -T --entrypoint tar transcriber -C /data -cf - . > "$scratch/backup.tar"
tar -tf "$scratch/backup.tar" | grep -Fx './backup-probe'
test "$(tar -xOf "$scratch/backup.tar" ./backup-probe)" = persistent
"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120
"${compose[@]}" exec -T transcriber python -c 'from pathlib import Path; assert Path("/data/backup-probe").read_text() == "persistent"'

espeak-ng -s 140 -w "$scratch/narration.wav" "Good morning. The sun is shining. This is a test of audiobook transcription."
chmod 644 "$scratch/narration.wav"
"${compose[@]}" exec -T transcriber python -c 'from pathlib import Path; from transcriber.model import model_directory; p=model_directory(Path("/data")); assert p.is_relative_to("/opt/fitbaus-models"); assert not Path("/data/models").exists()'
"${compose[@]}" run --rm --no-deps --pull never -e HF_HUB_OFFLINE=1 transcriber prepare-model
"${compose[@]}" exec -T transcriber python -c 'from pathlib import Path; assert not Path("/data/models").exists()'
for probe in verify_cpu.py verify_api.py; do
    "${compose[@]}" run --rm --no-deps --pull never --entrypoint python \
        -e HF_HUB_OFFLINE=1 -e PYTHONPATH=/app \
        -v "$scratch/narration.wav:/narration.wav:ro" \
        -v "$PWD/tests/$probe:/probe.py:ro" transcriber /probe.py /narration.wav
done

bash scripts/verify-nas-stack.sh
