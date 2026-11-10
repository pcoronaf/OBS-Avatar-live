"""Face detection with 5 landmarks.

Preferred: OpenCV YuNet (`face_detection_yunet_2023mar.onnx`, opencv_zoo, MIT),
placed by the user in the models directory. Fallback: OpenCV Haar cascades
shipped with opencv-python; landmarks are then *estimated* from face geometry
and flagged as such (adequate for the 2D audio rig, not for a precise swap).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from avatar_engine import paths

YUNET_FILE = "face_detection_yunet_2023mar.onnx"


@dataclass
class Face:
    bbox: np.ndarray  # x, y, w, h (float32)
    landmarks: np.ndarray  # (5,2): right eye, left eye (image left→right order: eye0 is image-left), nose, mouth L, mouth R
    score: float
    estimated_landmarks: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def center(self) -> np.ndarray:
        x, y, w, h = self.bbox
        return np.array([x + w / 2, y + h / 2], np.float32)

    @property
    def roll_deg(self) -> float:
        (x0, y0), (x1, y1) = self.landmarks[0], self.landmarks[1]
        return float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))


def _estimated_landmarks(x: float, y: float, w: float, h: float) -> np.ndarray:
    return np.array([
        [x + 0.31 * w, y + 0.40 * h],
        [x + 0.69 * w, y + 0.40 * h],
        [x + 0.50 * w, y + 0.60 * h],
        [x + 0.36 * w, y + 0.78 * h],
        [x + 0.64 * w, y + 0.78 * h],
    ], np.float32)


class FaceDetector:
    def __init__(self, model_path: str | Path | None = None, input_size: int = 320, score_threshold: float = 0.7):
        self.input_size = input_size
        self.score_threshold = score_threshold
        model = Path(model_path) if model_path else paths.models_dir() / YUNET_FILE
        self.backend = "haar"
        self._yunet = None
        if model.exists() and hasattr(cv2, "FaceDetectorYN"):
            try:
                self._yunet = cv2.FaceDetectorYN.create(str(model), "", (input_size, input_size), score_threshold,
                                                        0.3, 5000)
                self.backend = "yunet"
            except cv2.error:
                self._yunet = None
        if self._yunet is None:
            base = cv2.data.haarcascades
            self._haar = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
            self._eyes = cv2.CascadeClassifier(base + "haarcascade_eye.xml")

    def detect(self, bgr: np.ndarray, max_faces: int = 1) -> list[Face]:
        h, w = bgr.shape[:2]
        scale = min(1.0, self.input_size / max(h, w))
        small = cv2.resize(bgr, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA) \
            if scale < 1.0 else bgr
        faces = self._detect_yunet(small) if self._yunet is not None else self._detect_haar(small)
        for f in faces:
            f.bbox = f.bbox / scale
            f.landmarks = f.landmarks / scale
        faces.sort(key=lambda f: f.bbox[2] * f.bbox[3] * f.score, reverse=True)
        return faces[:max_faces]

    def _detect_yunet(self, img: np.ndarray) -> list[Face]:
        h, w = img.shape[:2]
        self._yunet.setInputSize((w, h))
        _, dets = self._yunet.detect(img)
        out = []
        if dets is None:
            return out
        for d in dets:
            lm = d[4:14].reshape(5, 2).astype(np.float32)
            out.append(Face(d[0:4].astype(np.float32), lm, float(d[14])))
        return out

    def _detect_haar(self, img: np.ndarray) -> list[Face]:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        min_side = max(24, int(min(gray.shape) * 0.12))
        rects = self._haar.detectMultiScale(gray, 1.1, 5, minSize=(min_side, min_side))
        out = []
        for (x, y, w, h) in rects:
            lm = _estimated_landmarks(x, y, w, h)
            estimated = True
            roi = gray[y:y + int(h * 0.6), x:x + w]
            eyes = self._eyes.detectMultiScale(roi, 1.1, 6, minSize=(max(8, w // 10), max(8, w // 10)))
            if len(eyes) >= 2:
                eyes = sorted(eyes, key=lambda e: e[2] * e[3], reverse=True)[:2]
                centers = sorted([(x + ex + ew / 2, y + ey + eh / 2) for ex, ey, ew, eh in eyes])
                if abs(centers[1][0] - centers[0][0]) > w * 0.2:
                    lm[0], lm[1] = centers[0], centers[1]
            out.append(Face(np.array([x, y, w, h], np.float32), lm, 0.8, estimated_landmarks=estimated))
        return out
