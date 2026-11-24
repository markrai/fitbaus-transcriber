#!/usr/bin/env bash
set -euo pipefail
umask 077

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
ROOT=$(pwd -P)
REPO=ghcr.io/markrai/fitbaus-transcriber
ENV_FILE="$ROOT/.env"
MANIFEST="$ROOT/compose.release.yaml"
project=
stage=
restart_needed=0

die() { printf '%s\n' "$*" >&2; exit 1; }

usage() {
    printf '%s\n' 'FitBaus Transcriber' \
        'bash install.sh install VERSION' \
        'bash install.sh upgrade VERSION' \
        'bash install.sh start|stop|status|pair|check-source|logs|backup'
}

version_ok() {
    [[ $1 =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[a-zA-Z0-9][a-zA-Z0-9.-]*)?$ ]]
}

private_ip() {
    local a b c d n
    [[ $1 =~ ^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$ ]] || return 1
    a=${BASH_REMATCH[1]}; b=${BASH_REMATCH[2]}; c=${BASH_REMATCH[3]}; d=${BASH_REMATCH[4]}
    for n in "$a" "$b" "$c" "$d"; do (( n <= 255 )) || return 1; done
    [[ $1 == 127.0.0.1 ]] || (( a == 10 || (a == 172 && b >= 16 && b <= 31) || (a == 192 && b == 168) ))
}

port_ok() { [[ $1 =~ ^[1-9][0-9]{0,4}$ ]] && (( 10#$1 <= 65535 )); }

source_ok() {
    local scheme host port
    [[ $1 =~ ^(https?)://([A-Za-z0-9.-]+)(:([0-9]{1,5}))?(/audiobookshelf)?/?$ ]] || return 1
    scheme=${BASH_REMATCH[1]}; host=${BASH_REMATCH[2]}; port=${BASH_REMATCH[4]}
    [[ -z $port ]] || port_ok "$port" || return 1
    if [[ $scheme == http ]]; then
        private_ip "$host" && [[ $host != 127.0.0.1 ]]
    else
        [[ $host != localhost && $host != 127.0.0.1 ]]
    fi
}

compose_at() (
    local envfile=$1 manifest=$2 key
    shift 2
    for key in ${!TRANSCRIBER_@} ${!AUDIOBOOKSHELF_@} ${!COMPOSE_@}; do unset "$key"; done
    docker compose --project-directory "$ROOT" --project-name "$project" --env-file "$envfile" -f "$manifest" "$@"
)

compose() { compose_at "$ENV_FILE" "$MANIFEST" "$@"; }
launch() { compose up -d --no-build --pull never --wait --wait-timeout 120; }

cleanup() {
    local result=$?
    trap - EXIT
    if (( restart_needed )); then
        printf '%s\n' 'Restarting original service after interrupted maintenance.' >&2
        launch || printf '%s\n' 'Restart failed. Inspect logs and run bash install.sh start.' >&2
    fi
    if [[ -n $stage ]]; then rm -rf -- "$stage"; fi
    exit "$result"
}

env_value() {
    awk -v key="$1" 'index($0,key "=")==1 {n++; value=substr($0,length(key)+2)} END {if(n!=1) exit 1; print value}' "$2"
}

require_installation() {
    [[ -f $ENV_FILE && ! -L $ENV_FILE && -f $MANIFEST && ! -L $MANIFEST ]] ||
        die 'No managed installation found. Manual/source installations are not adopted automatically.'
    project=$(env_value COMPOSE_PROJECT_NAME "$ENV_FILE") || die 'Missing or duplicate project identity.'
    [[ $project =~ ^fitbaus-transcriber-[a-f0-9]{12}$ ]] || die 'Unrecognized managed project identity.'
    local version image
    version=$(env_value FITBAUS_RELEASE_VERSION "$ENV_FILE") || die 'Missing installed version.'
    version_ok "$version" || die 'Invalid installed version.'
    image=$(env_value TRANSCRIBER_IMAGE "$ENV_FILE") || die 'Missing image reference.'
    [[ $image =~ ^ghcr\.io/markrai/fitbaus-transcriber@sha256:[a-f0-9]{64}$ ]] || die 'Expected a pinned FitBaus image digest.'
    chmod 600 "$ENV_FILE"
}

fetch_release() {
    local version=$1 tag digest
    stage=$(mktemp -d "$ROOT/.fitbaus-stage.XXXXXXXX")
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 120 \
        "https://raw.githubusercontent.com/markrai/fitbaus-transcriber/$version/compose.release.yaml" \
        -o "$stage/compose.release.yaml" || die 'Could not fetch the selected release Compose file.'
    tag="$REPO:$version"
    docker pull "$tag" || die 'Image unavailable. Check that the release is published and public.'
    digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$tag" |
        awk -v repo="$REPO" 'index($0,repo "@sha256:")==1 {print; exit}')
    [[ $digest =~ ^ghcr\.io/markrai/fitbaus-transcriber@sha256:[a-f0-9]{64}$ ]] || die 'Unable to resolve image digest.'
    pinned_image=$digest
}

show_pairing() {
    local bind port
    bind=$(env_value TRANSCRIBER_LAN_BIND "$ENV_FILE")
    port=$(env_value TRANSCRIBER_HOST_PORT "$ENV_FILE")
    printf '\n%s\n' 'On the bike: Settings → Transcriber'
    if [[ $bind == 127.0.0.1 ]]; then
        printf '%s\n' 'Loopback only. Use your configured HTTPS reverse-proxy address on the bike.'
    else
        printf 'Protocol: http\nHost: %s\nPort: %s\n' "$bind" "$port"
    fi
    printf '%s\n' 'Enter the following single-use pairing code:'
    compose exec -T transcriber python -m transcriber pair
}

preflight() {
    compose_at "$1" "$2" config --quiet
    compose_at "$1" "$2" run --rm --no-deps --pull never -T transcriber check-config
}

require_running() {
    local running
    running=$(compose ps -q --status running transcriber) || die 'Unable to inspect service.'
    [[ -n $running ]] || die 'Start the service before maintenance.'
}

backup_data() {
    require_running
    local backup running
    [[ ! -L $ROOT/backups ]] || die 'Backup directory must not be a symlink.'
    mkdir -p "$ROOT/backups"
    backup=$(mktemp -d "$ROOT/backups/$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")
    cp -- "$ENV_FILE" "$backup/.env"
    cp -- "$MANIFEST" "$backup/compose.release.yaml"
    chmod 600 "$backup/.env"
    printf 'Stopping transcription briefly for a consistent backup: %s\n' "$backup"
    restart_needed=1
    compose stop -t 30 transcriber
    running=$(compose ps -q --status running transcriber) || die 'Unable to inspect stopped service.'
    [[ -z $running ]] || die 'Service is still running; backup aborted.'
    compose run --rm --no-deps --pull never -T --entrypoint tar transcriber -C /data -cf - . > "$backup/data.tar.partial"
    tar -tf "$backup/data.tar.partial" >/dev/null
    mv -- "$backup/data.tar.partial" "$backup/data.tar"
    printf '%s\n' 'Backup complete. Treat the archive and configuration as private.'
}

command=${1:-help}
case "$command" in
    help|-h|--help) usage; exit 0 ;;
    install|upgrade) [[ $# == 2 ]] && version_ok "$2" || die 'Supply an exact release tag.' ;;
    start|status|pair|check-source|logs|backup|stop) [[ $# == 1 ]] || die 'Unexpected arguments.' ;;
    *) usage; exit 1 ;;
esac

for tool in docker curl openssl flock tar awk; do
    command -v "$tool" >/dev/null || die "Required command missing: $tool"
done
docker compose version >/dev/null || die 'Docker Compose v2 is required.'
platform=$(docker info --format '{{.OSType}}/{{.Architecture}}') || die 'Docker is unavailable.'
[[ $platform == linux/x86_64 || $platform == linux/amd64 ]] || die 'This installer supports Linux AMD64 only.'
[[ ! -L $ROOT/.fitbaus-install.lock ]] || die 'Unsafe installation lock path.'
exec 9>"$ROOT/.fitbaus-install.lock"
flock -n 9 || die 'Another maintenance command is running.'
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ $command == install ]]; then
    [[ ! -e $ENV_FILE && ! -L $ENV_FILE && ! -e $MANIFEST && ! -L $MANIFEST && ! -e $ROOT/compose.yaml ]] ||
        die 'Existing configuration found. Nothing was overwritten.'
    [[ -t 0 ]] || die 'Installation requires an interactive terminal.'
    printf '%s\n' 'FitBaus Transcriber setup' \
        'Use the same Audiobookshelf URL as on your bike. Example: http://192.168.1.20:13378'
    read -r -p 'Audiobookshelf URL: ' source
    source_ok "$source" || die 'Enter a valid HTTPS URL or private IPv4 HTTP URL with optional /audiobookshelf path.'
    source=${source%/}
    printf '%s\n' 'Use this Docker host private IPv4 address, or 127.0.0.1 for a local HTTPS proxy.'
    read -r -p 'Transcriber bind address: ' bind
    private_ip "$bind" || die 'A private IPv4 address or 127.0.0.1 is required.'
    read -r -p 'Transcriber port [8765]: ' port
    port=${port:-8765}
    port_ok "$port" || die 'Invalid port.'
    printf '%s\n' 'HTTP is unencrypted. Use a trusted private LAN; never forward this port to the internet.'
    read -r -p 'Continue? [y/N]: ' yes
    [[ $yes == y || $yes == Y ]] || die 'Installation cancelled.'
    fetch_release "$2"
    project="fitbaus-transcriber-$(openssl rand -hex 6)"
    token=$(openssl rand -hex 32)
    [[ $project =~ ^fitbaus-transcriber-[a-f0-9]{12}$ && $token =~ ^[a-f0-9]{64}$ ]] || die 'Credential generation failed.'
    {
        printf 'COMPOSE_PROJECT_NAME=%s\nFITBAUS_RELEASE_VERSION=%s\nTRANSCRIBER_IMAGE=%s\n' "$project" "$2" "$pinned_image"
        printf 'TRANSCRIBER_TOKEN=%s\nTRANSCRIBER_LAN_BIND=%s\nTRANSCRIBER_HOST_PORT=%s\n' "$token" "$bind" "$port"
        printf 'AUDIOBOOKSHELF_URL=%s\nAUDIOBOOKSHELF_SOURCE_ID=home-library\n' "$source"
        printf '%s\n' 'TRANSCRIBER_CPU_THREADS=2' 'TRANSCRIBER_CPU_LIMIT=2.0' 'TRANSCRIBER_MEMORY_LIMIT=2g'
    } > "$stage/.env"
    unset token
    compose_at "$stage/.env" "$stage/compose.release.yaml" config --quiet
    mv -- "$stage/.env" "$ENV_FILE"
    mv -- "$stage/compose.release.yaml" "$MANIFEST"
    preflight "$ENV_FILE" "$MANIFEST"
    launch
    compose exec -T transcriber python -m transcriber doctor
    printf '%s\n' 'Service is ready. Audiobookshelf account access is not yet verified.'
    read -r -p 'Verify Audiobookshelf sign-in now? [y/N]: ' yes
    if [[ $yes == y || $yes == Y ]]; then
        compose exec transcriber python -m transcriber check-source ||
            printf '%s\n' 'Source verification failed. Run bash install.sh check-source to retry.' >&2
    fi
    show_pairing
    exit 0
fi

require_installation
case "$command" in
    start)
        preflight "$ENV_FILE" "$MANIFEST"
        launch
        compose exec -T transcriber python -m transcriber doctor
        ;;
    status)
        compose ps
        compose exec -T transcriber python -m transcriber doctor
        ;;
    pair) show_pairing ;;
    check-source) compose exec transcriber python -m transcriber check-source ;;
    logs) compose logs --tail=100 transcriber ;;
    stop)
        compose stop -t 30 transcriber
        printf '%s\n' 'Stopped. Persistent data is retained.'
        ;;
    backup)
        backup_data
        launch
        restart_needed=0
        ;;
    upgrade)
        require_running
        [[ $(env_value FITBAUS_RELEASE_VERSION "$ENV_FILE") != "$2" ]] || die 'Already on that release.'
        [[ -t 0 ]] || die 'Upgrade requires an interactive terminal.'
        printf '%s\n' 'Upgrade creates a private cold backup. Running jobs require resume after restart.'
        read -r -p "Upgrade to $2? [y/N]: " yes
        [[ $yes == y || $yes == Y ]] || die 'Upgrade cancelled.'
        fetch_release "$2"
        awk -v image="$pinned_image" -v version="$2" '
            /^TRANSCRIBER_IMAGE=/ {print "TRANSCRIBER_IMAGE=" image; next}
            /^FITBAUS_RELEASE_VERSION=/ {print "FITBAUS_RELEASE_VERSION=" version; next}
            {print}
        ' "$ENV_FILE" > "$stage/.env"
        preflight "$stage/.env" "$stage/compose.release.yaml"
        backup_data
        restart_needed=0
        mv -- "$stage/.env" "$ENV_FILE"
        mv -- "$stage/compose.release.yaml" "$MANIFEST"
        if ! launch || ! compose exec -T transcriber python -m transcriber doctor; then
            die 'Upgrade did not become ready. Cold backup and new configuration retained. Inspect logs; do not downgrade against a potentially migrated volume.'
        fi
        printf '%s\n' 'Upgrade complete. Existing pairing and persistent data are retained.'
        ;;
esac
