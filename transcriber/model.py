from pathlib import Path
import tempfile

MODEL_REPO = "Systran/faster-whisper-tiny"
MODEL_REVISION = "d90ca5fe260221311c53c58e660288d3deb8d356"
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def model_directory(data_dir):
    return Path(data_dir) / "models" / ("tiny-" + MODEL_REVISION)


def model_present(path):
    try:
        return all((path / name).is_file() and (path / name).stat().st_size > 0 for name in MODEL_FILES)
    except OSError:
        return False


def prepare_model(data_dir):
    from huggingface_hub import snapshot_download

    destination = model_directory(data_dir)
    if model_present(destination):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError("Incomplete model directory; remove it before retrying")
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        candidate = Path(staging) / "model"
        snapshot_download(
            repo_id=MODEL_REPO,
            revision=MODEL_REVISION,
            local_dir=candidate,
            allow_patterns=list(MODEL_FILES),
            token=False,
            max_workers=2,
        )
        if not model_present(candidate):
            raise RuntimeError("Model download incomplete")
        try:
            candidate.rename(destination)
        except OSError:
            if not model_present(destination):
                raise
    return destination
