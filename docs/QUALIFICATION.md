# Hardware qualification

The local operator can benchmark Tiny, Base and Small after installation. The service continues to use Tiny exclusively. These measurements are hardware-specific candidates, not guarantees of accuracy, throughput or safety.

## Commands

Supply a representative 300-second, 16 kHz mono PCM16 WAV over standard input:

```sh
docker compose exec -T transcriber python -m transcriber benchmark --model all --wav-stdin < sample.wav
docker compose exec -T transcriber python -m transcriber qualification
```

Use `--model tiny`, `base` or `small` to test a single installed model. Missing models are reported without downloads. The operator must run this while playback transcription is idle: the benchmark holds the same exclusive job lock as uncached inference, and competing playback jobs may require resubmission. Existing cached results remain readable. A short `--probe` runs once without saving or qualifying a model; this is for smoke tests only.

Each qualification runs two cold subprocesses using the pinned model and the service's CPU thread, language and deadline settings. The report records model loading time, inference time, total worker wall time, peak process RSS, cue/word counts and covered cue duration, plus the input SHA-256 and timestamp. No sample or transcript is persisted. Results live under /data/qualification and are invalidated by changes in the recorded model identity, runtime package versions, CPU identity, affinity count, CPU quota, memory limit or inference settings. Qualification is not a word error rate assessment; speech checks only reject obviously unsuitable samples.

## Policy

Two full-length results with at least 100 words and 120 seconds of cue coverage per run are needed for classification. Worst worker time <=60 seconds is a live candidate; >60 and <300 seconds is a background candidate; >=300 seconds is slow. A deadline failure is reported as failure, not extrapolated. The default 120-second deadline remains in force. The local WAV test excludes upstream retrieval, FFmpeg and actual playback contention. Production deadlines include those costs.

The memory gate requires available memory >= max(model floor, 125% of measured peak RSS) + 128 MiB. Initial engineering floors are Tiny 768 MiB, Base 896 MiB and Small 1536 MiB, excluding the 128 MiB reserve. These are conservative starting estimates, not measured worst-case requirements. Availability is the smaller of host MemAvailable and visible cgroup limit minus working-set estimate (inactive file cache is treated as reclaimable). Linux cgroup v2 and common v1 mount layouts are supported; nested or unusual mounts and other processes can limit accuracy. CPU quota and affinity are reported and used in the fingerprint, not hard CPU admission limits. No memory reservation or independent worker cgroup is created. An OOM remains possible.

Benchmark admission fails when memory cannot be determined. For backward compatibility, existing Tiny production inference is rejected only when known headroom is insufficient. Cached jobs bypass admission. Failure is retryable within the existing attempt budget. Base and Small cannot yet be selected for production inference.

Authenticated GET /v1/qualification returns current resource estimates, per-model records and admission decisions. No network-triggered benchmark is exposed.
