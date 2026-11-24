from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from transcriber.engine import ENGINE_ID, EngineConfig
from transcriber.model import MODELS, MODEL_FILES, model_directory, model_present, model_spec, prepare_model
from transcriber.runner import engine_profile


class ModelTests(unittest.TestCase):
    def populate(self, path):
        path.mkdir(parents=True)
        for name in MODEL_FILES:
            (path / name).write_bytes(b"model")
        return path

    def test_registry_and_profile_compatibility(self):
        self.assertEqual(tuple(MODELS), ("tiny", "base", "small"))
        self.assertEqual([m.name for m in MODELS.values() if m.bundled], ["tiny"])
        self.assertEqual(ENGINE_ID, "faster-whisper-1.2.1-tiny-int8-d90ca5fe260221311c53c58e660288d3deb8d356")
        self.assertEqual(engine_profile(EngineConfig()), ENGINE_ID + "-segments-v1:auto")
        self.assertEqual(len({m.engine_id for m in MODELS.values()}), 3)
        for model in MODELS.values():
            self.assertRegex(model.revision, r"^[0-9a-f]{40}$")
        for name in ("medium", "large-v3", "../tiny", None):
            with self.assertRaises(ValueError):
                model_spec(name)

    def test_bundled_tiny_preferred_without_copying_legacy_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            bundle = root / "bundle"
            legacy = self.populate(data / "models" / model_spec().directory_name)
            self.assertEqual(model_directory(data, bundled_root=bundle), legacy)
            bundled = self.populate(bundle / model_spec().directory_name)
            self.assertEqual(model_directory(data, bundled_root=bundle), bundled)
            with patch("transcriber.model.model_directory", return_value=bundled), patch("transcriber.model.download_model") as download:
                self.assertEqual(prepare_model(data), bundled)
                download.assert_not_called()
            self.assertTrue(model_present(legacy))

    def test_optional_models_are_not_resolved_from_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("base", "small"):
                spec = model_spec(name)
                self.populate(root / "bundle" / spec.directory_name)
                path = model_directory(root / "data", name, bundled_root=root / "bundle")
                self.assertEqual(path, root / "data" / "models" / spec.directory_name)
                self.assertFalse(model_present(path))

    def test_incomplete_bundle_does_not_mask_legacy_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = model_spec()
            bundle = self.populate(root / "bundle" / spec.directory_name)
            (bundle / "model.bin").unlink()
            legacy = self.populate(root / "data" / "models" / spec.directory_name)
            self.assertEqual(model_directory(root / "data", bundled_root=root / "bundle"), legacy)
