import logging
import time

ENGINE_FAILURES = frozenset((
    "engine_failed", "audio_decode_failed", "audio_decode_timeout", "invalid_decoded_audio",
    "incomplete_decoded_audio", "invalid_model_timestamp", "model_cue_limit", "model_text_limit",
    "model_not_prepared", "model_load_failed", "inference_failed", "invalid_transcript",
    "inference_timeout", "worker_crashed", "worker_unavailable", "invalid_worker_result",
    "resource_unavailable", "cancelled",
))
EXCEPTION_TYPES = frozenset((
    "ValueError", "TypeError", "RuntimeError", "MemoryError", "OSError", "FileNotFoundError",
    "PermissionError", "ImportError", "ModuleNotFoundError", "TimeoutError", "TimeoutExpired",
    "CalledProcessError", "OverflowError", "IndexError", "KeyError", "AssertionError",
    "FloatingPointError", "OtherException",
))


def exception_type(error):
    name = type(error).__name__
    return name if name in EXCEPTION_TYPES else "OtherException"


def safe_exit_status(value):
    return value if type(value) is int and -255 <= value <= 255 else None


def stage(code):
    if code.startswith("audio_decode") or code.endswith("decoded_audio"):
        return "decode"
    if code in ("model_not_prepared", "model_load_failed"):
        return "model_load"
    if code in ("invalid_model_timestamp", "model_cue_limit", "model_text_limit", "invalid_transcript"):
        return "normalize"
    if code in ("inference_failed", "engine_failed"):
        return "inference"
    if code == "source_failed":
        return "source"
    if code == "resource_unavailable":
        return "admission"
    return "worker"


def log_failure(kind, model, start_ms, end_ms, code, error, started):
    safe_code = code if code in ENGINE_FAILURES or code in (
        "source_failed", "result_limit", "internal_error", "background_unavailable",
        "background_storage_full", "background_storage_error", "stale_revision",
        "incomplete_audio", "transfer_timeout", "size_limit", "upstream_unavailable",
        "invalid_manifest", "invalid_response", "not_found",
    ) else "other_failure"
    detail = getattr(error, "exception_type", None)
    if detail not in EXCEPTION_TYPES:
        detail = exception_type(error) if not hasattr(error, "exception_type") else None
    logging.getLogger("transcriber.diagnostics").warning(
        "transcription_failure kind=%s model=%s start_ms=%d end_ms=%d stage=%s code=%s "
        "exception_type=%s exit_status=%s elapsed_ms=%d",
        kind if kind in ("live", "background") else "unknown",
        model if model in ("tiny", "base", "small") else "unknown",
        start_ms, end_ms, stage(safe_code), safe_code, detail or "none",
        safe_exit_status(getattr(error, "exit_status", None)),
        max(0, int((time.monotonic() - started) * 1000)),
    )
