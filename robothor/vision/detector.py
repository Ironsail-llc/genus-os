"""
Object detection via YOLO and motion detection via frame differencing.

YOLO models are loaded lazily on first use. Motion detection uses OpenCV
frame differencing with configurable thresholds.

Usage:
    from robothor.vision.detector import ObjectDetector, detect_motion

    detector = ObjectDetector()
    objects = detector.detect(frame)

    motion, score = detect_motion(frame, prev_gray)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Configuration
YOLO_MODEL_NAME = os.environ.get("ROBOTHOR_YOLO_MODEL", "yolov8n.pt")
YOLO_CONFIDENCE = float(os.environ.get("YOLO_CONFIDENCE", "0.5"))
MOTION_THRESHOLD = float(os.environ.get("MOTION_THRESHOLD", "0.15"))


class ObjectDetector:
    """YOLO-based object detector with lazy model loading.

    Models stay in memory once loaded (~6MB for YOLOv8-nano).
    """

    def __init__(self, model_name: str | None = None, confidence: float | None = None):
        self.model_name = model_name or YOLO_MODEL_NAME
        self.confidence = confidence or YOLO_CONFIDENCE
        self._model: Any = None

    def _ensure_loaded(self) -> bool:
        """Load YOLO model if not already loaded."""
        if self._model is not None:
            return True
        try:
            from ultralytics import YOLO
            self._model = YOLO(self.model_name)
            logger.info("YOLO model loaded: %s", self.model_name)
            return True
        except Exception as e:
            logger.error("Failed to load YOLO model: %s", e)
            return False

    @property
    def loaded(self) -> bool:
        """Check if model is loaded."""
        return self._model is not None

    def detect(self, frame: np.ndarray) -> list[dict]:
        """Run YOLO detection on a frame.

        Args:
            frame: BGR image as numpy array.

        Returns:
            List of detections, each with 'class', 'confidence', 'bbox'.
        """
        if not self._ensure_loaded():
            return []

        results = self._model(frame, verbose=False, conf=self.confidence)
        detections = []
        for r in results:
            for box in r.boxes:
                detections.append({
                    "class": r.names[int(box.cls[0])],
                    "confidence": round(float(box.conf[0]), 3),
                    "bbox": [round(float(x), 1) for x in box.xyxy[0].tolist()],
                })
        return detections

    def has_person(self, frame: np.ndarray) -> bool:
        """Quick check: is there a person in the frame?"""
        detections = self.detect(frame)
        return any(d["class"] == "person" for d in detections)


def detect_motion(
    frame: np.ndarray,
    prev_gray: np.ndarray | None,
    threshold: float | None = None,
) -> tuple[bool, float, np.ndarray]:
    """Detect motion via frame differencing.

    Args:
        frame: Current BGR frame.
        prev_gray: Previous grayscale frame (or None for first frame).
        threshold: Motion threshold (fraction of changed pixels).

    Returns:
        (motion_detected, score, current_gray) — pass current_gray as
        prev_gray on the next call.
    """
    import cv2

    threshold = threshold or MOTION_THRESHOLD

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (21, 21), 0)

    if prev_gray is None:
        return False, 0.0, gray

    delta = cv2.absdiff(prev_gray, gray)
    _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
    score = float(np.count_nonzero(thresh)) / float(thresh.size)

    return score >= threshold, round(score, 4), gray
