"""Amortised face tracking: full detection every N frames, optical flow in between."""

from __future__ import annotations

import time

import cv2
import numpy as np

from avatar_engine.vision.face_detect import Face, FaceDetector


class LandmarkSmoother:
    """Velocity-adaptive EMA (One-Euro-like): smooth when still, responsive when moving."""

    def __init__(self, min_alpha: float = 0.35, max_alpha: float = 0.9, speed_ref: float = 0.08) -> None:
        self.min_alpha, self.max_alpha, self.speed_ref = min_alpha, max_alpha, speed_ref
        self.value: np.ndarray | None = None

    def __call__(self, pts: np.ndarray, scale: float) -> np.ndarray:
        if self.value is None:
            self.value = pts.copy()
            return pts
        speed = float(np.abs(pts - self.value).mean()) / max(scale, 1.0)
        a = self.min_alpha + (self.max_alpha - self.min_alpha) * min(1.0, speed / self.speed_ref)
        self.value = self.value + a * (pts - self.value)
        return self.value.copy()

    def reset(self) -> None:
        self.value = None


class FaceTracker:
    def __init__(self, detector: FaceDetector, detect_every: int = 6) -> None:
        self.detector = detector
        self.detect_every = max(1, detect_every)
        self._count = 0
        self._prev_gray: np.ndarray | None = None
        self.face: Face | None = None
        self.smoother = LandmarkSmoother()
        self.last_detect_ms = 0.0
        self.last_track_ms = 0.0
        self.lost = 0

    def reset(self) -> None:
        self._count, self._prev_gray, self.face = 0, None, None
        self.smoother.reset()

    def update(self, frame: np.ndarray) -> Face | None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        need_detect = self.face is None or self._count % self.detect_every == 0
        face = None
        if not need_detect and self._prev_gray is not None:
            t0 = time.perf_counter()
            face = self._track(gray)
            self.last_track_ms = (time.perf_counter() - t0) * 1000
        if face is None:
            t0 = time.perf_counter()
            faces = self.detector.detect(frame, max_faces=1)
            self.last_detect_ms = (time.perf_counter() - t0) * 1000
            face = faces[0] if faces else None
        self._count += 1
        self._prev_gray = gray
        if face is None:
            self.lost += 1
            self.face = None
            self.smoother.reset()
            return None
        self.lost = 0
        face.landmarks = self.smoother(face.landmarks, float(face.bbox[2]))
        self.face = face
        return face

    def _track(self, gray: np.ndarray) -> Face | None:
        prev = self.face
        pts0 = prev.landmarks.reshape(-1, 1, 2).astype(np.float32)
        pts1, st, err = cv2.calcOpticalFlowPyrLK(self._prev_gray, gray, pts0, None, winSize=(21, 21), maxLevel=3)
        if pts1 is None or st is None:
            return None
        good = st.reshape(-1).astype(bool)
        if good.sum() < 3:  # too few trackable points (flat skin, blur): re-detect
            return None
        pts1 = pts1.reshape(-1, 2)
        shift = pts1[good] - prev.landmarks[good]
        if float(np.abs(shift).max()) > prev.bbox[2] * 0.35:  # implausible jump: re-detect
            return None
        d = np.median(shift, axis=0)
        # Points that tracked keep their own motion; lost ones follow the face translation.
        new = prev.landmarks + d
        new[good] = pts1[good]
        bbox = prev.bbox.copy()
        bbox[:2] += d
        return Face(bbox, new.astype(np.float32), prev.score, prev.estimated_landmarks)
