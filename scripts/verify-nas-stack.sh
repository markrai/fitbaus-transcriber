#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${TRANSCRIBER_IMAGE:?Set TRANSCRIBER_IMAGE}"
: "${TRANSCRIBER_TOKEN:?Set TRANSCRIBER_TOKEN}"
umask 077
scratch=$(mktemp -d)
project="fitbaus-nas-test-$$"
export AUDIOBOOKSHELF_URL=http://192.168.1.20:13378
export AUDIOBOOKSHELF_SOURCE_ID=home-library
export TRANSCRIBER_LAN_BIND=127.0.0.1
export TRANSCRIBER_HOST_PORT=18766
compose=(docker compose --project-name "$project" --env-file /dev/null -f "$scratch/compose.yaml")
cleanup() {
    "${compose[@]}" down -v --remove-orphans || true
    rm -rf -- "$scratch"
}
trap cleanup EXIT

python - "$scratch/compose.yaml" <<'PY'
import os
from pathlib import Path
import sys

template = Path("compose.nas.yaml").read_text()
for placeholder, value in {
    "ghcr.io/markrai/fitbaus-transcriber:REPLACE_WITH_RELEASE": os.environ["TRANSCRIBER_IMAGE"],
    "REPLACE_WITH_RANDOM_TOKEN": os.environ["TRANSCRIBER_TOKEN"],
    "REPLACE_WITH_AUDIOBOOKSHELF_URL": os.environ["AUDIOBOOKSHELF_URL"],
    "127.0.0.1:8765:8765": "127.0.0.1:18766:8765",
}.items():
    assert template.count(placeholder) == 1, placeholder
    template = template.replace(placeholder, value)
assert "REPLACE_WITH_" not in template
Path(sys.argv[1]).write_text(template)
PY
docker compose --project-name "$project" --env-file /dev/null -f compose.release.yaml config --format json > "$scratch/release.json"
"${compose[@]}" config --format json > "$scratch/nas.json"
python - "$scratch/release.json" "$scratch/nas.json" <<'PY'
import json
import sys
from pathlib import Path

release, nas = (json.loads(Path(path).read_text()) for path in sys.argv[1:])
assert release == nas, "NAS template diverges from release stack"
PY

"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120
"${compose[@]}" exec -T transcriber python -m transcriber doctor
code=$("${compose[@]}" exec -T transcriber python -m transcriber pair)
printf '%s' "$code" | python - "$scratch/device-token" 3<&0 <<'PY'
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen

with os.fdopen(3) as stream:
    code = stream.read().strip()
request = Request("http://127.0.0.1:18766/v1/pairing/redeem",
                  data=json.dumps({"code": code}).encode(),
                  headers={"Content-Type": "application/json"})
with urlopen(request, timeout=5) as response:
    token = json.load(response)["token"]
assert len(token) == 43
Path(sys.argv[1]).write_text(token)
PY
"${compose[@]}" exec -T transcriber python -c 'from pathlib import Path; Path("/data/nas-probe").write_text("persistent")'
"${compose[@]}" up -d --no-build --pull never --force-recreate --wait --wait-timeout 120
"${compose[@]}" exec -T transcriber python -c 'from pathlib import Path; assert Path("/data/nas-probe").read_text() == "persistent"'
python - "$scratch/device-token" <<'PY'
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen

token = Path(sys.argv[1]).read_text()
request = Request("http://127.0.0.1:18766/v1/capabilities",
                  headers={"Authorization": "Bearer " + token})
with urlopen(request, timeout=5) as response:
    caps = json.load(response)
assert caps["default_model"] == "tiny"
assert caps["engine_ready"]
assert caps["paired_model_management"]
print("NAS stack parity, pairing and recreation checks passed.")
PY
