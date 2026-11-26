from dataclasses import dataclass
import math
import subprocess
import threading

from .audiobookshelf import SourceError
from .contract import Cue, MAX_CUE_CHARS, MAX_CUES, MAX_WINDOW_MS, TranscriptWindow
from .diagnostics import exception_type, safe_exit_status, log_timestamp_report
from .model import model_spec, model_directory, model_present

SAMPLE_RATE = 16_000
BYTES_PER_MS = 32
MAX_DECODE_BYTES = MAX_WINDOW_MS * BYTES_PER_MS
TIMESTAMP_TOLERANCE_MS = 1000
ENGINE_ID = model_spec().engine_id
FORMATS = "aac,mp3,mov,mp4,m4a,3gp,3g2,mj2,flac,wav,ogg,matroska,webm,asf,aiff"


class EngineError(Exception):
    def __init__(self, code, *, exception_type=None, exit_status=None):
        super().__init__(code)
        self.exception_type = exception_type
        self.exit_status = safe_exit_status(exit_status)


@dataclass(frozen=True)
class EngineConfig:
    cpu_threads: int = 2
    language: str | None = None
    decode_timeout: int = 120
    inference_timeout: int = 120

    def __post_init__(self):
        if type(self.cpu_threads) is not int or not 1 <= self.cpu_threads <= 8:
            raise ValueError("CPU threads must be between 1 and 8")
        if self.language is not None and (
            len(self.language) != 2 or not self.language.isascii() or not self.language.isalpha()
            or self.language != self.language.lower()
        ):
            raise ValueError("Language must be a lowercase two-letter code or auto")
        if type(self.decode_timeout) is not int or not 10 <= self.decode_timeout <= 600:
            raise ValueError("Invalid audio decoding timeout")
        if type(self.inference_timeout) is not int or not 10 <= self.inference_timeout <= 600:
            raise ValueError("Invalid inference worker timeout")


def decode_pcm(file, start_ms, end_ms, timeout=120):
    if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms:
        raise ValueError("Invalid decode range")
    duration = end_ms - start_ms
    if duration > MAX_WINDOW_MS:
        raise ValueError("Decode window exceeds limit")
    fd = file.fileno()
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-protocol_whitelist", "file,pipe", "-format_whitelist", FORMATS,
        "-threads", "1", "-ss", f"{start_ms / 1000:.3f}",
        "-i", f"/proc/self/fd/{fd}", "-t", f"{duration / 1000:.3f}",
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le", "-f", "s16le", "-fs", str(MAX_DECODE_BYTES + 4096), "pipe:1",
    ]
    try:
        result = subprocess.run(command, pass_fds=(fd,), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=True)
    except subprocess.TimeoutExpired as error:
        raise EngineError("audio_decode_timeout", exception_type=exception_type(error)) from None
    except (OSError, subprocess.SubprocessError) as error:
        raise EngineError("audio_decode_failed", exception_type=exception_type(error),
                          exit_status=getattr(error, "returncode", None)) from None
    pcm = result.stdout
    expected = duration * BYTES_PER_MS
    if not pcm or len(pcm) % 2 or len(pcm) > expected + 4096:
        raise EngineError("invalid_decoded_audio")
    if len(pcm) < expected - min(expected // 10, 8000):
        raise EngineError("incomplete_decoded_audio")
    return pcm[:expected]


def _segment_ms(value, duration_ms):
    tolerance = TIMESTAMP_TOLERANCE_MS / 1000
    if (type(value) not in (int, float)
            or not -tolerance <= value <= duration_ms / 1000 + tolerance
            or not math.isfinite(value)):
        raise EngineError("invalid_model_timestamp")
    return math.floor(value * 1000 + 0.5)


def _timestamp_diagnostic(value):
    if type(value) not in (int, float):
        return "non_numeric"
    if type(value) is float and not math.isfinite(value):
        return "nan" if math.isnan(value) else ("positive_infinity" if value > 0 else "negative_infinity")
    if not -1_000_000_000 <= value <= 1_000_000_000:
        return "outside_log_range"
    return f"{value:.6f}"


def normalize_segments(segments, start_ms, end_ms, *, audio_duration_ms=None, report_timestamp=log_timestamp_report):
    requested_duration = end_ms - start_ms
    duration_ms = requested_duration if audio_duration_ms is None else audio_duration_ms
    if (type(start_ms) is not int or type(end_ms) is not int or start_ms < 0
            or not 0 < requested_duration <= MAX_WINDOW_MS
            or type(duration_ms) is not int or not 0 <= duration_ms <= requested_duration):
        raise ValueError("Invalid normalization range")
    audio_end = start_ms + duration_ms
    cues = []
    previous_end = start_ms
    invalid = 0
    discarded = 0
    clamped = 0
    first_invalid = None
    for index, segment in enumerate(segments):
        if index >= MAX_CUES:
            raise EngineError("model_cue_limit")
        text = " ".join(segment.text.split())
        if not text:
            continue
        try:
            relative_start = _segment_ms(segment.start, duration_ms)
            relative_end = _segment_ms(segment.end, duration_ms)
            if segment.start > segment.end:
                raise EngineError("invalid_model_timestamp")
        except EngineError:
            invalid += 1
            discarded += 1
            if first_invalid is None:
                first_invalid = (index, _timestamp_diagnostic(segment.start), _timestamp_diagnostic(segment.end))
            continue
        if relative_start < 0 or relative_end > duration_ms:
            clamped += 1
        start = max(previous_end, start_ms + relative_start)
        end = min(audio_end, start_ms + relative_end)
        if start >= end:
            discarded += 1
            continue
        if len(text) > MAX_CUE_CHARS:
            raise EngineError("model_text_limit")
        cues.append(Cue(start, end, text))
        previous_end = end
    if invalid or clamped:
        sample = first_invalid if first_invalid is not None else ("none", "none", "none")
        report_timestamp({
            "start_ms": start_ms, "end_ms": end_ms, "audio_duration_ms": duration_ms,
            "accepted": len(cues), "discarded": discarded, "invalid": invalid, "clamped": clamped,
            "first_invalid_index": sample[0], "first_invalid_start_s": sample[1],
            "first_invalid_end_s": sample[2],
        })
    if invalid and not cues:
        raise EngineError("invalid_model_timestamp")
    return tuple(cues)


class WhisperCpuEngine:
    def __init__(self, data_dir, config=EngineConfig(), *, model_name="tiny", report_timestamp=log_timestamp_report):
        self.model_name = model_name
        self.path = model_directory(data_dir, model_name)
        self.config = config
        self.report_timestamp = report_timestamp
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            if not model_present(self.path):
                raise EngineError("model_not_prepared")
            try:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(
                    str(self.path), device="cpu", compute_type="int8",
                    cpu_threads=self.config.cpu_threads, num_workers=1, local_files_only=True,
                )
            except Exception as error:
                raise EngineError("model_load_failed", exception_type=exception_type(error)) from None
        return self._model

    def transcribe(self, pcm, start_ms, end_ms):
        if not pcm or len(pcm) % 2 or len(pcm) > MAX_DECODE_BYTES or not 0 < end_ms - start_ms <= MAX_WINDOW_MS:
            raise ValueError("Invalid PCM input")
        if len(pcm) > (end_ms - start_ms) * BYTES_PER_MS:
            raise ValueError("PCM exceeds the requested interval")
        with self._lock:
            model = self._load()
            try:
                import numpy as np
                audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                segments, _ = model.transcribe(
                    audio, language=self.config.language, task="transcribe",
                    beam_size=1, best_of=1, temperature=0.0,
                    condition_on_previous_text=False, vad_filter=True,
                )
                return normalize_segments(segments, start_ms, end_ms, audio_duration_ms=len(pcm) // BYTES_PER_MS,
                                          report_timestamp=self.report_timestamp)
            except EngineError:
                raise
            except Exception as error:
                raise EngineError("inference_failed", exception_type=exception_type(error)) from None


def transcribe_window(source, identity, token, directory, engine, start_ms, end_ms, decoder=decode_pcm):
    if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms or end_ms - start_ms > MAX_WINDOW_MS:
        raise ValueError("Invalid transcript window")
    manifest = source.manifest(identity.item_id, token)
    if manifest.identity != identity:
        raise SourceError("stale_revision")
    if end_ms > manifest.duration_ms:
        raise ValueError("Transcript window exceeds audiobook")
    cues = []
    previous_end = start_ms
    for track in manifest.tracks:
        left = max(start_ms, track.offset_ms, previous_end)
        right = min(end_ms, track.offset_ms + track.duration_ms)
        if right <= left:
            continue
        with source.materialize_track(identity, track.file_id, token, directory) as audio:
            pcm = decoder(audio, left - track.offset_ms, right - track.offset_ms, engine.config.decode_timeout)
        cues.extend(engine.transcribe(pcm, left, right))
        if len(cues) > MAX_CUES:
            raise EngineError("model_cue_limit")
        previous_end = right
    if source.manifest(identity.item_id, token).identity != identity:
        raise SourceError("stale_revision")
    return TranscriptWindow(identity, start_ms, end_ms, tuple(cues))
