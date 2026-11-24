import json
import io
from pathlib import Path
import sys
import time
import wave

from transcriber.engine import EngineConfig, WhisperCpuEngine, decode_pcm
from transcriber.jobs import JobStore
from transcriber.qualification import qualify


def main():
    path = Path(sys.argv[1])
    with wave.open(str(path), "rb") as audio:
        duration_ms = audio.getnframes() * 1000 // audio.getframerate()
    with path.open("rb") as audio:
        pcm = decode_pcm(audio, 0, duration_ms)
    start = time.monotonic()
    engine = WhisperCpuEngine(Path("/data"), EngineConfig(cpu_threads=2, language="en"))
    cues = engine.transcribe(pcm, 0, duration_ms)
    elapsed = time.monotonic() - start
    assert cues, "Expected narration from actual CPU inference"
    text = " ".join(cue.text for cue in cues).lower()
    assert any(word in text for word in ("morning", "sun", "shining", "audio", "test")), text
    assert all(0 <= cue.start_ms < cue.end_ms <= duration_ms for cue in cues)
    assert all(a.end_ms <= b.start_ms for a, b in zip(cues, cues[1:]))
    silence = engine.transcribe(bytes(16000 * 2 * 4), 0, 4000)
    assert not silence, "Silence must not generate narration"
    del engine
    sample = io.BytesIO()
    with wave.open(sample, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm)
    with JobStore(Path("/data") / "queue").worker() as lock_fd:
        result = qualify(Path("/data"), "tiny", EngineConfig(language="en"), sample.getvalue(), lock_fd, probe=True)
    assert result["state"] == "unqualified", result
    assert len(result["runs"]) == 1 and result["runs"][0]["word_count"] > 0, result
    print(json.dumps({"benchmark_probe": result}))
    print(json.dumps({"audio_ms": duration_ms, "elapsed_seconds": round(elapsed, 2), "cue_count": len(cues), "text": text}))


if __name__ == "__main__":
    main()
