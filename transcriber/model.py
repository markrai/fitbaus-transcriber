from dataclasses import dataclass
from pathlib import Path
import tempfile
from types import MappingProxyType

MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
DEFAULT_MODEL = "tiny"
BUNDLED_ROOT = Path("/opt/fitbaus-models")


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo: str
    revision: str
    bundled: bool
    approximate_bytes: int
    usage: str

    @property
    def directory_name(self):
        return self.name + "-" + self.revision

    @property
    def engine_id(self):
        return "faster-whisper-1.2.1-" + self.name + "-int8-" + self.revision


MODELS = MappingProxyType({
    "tiny": ModelSpec("tiny", "Systran/faster-whisper-tiny",
                      "d90ca5fe260221311c53c58e660288d3deb8d356", True, 78_200_000, "live"),
    "base": ModelSpec("base", "Systran/faster-whisper-base",
                      "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66", False, 148_000_000, "live_candidate"),
    "small": ModelSpec("small", "Systran/faster-whisper-small",
                       "536b0662742c02347bc0e980a01041f333bce120", False, 487_000_000, "background_recommended"),
})
MODEL_REPO = MODELS[DEFAULT_MODEL].repo
MODEL_REVISION = MODELS[DEFAULT_MODEL].revision


def model_spec(name=DEFAULT_MODEL):
    if not isinstance(name, str) or name not in MODELS:
        raise ValueError("Unsupported transcription model")
    return MODELS[name]


def model_directory(data_dir, name=DEFAULT_MODEL, *, bundled_root=BUNDLED_ROOT):
    spec = model_spec(name)
    bundled = Path(bundled_root) / spec.directory_name
    if spec.bundled and model_present(bundled):
        return bundled
    return Path(data_dir) / "models" / spec.directory_name


def model_present(path):
    try:
        return all((path / name).is_file() and (path / name).stat().st_size > 0 for name in MODEL_FILES)
    except OSError:
        return False


def download_model(spec, destination):
    from huggingface_hub import snapshot_download

    destination = Path(destination)
    if model_present(destination):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError("Incomplete model directory; remove it before retrying")
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        candidate = Path(staging) / "model"
        snapshot_download(
            repo_id=spec.repo,
            revision=spec.revision,
            local_dir=candidate,
            allow_patterns=list(MODEL_FILES),
            token=False,
            max_workers=2,
        )
        if not model_present(candidate):
            raise RuntimeError("Model download incomplete")
        candidate_metadata = candidate / ".cache"
        if candidate_metadata.exists():
            import shutil
            shutil.rmtree(candidate_metadata)
        try:
            candidate.rename(destination)
        except OSError:
            if not model_present(destination):
                raise
    return destination


def prepare_model(data_dir):
    destination = model_directory(data_dir)
    if model_present(destination):
        return destination
    return download_model(model_spec(), destination)


def bundle_default(root):
    spec = model_spec()
    return download_model(spec, Path(root) / spec.directory_name)
