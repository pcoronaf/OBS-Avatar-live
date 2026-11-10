"""Small image helpers shared by engines (OpenCV-backed)."""

from __future__ import annotations

import cv2
import numpy as np


def hex_to_bgr(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    return b, g, r


def to_bgra(bgr: np.ndarray, alpha: np.ndarray | None = None) -> np.ndarray:
    out = np.empty(bgr.shape[:2] + (4,), np.uint8)
    out[..., :3] = bgr
    out[..., 3] = 255 if alpha is None else alpha
    return out


def fit_into(img: np.ndarray, width: int, height: int, zoom: float = 1.0, offset_x: float = 0.0,
             offset_y: float = 0.0, background: tuple[int, int, int] = (32, 32, 32)) -> np.ndarray:
    """Cover-fit `img` into width×height (16:9 framing) with zoom and pan in [-1, 1]."""
    h, w = img.shape[:2]
    scale = max(width / w, height / h) * max(zoom, 0.1)
    m = np.array([[scale, 0, 0], [0, scale, 0]], np.float32)
    m[0, 2] = (width - w * scale) / 2 + offset_x * width / 2
    m[1, 2] = (height - h * scale) / 2 + offset_y * height / 2
    return cv2.warpAffine(img, m, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=background)


def framing_matrix(src_w: int, src_h: int, width: int, height: int, zoom: float, offset_x: float,
                   offset_y: float) -> np.ndarray:
    scale = max(width / src_w, height / src_h) * max(zoom, 0.1)
    return np.array([[scale, 0, (width - src_w * scale) / 2 + offset_x * width / 2],
                     [0, scale, (height - src_h * scale) / 2 + offset_y * height / 2]], np.float32)


def laplacian_sharpness(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def feather_mask(h: int, w: int, feather: float = 0.15) -> np.ndarray:
    """Elliptical soft mask in [0,1] (float32, HxW)."""
    mask = np.zeros((h, w), np.float32)
    cv2.ellipse(mask, (w // 2, h // 2), (int(w * 0.42), int(h * 0.47)), 0, 0, 360, 1.0, -1)
    k = max(3, int(min(h, w) * feather) | 1)
    return cv2.GaussianBlur(mask, (k, k), 0)
