"""5-point similarity alignment and paste-back."""

from __future__ import annotations

import cv2
import numpy as np

# Canonical 5-point layout for 112×112 face-recognition crops (eyes, nose tip,
# mouth corners), the de-facto standard used by ArcFace-style models.
TEMPLATE_112 = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], np.float32)


def template(size: int) -> np.ndarray:
    """Template for a square crop of `size` px (112-multiples scale; 128 uses an 8 px x-margin convention)."""
    if size % 112 == 0:
        return TEMPLATE_112 * (size / 112.0)
    ratio = size / 128.0
    return (TEMPLATE_112 + np.array([8.0, 0.0], np.float32)) * ratio


def umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform (rotation, uniform scale, translation) src→dst, 2×3."""
    src = src.astype(np.float64)
    dst = dst.astype(np.float64)
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s, d = src - mu_s, dst - mu_d
    cov = d.T @ s / len(src)
    U, S, Vt = np.linalg.svd(cov)
    D = np.eye(2)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        D[1, 1] = -1
    R = U @ D @ Vt
    var_s = (s ** 2).sum() / len(src)
    scale = np.trace(np.diag(S) @ D) / var_s if var_s > 0 else 1.0
    t = mu_d - scale * R @ mu_s
    M = np.zeros((2, 3), np.float32)
    M[:, :2] = scale * R
    M[:, 2] = t
    return M


def align(img: np.ndarray, landmarks: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray]:
    M = umeyama_similarity(landmarks, template(size))
    crop = cv2.warpAffine(img, M, (size, size), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return crop, M


_MASK_CACHE: dict[int, np.ndarray] = {}


def crop_mask(size: int) -> np.ndarray:
    if size not in _MASK_CACHE:
        m = np.zeros((size, size), np.float32)
        # Face area excluding forehead edge/hair and the crop border.
        cv2.ellipse(m, (size // 2, int(size * 0.56)), (int(size * 0.36), int(size * 0.42)), 0, 0, 360, 1.0, -1)
        k = max(3, int(size * 0.12) | 1)
        _MASK_CACHE[size] = cv2.GaussianBlur(m, (k, k), 0)
    return _MASK_CACHE[size]


def color_transfer(src: np.ndarray, ref: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Match mean/std of `src` to `ref` inside mask (LAB), cheap tone matching."""
    m = mask > 0.5
    if m.sum() < 16:
        return src
    s = cv2.cvtColor(src, cv2.COLOR_BGR2LAB).astype(np.float32)
    r = cv2.cvtColor(ref, cv2.COLOR_BGR2LAB).astype(np.float32)
    for c in range(3):
        sm, ss = s[..., c][m].mean(), s[..., c][m].std() + 1e-3
        rm, rs = r[..., c][m].mean(), r[..., c][m].std() + 1e-3
        s[..., c] = (s[..., c] - sm) * (rs / ss) + rm
    return cv2.cvtColor(np.clip(s, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


def paste_back(frame: np.ndarray, face: np.ndarray, M: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    """Inverse-warp `face` (aligned crop) into `frame` with a soft mask; only the face bbox is touched."""
    size = face.shape[0]
    mask = crop_mask(size) if mask is None else mask
    Mi = cv2.invertAffineTransform(M)
    corners = np.array([[0, 0, 1], [size, 0, 1], [0, size, 1], [size, size, 1]], np.float32) @ Mi.T
    H, W = frame.shape[:2]
    x0, y0 = np.floor(corners.min(0)).astype(int)
    x1, y1 = np.ceil(corners.max(0)).astype(int)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0:
        return frame
    Mi_roi = Mi.copy()
    Mi_roi[:, 2] -= (x0, y0)
    w, h = x1 - x0, y1 - y0
    warped = cv2.warpAffine(face, Mi_roi, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    wm = cv2.warpAffine(mask, Mi_roi, (w, h), flags=cv2.INTER_LINEAR, borderValue=0)[..., None]
    roi = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = (warped.astype(np.float32) * wm + roi * (1 - wm)).astype(np.uint8)
    return frame
