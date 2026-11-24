# Portainer, Synology and UGREEN deployment

FitBaus Transcriber is an optional companion to Audiobookshelf. The supported release image is Linux AMD64 (x86-64) only. Use an x86-64 NAS with a supported Docker/Compose installation, or run the Transcriber on a separate x86-64 computer and leave Audiobookshelf on the NAS. ARM-based NAS devices are not supported by this release. Do not use emulation for production inference.

Choose a published release whose container publication has succeeded. Download the Compose files from that release, not the development branch. The first public release and actual NAS hardware qualification are separate release gates.

## Configuration

These values are common to all three platforms.

| Setting | Value |
| --- | --- |
| Image | `ghcr.io/markrai/fitbaus-transcriber:VERSION` (replace VERSION with an actual published tag; a published digest is preferable) |
| Operator token | Generate a unique random 64-character alphanumeric token using a trusted local password manager; keep it private |
| Audiobookshelf URL | The exact URL configured on the bike, e.g. `http://192.168.1.20:13378` |
| Source ID | `home-library` for a new installation; keep unchanged for this library |
| Bind IP | The Docker host's private IPv4 address, or loopback for an existing HTTPS reverse proxy |
| Published port | 8765 by default; change the host port only if occupied |
| Resources | 2 CPU threads, 2 CPUs, 2 GiB memory by default |
| Storage | Local Docker named volume `transcriber-data`, mounted at `/data` |

Do not enter an Audiobookshelf password or API key in Compose. Those are only used when the account is accessed. The operator token is not the Audiobookshelf token or bike pairing code. The NAS administrator can inspect container environment variables; restrict the NAS management interface to trusted administrators.

The Transcriber must reach Audiobookshelf from inside its container. Use the actual reachable address, not localhost. A Docker-internal service hostname may not match the saved bike URL. HTTPS Audiobookshelf requires an HTTPS Transcriber endpoint on the bike. For direct private HTTP use an RFC1918 IPv4 address on the trusted LAN. Do not expose the API port, NAS management interface or container console to the public internet.

The default loopback bind intentionally does not permit direct bike connections. Set the private LAN address explicitly, or configure an HTTPS reverse proxy separately. Do not weaken the container's read-only root, non-root user, capability restrictions, memory/process limits or health check to make an older NAS manager accept the stack. If its Compose engine cannot honor these fields, update the manager or use another host.

Allow several GiB of free storage for the image, databases, optional models, temporary track materialization and backups. The included Tiny model is available immediately; Base and Small are optional and qualified against available resources. A 2 GiB container limit is not a promise that every NAS will have adequate free RAM or CPU performance.

## Portainer (Docker Standalone)

Use the existing [compose.release.yaml](../compose.release.yaml). This is the same stack used by the guided installer, without a separate Portainer-specific implementation.

1. Select the Docker Standalone environment in Portainer. Open Stacks and add a stack named `fitbaus-transcriber` (or another unique name you will keep).
2. Paste the release Compose file into the web editor, or upload it. In the environment-variable section set the following entries individually or load a private local environment file:

   ```dotenv
   TRANSCRIBER_IMAGE=ghcr.io/markrai/fitbaus-transcriber:VERSION
   TRANSCRIBER_TOKEN=REPLACE_WITH_RANDOM_TOKEN
   AUDIOBOOKSHELF_URL=http://192.168.1.20:13378
   AUDIOBOOKSHELF_SOURCE_ID=home-library
   TRANSCRIBER_LAN_BIND=192.168.1.30
   TRANSCRIBER_HOST_PORT=8765
   ```

   Replace the tag, token and example addresses before deploying. Do not upload this environment file to Git or share it. Leave optional resource values at their defaults initially.
3. Deploy the stack and check that its container is running and healthy. Open its Console, choose `/bin/sh`, connect as the image's normal user (UID 10001 if a user must be supplied), and run the commands in the Pairing section below.

For the interface, see [Portainer: Add a new stack](https://docs.portainer.io/user/docker/stacks/add) and [Inspect or edit a stack](https://docs.portainer.io/user/docker/stacks/edit). This deployment targets Docker Standalone, not Swarm or Kubernetes. Leave GitOps automatic redeployments and update webhooks disabled.

## Synology Container Manager

Use [compose.nas.yaml](../compose.nas.yaml) from the release. It contains literal values so the project does not depend on GUI support for .env substitution.

1. In DSM, install/open Container Manager on a supported x86-64 NAS. Create a private project folder on a local volume.
2. Open Project and create a new project. Choose its name and the project folder. Paste or upload the NAS Compose file. Replace the image tag, operator token and Audiobookshelf URL placeholders, and replace the loopback IP in the quoted port mapping with the NAS private IPv4 address for direct LAN access. Keep the source ID and volume name stable. Do not enable a public web portal.
3. Deploy the project. Inspect the container state and logs. From the container details open its terminal and create a `/bin/sh` session (normal image user). Run the Pairing commands below.

See [Synology Container Manager: Project](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project) and [Container](https://kb.synology.com/en-my/DSM/help/ContainerManager/docker_container?version=7). Menu names vary with DSM and Container Manager versions. Availability of Container Manager does not establish sufficient resources or CPU compatibility.

## UGREEN UGOS Pro

Use the same [compose.nas.yaml](../compose.nas.yaml) from the release.

1. Install/open Docker from the NAS App Center on a supported x86-64 model.
2. Open Docker, then Project, and create a named project in a private location. Paste the NAS Compose file and replace the image tag, operator token, source URL and bind IP as above. Keep the source ID and volume name unchanged.
3. Deploy; verify the container is running. Open the container's Terminal and start a `/bin/sh` session (normal image user). Run the Pairing commands below.

See [UGREEN Docker Compose Project User Guide](https://support.ugnas.com/knowledgecenter/detail/article/en-US/411) and [Container management](https://support.ugnas.com/detail/article/en-US/289). Firmware and app versions may differ. Do not enable UGREENlink remote access for this API.

## Pairing and verification (no host SSH needed)

In the running container's GUI terminal, run:

```sh
python -m transcriber doctor
python -m transcriber pair
```

The first command checks local readiness and reports model qualification; it does not prove that Audiobookshelf is reachable. The second prints a single-use code valid for ten minutes. On the bike open Settings → Transcriber, enter the published host and port (or your existing HTTPS proxy address) and the code. The operator token must stay on the server. Do not publish a screenshot or log containing credentials or an unredeemed code.

Optionally, from a trusted interactive terminal:

```sh
python -m transcriber check-source
```

Select username/password or API key. This checks accessible book libraries; the credentials are not stored. If hidden credential input is unavailable in the NAS terminal, skip this check rather than typing secrets into an echoed field. Test actual access using Read Along on the bike. Run `python -m transcriber revoke-pairings` only when you intentionally want to disconnect all paired devices. That also interrupts their background authorizations.

## Storage, backups and updates

The default project-scoped Docker volume is created by Compose and is separate from the container filesystem. It stores the queue, prepared transcripts, optional models, qualification records and paired-device database. Normal restart or recreation with the same project/volume preserves these. Do not delete the volume, rename the project, select removal of project data, or prune unused volumes. Never run two Transcriber services against one data volume.

Before upgrading, stop the Transcriber and back up the exact named volume and private stack configuration, including the old image reference. The NAS's ordinary shared-folder backup may not include Docker-managed named volumes: confirm that it really captures this volume. Do not rely on an exported container image or container configuration as a data backup. A supported offline volume archive procedure is available below. Backups include private narration and credential hashes; protect them.

If a NAS backup tool only supports normal folders, an advanced fresh-install alternative is to replace the service volume mapping with an absolute local host directory mapped to /data. Create that directory first and grant UID/GID 10001 read/write access through supported NAS permissions, then verify the container starts without changing its user. Keep the directory private and on a local filesystem with SQLite locking support. Do not use SMB/NFS mounts for the databases, and do not switch an existing named volume to an empty bind mount (that would appear to lose all data). Bind-mount permission handling is platform-specific and not physically qualified in this phase.

For an offline named-volume backup, an administrator with Docker host access can use the following commands from a private backup directory, after stopping the project through its GUI. Replace the example volume and image reference with the actual values from the container's mount details and current stack configuration. Confirm no other container is using the volume; the inspect command must succeed before the archive command runs.

```sh
umask 077
VOLUME=fitbaus-transcriber_transcriber-data
IMAGE=ghcr.io/markrai/fitbaus-transcriber:VERSION
docker volume inspect "$VOLUME" >/dev/null &&
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=volume,source=$VOLUME,target=/data,readonly" \
  --entrypoint tar "$IMAGE" -C /data -cf - . > data.tar.partial &&
tar -tf data.tar.partial >/dev/null &&
mv data.tar.partial data.tar
```

Save the matching stack configuration and credentials separately in the protected backup directory. Restart the project when the backup is complete. Verify restoration to a separate empty volume before relying on a backup. See [installation recovery](INSTALLATION.md#upgrades-and-recovery). The GUI deployment workflow does not automate backup restoration.

To update, select a newer published image and review its release notes and Compose changes. Edit the existing stack/project (do not delete and recreate it under a new name), preserve credentials, source ID, limits, ports and data mount, and redeploy. Check readiness and existing bike pairing after recreation. Background grants are memory-only; paused/interrupted work requires explicit resume and sign-in when needed. Do not automatically downgrade a potentially migrated database. If the update fails, retain the backup and inspect logs.

## Qualification boundary

CI resolves the literal NAS template against the canonical release stack and runs the image through readiness, pairing and persistent-volume recreation. The same Compose definition is used for Portainer Standalone. Physical Portainer, DSM and UGOS UI testing, public image pulling, actual host memory behavior and full backup restoration remain release-qualification tasks. No particular NAS model or firmware is certified by this phase.
