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

Example for a separately deployed Ollama instance reachable from the transcriber container on a trusted private network:

```dotenv
TRANSCRIBER_SUMMARY_BASE_URL=http://ollama:11434/v1
TRANSCRIBER_SUMMARY_MODEL=qwen3:14b
TRANSCRIBER_SUMMARY_ALLOW_HTTP=true
```

The hostname must resolve from inside the container, and the chosen model must already exist on the provider. No Ollama container or LLM weights are installed by FitBaus. For a cloud provider use HTTPS and its API key; configuring a remote provider is an explicit operator choice to send future audiobook excerpts outside the transcriber. Use HTTP only on an isolated trusted network; it does not encrypt text or keys. Prefer HTTPS even on a LAN. The guided installer does not prompt for these optional settings.

The authenticated GET /v1/capabilities response adds an optional summary_provider object: {"configured": false} or {"configured": true, "protocol": "openai-chat/v1", "model": "model-id"}. Configured means syntactically configured, not connected, tested, or ready. No endpoint URL, secret, or provider response is exposed. Existing clients ignore the object; existing servers without the field remain compatible.

The internal adapter sends a nonstreaming POST to /v1/chat/completions with system and user messages, a 1024-token output request, and only the provider bearer if configured. It disables redirects and environment proxies, verifies normal HTTPS certificates, bounds input (64 KiB UTF-8), response (128 KiB), and returned text (8000 characters), and rejects truncated or tool-call completions. Network errors are sanitized. Timeout applies to socket operations with an elapsed-time check during response reads; it is not a strict process deadline. No retries, tools, model listing or provider probes are performed.

Phase 3 introduces the transport only. No public completion/summary route, background summarization, transcript forwarding, caching or inference invocation is wired. The next phase must enforce Audiobookshelf item authorization and revision, collect complete verified section text, constrain prompts, isolate summary work from transcription, bound concurrency and deadlines, and establish cache identity including provider and model. Other OpenAI-compatible implementations (including LOBO if its endpoint satisfies this contract) require interoperability qualification.
