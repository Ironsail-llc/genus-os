"""
Face recognition via InsightFace (ArcFace embeddings).

Provides face detection, embedding extraction, matching against
enrolled faces, and face enrollment from camera frames.

Usage:
    from robothor.vision.face import FaceRecognizer

    recognizer = FaceRecognizer(data_dir="/path/to/faces")
    faces = recognizer.detect(frame)
    name, similarity = recognizer.match(faces[0]["embedding"])
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

FACE_MODEL = os.environ.get("ROBOTHOR_FACE_MODEL", "buffalo_l")
FACE_MATCH_THRESHOLD = float(os.environ.get("FACE_MATCH_THRESHOLD", "0.45"))


class FaceRecognizer:
    """InsightFace-based face recognizer with persistent enrollment.

    Enrolled face embeddings are stored as JSON and loaded on init.
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        model_name: str | None = None,
        match_threshold: float | None = None,
    ):
        self.data_dir = Path(data_dir) if data_dir else Path(
            os.environ.get("FACE_DATA_DIR", "faces")
        )
        self.model_name = model_name or FACE_MODEL
        self.match_threshold = match_threshold or FACE_MATCH_THRESHOLD
        self._app: Any = None
        self.enrolled: dict[str, np.ndarray] = {}
        self._load_enrolled()

    def _ensure_loaded(self) -> bool:
        """Load InsightFace model if not already loaded."""
        if self._app is not None:
            return True
        try:
            from insightface.app import FaceAnalysis
            self._app = FaceAnalysis(
                name=self.model_name,
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            )
            self._app.prepare(ctx_id=0, det_size=(640, 640))
            logger.info("InsightFace %s loaded", self.model_name)
            return True
        except Exception as e:
            logger.warning("InsightFace failed to load: %s", e)
            return False

    @property
    def loaded(self) -> bool:
        """Check if model is loaded."""
        return self._app is not None

    def _load_enrolled(self) -> None:
        """Load enrolled face embeddings from disk."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        face_file = self.data_dir / "enrolled_faces.json"
        if face_file.exists():
            with open(face_file) as f:
                data = json.load(f)
            for name, emb_list in data.items():
                self.enrolled[name] = np.array(emb_list, dtype=np.float32)
            logger.info("Loaded %d enrolled faces", len(self.enrolled))

    def _save_enrolled(self) -> None:
        """Save enrolled face embeddings to disk."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        face_file = self.data_dir / "enrolled_faces.json"
        data = {name: emb.tolist() for name, emb in self.enrolled.items()}
        with open(face_file, "w") as f:
            json.dump(data, f)
        logger.info("Saved %d enrolled faces", len(self.enrolled))

    def detect(self, frame: np.ndarray) -> list[dict]:
        """Detect faces and extract embeddings from a frame.

        Returns list of dicts with 'bbox', 'embedding', 'det_score'.
        """
        if not self._ensure_loaded():
            return []
        faces = self._app.get(frame)
        return [
            {
                "bbox": face.bbox.tolist(),
                "embedding": face.normed_embedding,
                "det_score": float(face.det_score),
            }
            for face in faces
        ]

    def match(self, embedding: np.ndarray) -> tuple[str | None, float]:
        """Match a face embedding against enrolled faces.

        Returns (name, similarity) or (None, best_sim) if no match.
        """
        if not self.enrolled:
            return None, 0.0

        best_name: str | None = None
        best_sim = 0.0
        for name, enrolled_emb in self.enrolled.items():
            sim = float(np.dot(embedding, enrolled_emb) /
                        (np.linalg.norm(embedding) * np.linalg.norm(enrolled_emb)))
            if sim > best_sim:
                best_sim = sim
                best_name = name

        if best_sim >= self.match_threshold:
            return best_name, best_sim
        return None, best_sim

    def enroll(self, name: str, embeddings: list[np.ndarray]) -> bool:
        """Enroll a person by averaging multiple face embeddings.

        Args:
            name: Person's name.
            embeddings: List of face embeddings (at least 1).

        Returns:
            True if successfully enrolled.
        """
        if not embeddings:
            return False
        avg = np.mean(embeddings, axis=0)
        avg = avg / np.linalg.norm(avg)  # Normalize
        self.enrolled[name] = avg
        self._save_enrolled()
        logger.info("Enrolled face for: %s (from %d samples)", name, len(embeddings))
        return True

    def unenroll(self, name: str) -> bool:
        """Remove a person from enrollment."""
        if name in self.enrolled:
            del self.enrolled[name]
            self._save_enrolled()
            return True
        return False

    @property
    def enrolled_names(self) -> list[str]:
        """List all enrolled person names."""
        return list(self.enrolled.keys())
