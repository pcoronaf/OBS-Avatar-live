"""DeepFaceLive / DeepFaceLab `.dfm` face models (a model trained on one specific face).

A `.dfm` file is an ONNX graph with (as exported by DeepFaceLab/DeepFaceLive):
  input   ``in_face:0``      N×H×W×3 float32, BGR in [0, 1] (NHWC)
  input   ``morph_value:0``  [1] float32 — only in "morphable" (AMP) models
  outputs ``out_face_mask:0`` N×H×W×1, ``out_celeb_face:0`` N×H×W×3, ``out_celeb_face_mask:0`` N×H×W×1
Unlike inswapper there is no source photo/identity input: the identity is in the weights.

Alignment: DeepFaceLab crops faces from 68 landmarks; this engine detects 5. The crop below
approximates a DeepFaceLab *whole face* (WF) crop from those 5 points. `coverage` (<1 zooms out,
e.g. ~0.75 for HEAD models) and `offset_y` let the user match the crop their model was trained on.
This file is an independent implementation of that I/O contract (no DeepFaceLab code is copied).
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from avatar_engine.engines.swap_pipeline import SwapResult, make_session
from avatar_engine.models.manifest import ModelError
from avatar_engine.performance.provider_probe import ProviderCandidate
from avatar_engine.vision import alignment
from avatar_engine.vision.face_detect import FaceDetector
from avatar_engine.vision.face_tracking import FaceTracker

# 5-point layout of a DeepFaceLab whole-face crop, normalised to [0, 1]
# (eyes, nose tip, mouth corners). Approximate: see module docstring.
WF_TEMPLATE = np.array([
    [0.345, 0.430],
    [0.655, 0.430],
    [0.500, 0.570],
    [0.375, 0.700],
    [0.625, 0.700],
], np.float32)
TEMPLATE_CENTER = np.array([0.5, 0.56], np.float32)
MAX_DFM_BYTES = 2 * 1024 ** 3


def dfm_template(size: int, coverage: float = 1.0, offset_y: float = 0.0) -> np.ndarray:
    pts = (WF_TEMPLATE - TEMPLATE_CENTER) * float(coverage) + TEMPLATE_CENTER + np.array([0.0, offset_y], np.float32)
    return pts * size


def validate_dfm_path(path: str | Path) -> Path:
    p = Path(path)
    if p.suffix.lower() != ".dfm":
        raise ModelError("the face model must be a .dfm file")
    if not p.is_file():
        raise ModelError(f"DFM model not found: {p}")
    if p.stat().st_size > MAX_DFM_BYTES:
        raise ModelError("DFM model larger than 2 GB")
    return p


class DfmModel:
    def __init__(self, session) -> None:
        self.sess = session
        inputs = {i.name: i for i in session.get_inputs()}
        face_in = next((i for n, i in inputs.items() if n.startswith("in_face")), None)
        if face_in is None:
            face_in = next((i for i in inputs.values() if len(i.shape) == 4), None)
        if face_in is None:
            raise ModelError("not a DFM face model: no N×H×W×3 'in_face' input")
        self.input_name = face_in.name
        shape = face_in.shape
        if len(shape) != 4 or (isinstance(shape[3], int) and shape[3] != 3):
            raise ModelError(f"unsupported DFM input shape {shape} (expected N×H×W×3)")
        self.size = int(shape[1]) if isinstance(shape[1], int) else 224
        self.morph_name = next((n for n in inputs if n.startswith("morph_value")), None)
        outs = [o.name for o in session.get_outputs()]
        by_base = {n.split(":")[0]: n for n in outs}
        names = [by_base.get(b) for b in ("out_face_mask", "out_celeb_face", "out_celeb_face_mask")]
        if None in names:
            if len(outs) < 3:
                raise ModelError(f"not a DFM face model: expected 3 outputs, got {outs}")
            names = outs[:3]  # DeepFaceLive output order: face mask, celeb face, celeb mask
        self.out_face_mask, self.out_celeb, self.out_celeb_mask = names

    @property
    def morphable(self) -> bool:
        return self.morph_name is not None

    def run(self, crop_bgr: np.ndarray, morph: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
        """Returns (face BGR float [0,1] H×W×3, mask float [0,1] H×W)."""
        if crop_bgr.shape[0] != self.size:
            crop_bgr = cv2.resize(crop_bgr, (self.size, self.size), interpolation=cv2.INTER_LINEAR)
        x = (crop_bgr.astype(np.float32) / 255.0)[None]
        feeds = {self.input_name: x}
        if self.morph_name:
            feeds[self.morph_name] = np.array([morph], np.float32)
        celeb, celeb_mask, face_mask = self.sess.run([self.out_celeb, self.out_celeb_mask, self.out_face_mask], feeds)
        face = np.clip(celeb[0], 0.0, 1.0)
        mask = np.clip(celeb_mask[0, ..., 0] * face_mask[0, ..., 0], 0.0, 1.0)
        return face, mask


class DfmPipeline:
    """Same interface as `SwapPipeline` (process / set_source / probe / detector / tracker)."""

    def __init__(self, detector: FaceDetector, model: DfmModel, detection_every_n_frames: int = 6,
                 coverage: float = 1.0, offset_y: float = 0.0, morph: float = 1.0) -> None:
        self.detector = detector
        self.tracker = FaceTracker(detector, detection_every_n_frames)
        self.model = model
        self.coverage, self.offset_y, self.morph = coverage, offset_y, morph
        self.size = model.size
        self._template = dfm_template(self.size, coverage, offset_y)
        self.source_latent = None  # identity is in the model weights

    def set_source(self, img: np.ndarray, landmarks: np.ndarray | None = None):
        return None  # no source photo needed

    def set_detection_interval(self, n: int) -> None:
        self.tracker.detect_every = max(1, n)

    def probe(self, crop_bgr: np.ndarray) -> np.ndarray:
        face, _ = self.model.run(crop_bgr, self.morph)
        return face

    def align(self, frame: np.ndarray, landmarks: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        M = alignment.umeyama_similarity(landmarks, self._template)
        crop = cv2.warpAffine(frame, M, (self.size, self.size), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)
        return crop, M

    def process(self, frame: np.ndarray) -> SwapResult:
        t = {}
        t0 = time.perf_counter()
        face = self.tracker.update(frame)
        t["detect"] = (time.perf_counter() - t0) * 1000
        if face is None:
            return SwapResult(frame, False, t)
        t1 = time.perf_counter()
        crop, M = self.align(frame, face.landmarks)
        t["align"] = (time.perf_counter() - t1) * 1000
        t2 = time.perf_counter()
        out, mask = self.model.run(crop, self.morph)
        t["swap"] = (time.perf_counter() - t2) * 1000
        t3 = time.perf_counter()
        # Soften the model mask edge so the paste-back has no visible seam.
        k = max(3, int(self.size * 0.04) | 1)
        mask = cv2.GaussianBlur(cv2.erode(mask, np.ones((3, 3), np.uint8)), (k, k), 0)
        out_bgr = (out * 255.0).astype(np.uint8)
        result = alignment.paste_back(frame.copy(), out_bgr, M, mask.astype(np.float32))
        t["blend"] = (time.perf_counter() - t3) * 1000
        return SwapResult(result, True, t)


def build_dfm_pipeline(candidate: ProviderCandidate, models: dict, threads: int, detection_every_n_frames: int = 6,
                       detector_size: int = 640, options: dict | None = None) -> DfmPipeline:
    options = options or {}
    detector = FaceDetector(models.get("yunet_face_detector"), input_size=detector_size)
    if detector.backend != "yunet":
        raise ModelError("the face-swap engines need the YuNet detector model (landmarks for alignment)")
    model = DfmModel(make_session(models["dfm_face_model"], candidate, threads))
    return DfmPipeline(detector, model, detection_every_n_frames, options.get("dfm_coverage", 1.0),
                       options.get("dfm_offset_y", 0.0), options.get("dfm_morph", 1.0))
