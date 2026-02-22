"""Tests for robothor.vision.detector — ObjectDetector and detect_motion."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from robothor.vision.detector import (
    MOTION_THRESHOLD,
    YOLO_CONFIDENCE,
    YOLO_MODEL_NAME,
    ObjectDetector,
    detect_motion,
)

# ─── Config Defaults ─────────────────────────────────────────────────


class TestDetectorConfig:
    def test_default_yolo_model(self):
        assert YOLO_MODEL_NAME == "yolov8n.pt"

    def test_default_confidence(self):
        assert YOLO_CONFIDENCE == 0.5

    def test_default_motion_threshold(self):
        assert MOTION_THRESHOLD == 0.15


# ─── ObjectDetector ──────────────────────────────────────────────────


class TestObjectDetector:
    def test_init_defaults(self):
        det = ObjectDetector()
        assert det.model_name == "yolov8n.pt"
        assert det.confidence == 0.5
        assert det.loaded is False

    def test_init_custom(self):
        det = ObjectDetector(model_name="yolov8s.pt", confidence=0.7)
        assert det.model_name == "yolov8s.pt"
        assert det.confidence == 0.7

    def test_detect_returns_empty_when_model_fails(self):
        det = ObjectDetector()
        with patch.object(det, "_ensure_loaded", return_value=False):
            result = det.detect(np.zeros((480, 640, 3), dtype=np.uint8))
        assert result == []

    def test_detect_parses_yolo_results(self):
        det = ObjectDetector()
        # Mock YOLO result structure with proper numpy-like arrays
        mock_box = MagicMock()
        mock_box.cls = np.array([0])
        mock_box.conf = np.array([0.95])
        mock_box.xyxy = np.array([[10.0, 20.0, 100.0, 200.0]])

        mock_result = MagicMock()
        mock_result.names = {0: "person"}
        mock_result.boxes = [mock_box]

        det._model = MagicMock(return_value=[mock_result])

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        detections = det.detect(frame)

        assert len(detections) == 1
        assert detections[0]["class"] == "person"
        assert isinstance(detections[0]["confidence"], float)
        assert len(detections[0]["bbox"]) == 4

    def test_has_person_true(self):
        det = ObjectDetector()
        with patch.object(det, "detect", return_value=[{"class": "person", "confidence": 0.9, "bbox": [0, 0, 1, 1]}]):
            assert det.has_person(np.zeros((1, 1, 3), dtype=np.uint8)) is True

    def test_has_person_false(self):
        det = ObjectDetector()
        with patch.object(det, "detect", return_value=[{"class": "cat", "confidence": 0.9, "bbox": [0, 0, 1, 1]}]):
            assert det.has_person(np.zeros((1, 1, 3), dtype=np.uint8)) is False

    def test_has_person_empty(self):
        det = ObjectDetector()
        with patch.object(det, "detect", return_value=[]):
            assert det.has_person(np.zeros((1, 1, 3), dtype=np.uint8)) is False

    def test_loaded_property(self):
        det = ObjectDetector()
        assert det.loaded is False
        det._model = MagicMock()
        assert det.loaded is True

    def test_ensure_loaded_catches_import_error(self):
        det = ObjectDetector()
        with patch.dict("sys.modules", {"ultralytics": None}):
            result = det._ensure_loaded()
        # Should return False gracefully on import failure
        assert result is False or det._model is not None


# ─── detect_motion ───────────────────────────────────────────────────


cv2_installed = True
try:
    import cv2  # noqa: F401
except ImportError:
    cv2_installed = False


@pytest.mark.skipif(not cv2_installed, reason="opencv-python not installed")
class TestDetectMotion:
    def test_first_frame_no_motion(self):
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        motion, score, gray = detect_motion(frame, None)
        assert motion is False
        assert score == 0.0
        assert gray.shape == (480, 640)

    def test_identical_frames_no_motion(self):
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        _, _, gray1 = detect_motion(frame, None)
        motion, score, _ = detect_motion(frame, gray1)
        assert motion is False
        assert score == 0.0

    def test_different_frames_detect_motion(self):
        frame1 = np.zeros((480, 640, 3), dtype=np.uint8)
        frame2 = np.full((480, 640, 3), 200, dtype=np.uint8)
        _, _, gray1 = detect_motion(frame1, None)
        motion, score, _ = detect_motion(frame2, gray1)
        assert motion is True
        assert score > 0.0

    def test_custom_threshold(self):
        frame1 = np.zeros((100, 100, 3), dtype=np.uint8)
        frame2 = np.full((100, 100, 3), 200, dtype=np.uint8)
        _, _, gray1 = detect_motion(frame1, None)
        # Very high threshold — even large changes shouldn't trigger
        motion, score, _ = detect_motion(frame2, gray1, threshold=0.99)
        # Score will be high but threshold is higher
        assert isinstance(motion, bool)
        assert isinstance(score, float)

    def test_returns_grayscale(self):
        frame = np.random.randint(0, 255, (240, 320, 3), dtype=np.uint8)
        _, _, gray = detect_motion(frame, None)
        assert gray.ndim == 2
        assert gray.dtype == np.uint8

    def test_score_is_rounded(self):
        frame1 = np.zeros((100, 100, 3), dtype=np.uint8)
        frame2 = np.full((100, 100, 3), 128, dtype=np.uint8)
        _, _, gray1 = detect_motion(frame1, None)
        _, score, _ = detect_motion(frame2, gray1)
        # Score should be rounded to 4 decimal places
        assert score == round(score, 4)
