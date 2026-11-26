# Summary provider foundation (Phase 3)

FitBaus Transcriber owns configuration and outbound LLM transport. FitBaus Trainer owns playback position, sections and future presentation; it discovers the provider using the existing authenticated capabilities API. No LLM credentials belong on the bike.

Summarization is disabled by default. To opt in, configure these variables on the transcriber container (for Compose source or release deployments, edit the private .env file; for NAS deployments, edit the service environment and recreate the container):

| Variable | Meaning |
| --- | --- |
| TRANSCRIBER_SUMMARY_BASE_URL | OpenAI-compatible base URL ending in /v1 |
| TRANSCRIBER_SUMMARY_MODEL | Exact model identifier configured at the provider |
| TRANSCRIBER_SUMMARY_API_KEY | Optional provider bearer credential; keep private |
| TRANSCRIBER_SUMMARY_ALLOW_HTTP | false by default; true explicitly permits unencrypted HTTP |
| TRANSCRIBER_SUMMARY_TIMEOUT | 5–120 seconds; default 60 |
| TRANSCRIBER_SUMMARY_CACHE_EPOCH | Cache namespace; default 1. Change after replacing model weights under the same name |
| TRANSCRIBER_SUMMARY_CA_FILE | Optional absolute container path to a trusted PEM CA bundle for the HTTPS summary provider only |

Example for a separately deployed Ollama instance reachable from the transcriber container on a trusted private network:

```dotenv
TRANSCRIBER_SUMMARY_BASE_URL=http://ollama:11434/v1
TRANSCRIBER_SUMMARY_MODEL=qwen3:14b
TRANSCRIBER_SUMMARY_ALLOW_HTTP=true
```

The hostname must resolve from inside the container, and the chosen model must already exist on the provider. No Ollama container or LLM weights are installed by FitBaus. For a cloud provider use HTTPS and its API key; configuring a remote provider is an explicit operator choice to send requested audiobook excerpts outside the transcriber. Use HTTP only on an isolated trusted network; it does not encrypt text or keys. Prefer HTTPS even on a LAN. The guided installer does not prompt for these optional settings.

The authenticated GET /v1/capabilities response adds an optional summary_provider object: {"configured": false} or {"configured": true, "protocol": "openai-chat/v1", "model": "model-id"}. Configured means syntactically configured, not connected, tested, or ready. No endpoint URL, secret, or provider response is exposed. Existing clients ignore the object; existing servers without the field remain compatible.

The internal adapter sends a nonstreaming POST to /v1/chat/completions with system and user messages, a 1024-token output request, and only the provider bearer if configured. It disables redirects and environment proxies, verifies normal HTTPS certificates, bounds input (64 KiB UTF-8), response (128 KiB), and returned text (8000 characters), and rejects truncated or tool-call completions. Network errors are sanitized. Timeout applies to socket operations with an elapsed-time check during response reads; it is not a strict process deadline. No retries, tools, model listing or provider probes are performed.

Phase 4A exposes the authenticated [summary generation API](SUMMARY-API.md). Requests only send validated, fully contained transcript cues to the configured provider. Phase 4B adds persistent caching. There is no automatic summarization or Trainer summary control yet. Other OpenAI-compatible implementations (including LOBO if its endpoint satisfies this contract) require interoperability qualification.


## Private CA and LOBO

Use the same OpenAI-compatible adapter for LOBO. Verify the installation CA against the AUTH code on the phone (or obtain it from an already trusted PACK installation) before trusting it. Never use an unverified certificate retrieved from the network. Store the public CA certificate on the host, readable by container UID 10001. Add a read-only bind mount through a Compose override, for example:

```yaml
services:
  transcriber:
    volumes:
      - ./certs/lobo-ca.pem:/certs/lobo-ca.pem:ro
```

Set the following in the private .env and recreate the container with the override (use the appropriate base Compose file for the deployment):

```dotenv
TRANSCRIBER_SUMMARY_BASE_URL=https://192.168.1.200:7869/v1
TRANSCRIBER_SUMMARY_MODEL=lobo-nano
TRANSCRIBER_SUMMARY_ALLOW_HTTP=false
TRANSCRIBER_SUMMARY_CA_FILE=/certs/lobo-ca.pem
TRANSCRIBER_SUMMARY_TIMEOUT=120
```

Normal hostname/IP verification remains enabled. The custom CA supplements system trust for this adapter only, and invalid or missing files fail configuration validation. Other services' TLS trust is unchanged. LOBO addresses are examples; each installation has a distinct CA. A legacy LOBO chat endpoint may lack client authentication: keep it on a trusted private network.

## Provider management foundation

The authenticated capabilities API advertises summary_provider_admin_probe_api. Three initial administration routes require the operator bearer AND a loopback connection (e.g. inside the container). Ordinary paired devices cannot call them.

- GET /v1/admin/summary-provider: configured URL, model, HTTP opt-in, and booleans indicating a key and private CA; never returns the credential or certificate.
- POST /v1/admin/summary-provider/models: explicit bounded model discovery at the configured endpoint.
- POST /v1/admin/summary-provider/test: explicit synthetic inference through the same adapter; returns success, model and elapsed milliseconds, not generated text.

Probes share the summary concurrency slot and return sanitized errors. Neither probe accepts a caller-supplied URL, sends audiobook content, or changes configuration. A later administrator-authorized management flow and Trainer selector will build on these contracts. No remote configuration or hot switching is enabled in this phase.


## Managed provider settings (Phase 2)

The capability summary_provider_management_api advertises the provider selection backend. Configuration remains environment-based until an operator explicitly saves or disables a managed provider. The managed selection then takes precedence over environment settings, including after a restart. It is stored atomically at /data/summary-provider.json with mode 0600; protect backups and the data volume because this file contains provider credentials. A malformed or unreadable saved selection disables summaries rather than silently reverting to an alternate egress destination. Transcription and playback remain available; save a new provider to recover.

Issue a one-use administration code on the host:

```bash
docker compose exec transcriber python -m transcriber summary-admin
```

The operator bearer must be used from loopback to issue the code. A normally paired bike redeems it; the resulting separate grant expires after ten minutes, is bound to that device, is invalid after successful save/disable/cancel, and is revoked by issuing another code or revoking the paired device. There are five attempts per code. Neither the operator bearer nor the saved provider API key is returned to the bike. Only use this flow on a trusted network or through authenticated HTTPS transport: the historical bike-to-Transcriber API can use plain HTTP, and administration codes, grants and newly entered credentials could otherwise be intercepted.

API contract (all routes prefixed /v1/admin/summary-provider):

| Method and suffix | Body | Result |
| --- | --- | --- |
| POST /issue | none | Loopback operator only: code, expires_in |
| POST /redeem | code | Paired bearer: token, expires_in |
| GET /session | none | Current redacted settings and storage_error |
| POST /discover | kind, base_url, allow_http; optional api_key, timeout, lobo_auth | Candidate description and model identifiers |
| POST /discover | use_current: true | Discover at the active endpoint using its stored credential and trust |
| POST /test-candidate | model | Synthetic inference: ok, model, elapsed_ms |
| POST /save | confirm_egress: true | Persist and activate the tested candidate; consume grant |
| POST /disable | confirm: true | Persist disabled state; consume grant |
| POST /cancel | empty object | Discard grant and candidate |

Except issue/redeem, supply the existing paired bearer and X-FitBaus-Summary-Admin with the redeemed grant. The candidate is held in server memory only; every discovery or test invalidates the previous successful test. Saving requires a successful test in the same session and explicit consent to send requested narration to the selected endpoint. A failed test cannot change the active provider. An empty model listing permits manual model entry; otherwise choose an advertised model. New API keys require HTTPS. For custom private CAs configured in the environment, use_current retains the existing trust; arbitrary certificate paths are not accepted remotely.

For LOBO, use kind lobo, an HTTPS private IPv4 address on port 7869 ending /v1, allow_http false, and the AUTH code shown on the phone. The server captures the two-certificate chain without sending HTTP, verifies the installation CA's SHA-256-derived AUTH code, then uses only that CA with normal IP/hostname verification for discovery and inference. The pinned CA is persisted with the selected provider. LOBO itself must remain on a trusted network; AUTH verifies device identity, not caller authorization.

Probes use synthetic text only, share the summary slot, and never send an Audiobookshelf credential. Saving and disabling also share that slot so a generation request cannot see mismatched provider and cache settings. A new selection gets a new cache epoch. In-flight summary requests finish before a switch can succeed; clients should retry a busy response and reopen the audiobook to refresh capability/model state. No automatic summarization occurs. The Trainer selector is a separate implementation slice.


## Remembered LOBO devices

Summary provider settings version 2 stores a bounded registry of up to 16 exact private IPv4 HTTPS LOBO endpoints and their validated installation CA certificates, alongside the active configuration in the same atomic mode-0600 settings file. Existing version 1 active pinned LOBO configurations are recognized and migrated on the next save or disable. Settings without remembered pins retain version 1. Back up summary-provider.json before updating; older versions do not understand version 2. No AUTH code, phone private key, or additional bearer is stored in the registry.

The administrator-authorized session response includes trusted_lobo_endpoints (endpoint URLs only), and capabilities advertises summary_lobo_trust_registry. With a remembered endpoint, POST /discover accepts kind lobo, base_url and allow_http false without lobo_auth. The adapter still verifies HTTPS against the stored CA and IP; a changed certificate fails the connection. A different address requires verification, even if it is the same physical phone. Providing lobo_auth explicitly verifies a replacement CA; a new or replaced pin is persisted only after successful model discovery, inference test and consented Save. Disabling summaries or switching to another provider preserves remembered pins. No silent eviction.

An administrator can remove an inactive endpoint using POST /forget-lobo with {"base_url":"https://192.168.1.200:7869/v1","confirm":true}, the paired bearer and current admin grant. Switch away from or disable an active pinned endpoint first. Removing a pin invalidates any in-progress candidate and returns only public endpoint metadata. Registry corruption fails closed; explicit reconfiguration can restore settings but previously remembered pins may need verification again. No unauthenticated trust listing or HTTP downgrade is provided.
