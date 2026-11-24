# Optional model management

Tiny is bundled, default and the only executable model in this phase. Base and Small are the only optional models. All three use pinned revisions. Medium and Large-v3 are unsupported.

## Inventory

`GET /v1/models` requires the operator or a paired device bearer. The response contains `models` and `quota_mib`. The existing capabilities endpoint also includes model inventory, quota and `model_download_api: true`. Each model reports its ID, revision, bundled flag, installed flag, approximate bytes, intended usage, state, received bytes, total bytes (null until known), and a sanitized error code.

States: not_installed, downloading, verifying, installed, failed, cancelled, interrupted. Installed means the expected local files are present; managed downloads have been checked against pinned upstream metadata before installation. Manual modifications to the volume are outside this trust boundary.

## Administrative operations

These routes require both the operator bearer and a loopback peer. Paired tokens are read-only.

- POST /v1/models/base/download (or small)
- POST /v1/models/base/cancel (or small)

No arbitrary model ID, repository, revision, URL or destination is accepted. HTTP 202 means accepted or already downloading; HTTP 200 means no work was needed. An accepted cancellation may race with successful installation, so poll inventory to confirm the terminal state. HTTP 409 reports busy, quota, free-disk or other admission conflicts; HTTP 400 rejects unsupported models; HTTP 503 means the worker or storage is unavailable. There is no model activation or deletion endpoint.

A single download process holds an exclusive filesystem lock shared by all services using the data volume. It is supervised with a 30-minute deadline and bounded termination, with atomic staging and checksum verification. Optional-model storage defaults to 1024 MiB; the downloader checks actual upstream sizes, reserves 256 MiB of free disk and bounds streamed bytes. Tiny is excluded from the optional quota. Staging is discarded on cancellation/failure and recovered under the lock after restart. Explicitly retry to start a new download. Network transfer may compete with inference for host resources; no playback scheduling priority is provided yet.

No audiobook audio or Audiobookshelf credential is sent to the model host. Downloads use the public pinned model repository and no Hugging Face bearer. Error messages do not expose signed download URLs.

## Deferred

Admin permission for bike-initiated download, removal, inference model selection, hardware qualification, inference isolation and durable background audiobook jobs are separate phases.
