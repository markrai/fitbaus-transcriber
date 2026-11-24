# Guided installation and maintenance

## Requirements

- Linux AMD64 Docker host with Docker Compose v2 and access to the local daemon.
- Bash, curl, OpenSSL, flock, awk and tar.
- A published FitBaus Transcriber release with a publicly pullable GHCR image.
- Audiobookshelf reachable from the container, using the same base URL as the bike.
- Enough memory and storage for the configured container, model files and backups. The initial limits are two CPUs and 2 GiB RAM; optional models are automatically qualified.

Use a trusted private LAN. The installer does not open firewall rules or configure a reverse proxy. HTTPS is recommended. Do not expose private HTTP ports to the internet.

## Install

Choose an actual published release that contains the installer. The tag below is illustrative.

```sh
mkdir -p fitbaus-transcriber
cd fitbaus-transcriber
VERSION=v0.1.0
curl -fsSLo install.sh "https://raw.githubusercontent.com/markrai/fitbaus-transcriber/$VERSION/install.sh"
bash install.sh install "$VERSION"
```

Review the script before running it. Do not run it in an existing source or manually configured deployment directory. The wizard asks for the Audiobookshelf URL, the host's private IPv4 bind address and the published port (8765 by default). For a local reverse proxy choose 127.0.0.1 and configure HTTPS separately. The service must be able to reach Audiobookshelf at the entered URL; localhost inside a container is not the host.

The installer downloads the selected version and resolves its image digest. It uses a random operator credential, a unique persistent Compose project name and a private .env file. It does not execute the .env file as shell code. It checks the Compose configuration, validates the application's configuration, model and data directory, starts the container and reports readiness. The optional account verification prompts for Audiobookshelf username/password or API key, then checks accessible book libraries. Credentials are not stored. Readiness alone is not proof of source connectivity or account access.

Enter the displayed host, port and single-use pairing code in FitBaus Settings → Transcriber. No operator token should be entered on the bike. Tiny is bundled; optional models can be downloaded and qualified using the bike.

## Maintenance

| Command | Purpose |
| --- | --- |
| `bash install.sh status` | Container state, readiness and model qualification |
| `bash install.sh pair` | Generate a new single-use pairing code |
| `bash install.sh check-source` | Check actual Audiobookshelf account access |
| `bash install.sh logs` | Recent container logs |
| `bash install.sh stop` | Stop without deleting data |
| `bash install.sh start` | Validate configuration and start the pinned local image |
| `bash install.sh backup` | Stop, archive configuration and volume, restart |
| `bash install.sh upgrade VERSION` | Stage a newer release, back up, deploy and verify |

Keep the installation directory private. Keep the same project name and data volume. Do not delete volumes, use `down -v`, or run a second service against the same volume. The installer serializes its own maintenance commands; do not run parallel manual Compose operations. If an image has been pruned locally, pull the exact digest recorded in .env before starting. Backups require free space for a full uncompressed copy of the volume, including optional models and prepared transcripts. Keep copies on another disk if possible.

## Upgrades and recovery

Use a newer published release. Download and review a newer installer separately if the release instructions require it. The upgrade helper resolves the candidate image digest and checks its configuration before downtime. It then stops the existing container and saves the old .env, Compose manifest and full data volume under a private timestamped backups directory. It verifies that the tar archive can be listed before changing the active configuration. If backup fails, the old configuration is unchanged and the helper attempts to restart the original service. Incomplete archives have a .partial suffix and must not be used as backups.

A failed deployment after the configuration switch retains the new configuration and the previous cold backup. Inspect logs and address the failure. Do not simply downgrade the binary against a potentially migrated database. A recovery from the backup should restore the matching old configuration and data into a separate, empty volume using a different Compose project and unused host port, and verify it before switching traffic. This phase does not automate data restoration or guarantee downgrade compatibility. Copy the backup off the host and verify it before relying on it; listing a tar archive is not a restoration test.

Backups contain operator credentials, pairing credential hashes and private transcript text. Restrict access and use encrypted storage where appropriate. No automatic backup retention or deletion is performed. Stopping, backing up or upgrading clears memory-only background authorization. Committed windows remain on disk; resume unfinished jobs after signing in again.

## Existing source or manually configured deployments

The installer deliberately refuses to adopt existing configurations automatically. Continue using the current Compose project and deployment method. To migrate to a published image, back up the existing volume and configuration, retain the same project and volume, use the release Compose file and set the published image reference. Do not run the source-built and release services concurrently. The managed installer is intended for fresh installs; automatic adoption is not supported. For a fresh GUI-managed deployment, see [Portainer and NAS deployment](NAS-DEPLOYMENT.md).

## Troubleshooting

- Image unavailable: confirm the exact release exists, publishing succeeded and the package is publicly accessible.
- Container not ready: run status and logs; check the source URL, volume access and bundled model.
- Source check fails: verify URL, network and account access. Use the same source URL on the bike.
- Bike cannot connect: verify the host IP, published port, network isolation and firewall; generate a fresh pairing code when necessary.
- Optional model not ready: use Manage Models to inspect download and automatic hardware qualification status. Tiny remains available for live Read Along.
