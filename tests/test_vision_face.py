"""Tests for robothor.vision.face — FaceRecognizer."""

import json

import numpy as np

from robothor.vision.face import FACE_MATCH_THRESHOLD, FACE_MODEL, FaceRecognizer

# ─── Config Defaults ─────────────────────────────────────────────────


class TestFaceConfig:
    def test_default_model(self):
        assert FACE_MODEL == "buffalo_l"

    def test_default_threshold(self):
        assert FACE_MATCH_THRESHOLD == 0.45


# ─── FaceRecognizer Init ─────────────────────────────────────────────


class TestFaceRecognizerInit:
    def test_init_defaults(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        assert rec.model_name == "buffalo_l"
        assert rec.match_threshold == 0.45
        assert rec.loaded is False
        assert rec.enrolled == {}

    def test_init_custom(self, tmp_path):
        rec = FaceRecognizer(
            data_dir=tmp_path,
            model_name="buffalo_s",
            match_threshold=0.6,
        )
        assert rec.model_name == "buffalo_s"
        assert rec.match_threshold == 0.6

    def test_data_dir_created(self, tmp_path):
        data_dir = tmp_path / "faces"
        FaceRecognizer(data_dir=data_dir)
        assert data_dir.exists()


# ─── Enrollment Persistence ──────────────────────────────────────────


class TestEnrollment:
    def _make_embedding(self, seed: int = 42) -> np.ndarray:
        rng = np.random.RandomState(seed)
        emb = rng.randn(512).astype(np.float32)
        return emb / np.linalg.norm(emb)

    def test_enroll_single(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        emb = self._make_embedding()
        assert rec.enroll("Alice", [emb]) is True
        assert "Alice" in rec.enrolled_names

    def test_enroll_multiple_embeddings(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        embs = [self._make_embedding(seed=i) for i in range(5)]
        assert rec.enroll("Bob", embs) is True
        # Average embedding should be normalized
        norm = np.linalg.norm(rec.enrolled["Bob"])
        assert abs(norm - 1.0) < 1e-5

    def test_enroll_empty_fails(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        assert rec.enroll("Nobody", []) is False

    def test_enroll_persists_to_disk(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        emb = self._make_embedding()
        rec.enroll("Alice", [emb])

        # Load a new recognizer from same dir
        rec2 = FaceRecognizer(data_dir=tmp_path)
        assert "Alice" in rec2.enrolled_names
        assert rec2.enrolled["Alice"].shape == (512,)

    def test_unenroll(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        rec.enroll("Alice", [self._make_embedding()])
        assert rec.unenroll("Alice") is True
        assert "Alice" not in rec.enrolled_names

    def test_unenroll_nonexistent(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        assert rec.unenroll("Nobody") is False

    def test_enrolled_names(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        rec.enroll("Alice", [self._make_embedding(1)])
        rec.enroll("Bob", [self._make_embedding(2)])
        assert sorted(rec.enrolled_names) == ["Alice", "Bob"]

    def test_enrollment_file_is_valid_json(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        rec.enroll("Alice", [self._make_embedding()])
        face_file = tmp_path / "enrolled_faces.json"
        assert face_file.exists()
        data = json.loads(face_file.read_text())
        assert "Alice" in data
        assert isinstance(data["Alice"], list)
        assert len(data["Alice"]) == 512


# ─── Matching ────────────────────────────────────────────────────────


class TestMatching:
    def _make_embedding(self, seed: int = 42) -> np.ndarray:
        rng = np.random.RandomState(seed)
        emb = rng.randn(512).astype(np.float32)
        return emb / np.linalg.norm(emb)

    def test_match_no_enrolled(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        name, sim = rec.match(self._make_embedding())
        assert name is None
        assert sim == 0.0

    def test_match_exact(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        emb = self._make_embedding()
        rec.enroll("Alice", [emb])
        name, sim = rec.match(emb)
        assert name == "Alice"
        assert sim > 0.99  # Same embedding should be ~1.0

    def test_match_similar(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        emb1 = self._make_embedding(42)
        # Create a very slightly perturbed embedding (small noise)
        noise = np.random.RandomState(99).randn(512).astype(np.float32) * 0.01
        emb2 = emb1 + noise
        emb2 = emb2 / np.linalg.norm(emb2)

        rec.enroll("Alice", [emb1])
        name, sim = rec.match(emb2)
        # Should match since perturbation is tiny
        assert name == "Alice"
        assert sim > 0.95

    def test_match_different_returns_none(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path, match_threshold=0.9)
        emb1 = self._make_embedding(1)
        emb2 = self._make_embedding(99)  # Different seed = very different
        rec.enroll("Alice", [emb1])
        name, sim = rec.match(emb2)
        assert name is None
        assert isinstance(sim, float)

    def test_match_best_of_multiple(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        emb_alice = self._make_embedding(1)
        emb_bob = self._make_embedding(2)
        rec.enroll("Alice", [emb_alice])
        rec.enroll("Bob", [emb_bob])

        name, sim = rec.match(emb_alice)
        assert name == "Alice"

    def test_detect_returns_empty_when_model_not_loaded(self, tmp_path):
        rec = FaceRecognizer(data_dir=tmp_path)
        # Model won't load without insightface installed
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        result = rec.detect(frame)
        assert result == []
