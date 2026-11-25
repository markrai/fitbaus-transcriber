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
