"""Face-swap pipeline for E2/E2a/E3 (own implementation; Deep-Live-Cam is only
an analysis reference, no code is copied — see docs/LICENSE_REVIEW.md).

Stages: track (detect every N + optical flow) → align (5-pt similarity) →
swap (ONNX, "inswapper-compatible" I/O) → tone match → soft paste-back.
No restoration/enhancer, single face, no Poisson blending (spec §5 E2).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from avatar_engine.models.manifest import ModelError
from avatar_engine.performance.provider_probe import ProviderCandidate, fault
from avatar_engine.vision import alignment
from avatar_engine.vision.face_detect import FaceDetector
from avatar_engine.vision.face_tracking import FaceTracker


class ProviderError(RuntimeError):
    pass


def make_session(model_path: Path, candidate: ProviderCandidate, threads: int):
    import onnxruntime as ort

    if fault("provider") and candidate.key != "cpu":
        raise ProviderError(f"fault injection: {candidate.label} failed to initialise")
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(0, threads)  # 0: ONNX Runtime uses all physical cores
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.log_severity_level = 3
    if candidate.key == "directml":
        so.enable_mem_pattern = False  # required by the DirectML EP
    try:
        sess = ort.InferenceSession(str(model_path), so, providers=candidate.providers())
    except Exception as exc:
        raise ProviderError(f"{candidate.label}: {exc}") from exc
    active = sess.get_providers()
    if candidate.key != "cpu" and (not active or active[0] != candidate.ort_name):
        raise ProviderError(f"{candidate.label}: provider not active (got {active})")
    return sess


class Recognizer:
    """ArcFace-style identity embedding (input N×3×S×S, (x-127.5)/127.5, RGB)."""

    def __init__(self, session) -> None:
        self.sess = session
        inp = session.get_inputs()[0]
        self.input_name = inp.name
        self.size = int(inp.shape[2]) if isinstance(inp.shape[2], int) else 112

    def embed(self, img: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
        crop, _ = alignment.align(img, landmarks, self.size)
        x = (cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) - 127.5) / 127.5
        x = x.transpose(2, 0, 1)[None]
        emb = self.sess.run(None, {self.input_name: x})[0].reshape(-1).astype(np.float32)
        return emb / (np.linalg.norm(emb) + 1e-8)


def _emap_cache_path(model_path: Path) -> Path:
    from avatar_engine import paths

    st = model_path.stat()
    key = f"{model_path.name}-{st.st_size}-{int(st.st_mtime)}".replace(" ", "_")
    d = paths.data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"emap_{key}.npy"


def _load_emap(model_path: Path) -> np.ndarray | None:
    """512x512 identity mapping embedded in inswapper-style models.

    Reading it means parsing the whole (~550 MB) model with `onnx`, which is slow and needs a lot of
    memory, so the result is cached (keyed by file name, size and mtime) after the first read."""
    sidecar = model_path.with_suffix(".emap.npy")
    if sidecar.exists():
        return np.load(sidecar).astype(np.float32)
    try:
        cache = _emap_cache_path(model_path)
        if cache.exists():
            return np.load(cache).astype(np.float32)
    except OSError:
        cache = None
    try:
        import onnx
        from onnx import numpy_helper

        graph = onnx.load(str(model_path)).graph
        emap = None
        for init in reversed(graph.initializer):
            if list(init.dims) == [512, 512]:
                emap = numpy_helper.to_array(init).astype(np.float32)
                break
        del graph
    except Exception:
        return None
    if emap is not None and cache is not None:
        try:
            np.save(cache, emap)
        except OSError:
            pass
    return emap


class Swapper:
    """Swap model with inputs (target image N×3×S×S in [0,1] RGB, source latent N×D)."""

    def __init__(self, session, model_path: Path) -> None:
        self.sess = session
        self.target_name = self.latent_name = None
        for inp in session.get_inputs():
            if len(inp.shape) == 4:
                self.target_name = inp.name
                self.size = int(inp.shape[2]) if isinstance(inp.shape[2], int) else 128
            else:
                self.latent_name = inp.name
                self.latent_dim = int(inp.shape[1]) if isinstance(inp.shape[1], int) else 512
        if self.target_name is None or self.latent_name is None:
            raise ModelError("swap model must have an image input and a latent input")
        self.emap = _load_emap(model_path)
        if self.emap is None and self.latent_dim == 512:
            # inswapper-style models embed a 512x512 identity mapping; without it the result is wrong.
            raise ModelError("could not read the identity mapping (emap) from the swap model; "
                             "reinstall the engine or provide <model>.emap.npy next to it")
        self.output_name = session.get_outputs()[0].name

    def latent(self, embedding: np.ndarray) -> np.ndarray:
        lat = embedding.reshape(1, -1).astype(np.float32)
        if self.emap is not None and self.emap.shape[0] == lat.shape[1]:
            lat = lat @ self.emap
        return lat / (np.linalg.norm(lat) + 1e-8)

    def run(self, crop_bgr: np.ndarray, latent: np.ndarray) -> np.ndarray:
        x = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        y = self.sess.run([self.output_name], {self.target_name: x, self.latent_name: latent})[0][0]
        return y

    @staticmethod
    def to_bgr(y: np.ndarray) -> np.ndarray:
        img = np.clip(y.transpose(1, 2, 0) * 255.0, 0, 255).astype(np.uint8)
        return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


@dataclass
class SwapResult:
    frame: np.ndarray
    face_found: bool
    timings_ms: dict


class SwapPipeline:
    def __init__(self, detector: FaceDetector, recognizer: Recognizer, swapper: Swapper,
                 detection_every_n_frames: int = 6, color_match: bool = True) -> None:
        self.detector = detector
        self.tracker = FaceTracker(detector, detection_every_n_frames)
        self.recognizer = recognizer
        self.swapper = swapper
        self.color_match = color_match
        self.source_latent: np.ndarray | None = None

    def set_source(self, img: np.ndarray, landmarks: np.ndarray | None = None) -> np.ndarray:
        if landmarks is None:
            faces = self.detector.detect(img, max_faces=1)
            if not faces:
                raise ModelError("no face in source photo")
            landmarks = faces[0].landmarks
        emb = self.recognizer.embed(img, landmarks)
        self.source_latent = self.swapper.latent(emb)
        return emb

    def set_detection_interval(self, n: int) -> None:
        self.tracker.detect_every = max(1, n)

    def probe(self, crop_bgr: np.ndarray) -> np.ndarray:
        """Raw model output for a fixed crop (benchmark: compare each provider with the CPU)."""
        if crop_bgr.shape[0] != self.swapper.size:
            crop_bgr = cv2.resize(crop_bgr, (self.swapper.size, self.swapper.size))
        return self.swapper.run(crop_bgr, self.source_latent)

    def process(self, frame: np.ndarray) -> SwapResult:
        if self.source_latent is None:
            raise RuntimeError("source identity not set")
        t = {}
        t0 = time.perf_counter()
        face = self.tracker.update(frame)
        t["detect"] = (time.perf_counter() - t0) * 1000
        if face is None:
            return SwapResult(frame, False, t)
        t1 = time.perf_counter()
        crop, M = alignment.align(frame, face.landmarks, self.swapper.size)
        t["align"] = (time.perf_counter() - t1) * 1000
        t2 = time.perf_counter()
        y = self.swapper.run(crop, self.source_latent)
        t["swap"] = (time.perf_counter() - t2) * 1000
        t3 = time.perf_counter()
        out_face = Swapper.to_bgr(y)
        mask = alignment.crop_mask(self.swapper.size)
        if self.color_match:
            out_face = alignment.color_transfer(out_face, crop, mask)
        out = alignment.paste_back(frame.copy(), out_face, M, mask)
        t["blend"] = (time.perf_counter() - t3) * 1000
        return SwapResult(out, True, t)


def build_pipeline(candidate: ProviderCandidate, models: dict[str, Path], threads: int,
                   detection_every_n_frames: int = 6, detector_size: int = 640, options: dict | None = None):
    """`models` maps manifest ids (yunet_face_detector, arcface_recognition, inswapper_compatible_swapper,
    or dfm_face_model) → path. A DFM model selects the DeepFaceLive-style pipeline."""
    if "dfm_face_model" in models:
        from avatar_engine.engines.dfm_pipeline import build_dfm_pipeline

        return build_dfm_pipeline(candidate, models, threads, detection_every_n_frames, detector_size, options)
    detector = FaceDetector(models.get("yunet_face_detector"), input_size=detector_size)
    if detector.backend != "yunet":
        raise ModelError("the face-swap engines need the YuNet detector model (landmarks for alignment)")
    rec = Recognizer(make_session(models["arcface_recognition"], candidate, threads))
    sw = Swapper(make_session(models["inswapper_compatible_swapper"], candidate, threads),
                 models["inswapper_compatible_swapper"])
    return SwapPipeline(detector, rec, sw, detection_every_n_frames)
