"""Visible synthetic-content disclosure (spec §8, ADR-006).

`Compositor.finalize()` always calls `DisclosureLabel.apply()`; there is no
configuration that disables it (``privacy.disclosure_overlay`` is ``const: true``
in the schema). Users may move it and scale it up for accessibility.
"""

from __future__ import annotations

import cv2
import numpy as np

DEFAULT_TEXT = "Avatar generado por IA"
MIN_SCALE = 1.0


def _ascii_fold(text: str) -> str:
    """OpenCV's Hershey fonts are ASCII-only: fold accents (á→a) and drop other symbols («»)."""
    import unicodedata

    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.split())


class DisclosureLabel:
    def __init__(self, text: str = DEFAULT_TEXT, position: str = "bottom_left", scale: float = 1.0) -> None:
        text = _ascii_fold((text or "").strip())
        # The label may be localised but never emptied or shortened to nothing meaningful.
        if len(text) < 8 or "IA" not in text.upper() and "AI" not in text.upper():
            text = DEFAULT_TEXT
        self.text = text
        self.position = position
        self.scale = max(MIN_SCALE, float(scale))
        self._cache: dict[tuple[int, int], tuple[np.ndarray, np.ndarray, int, int]] = {}

    def _patch(self, out_w: int, out_h: int):
        key = (out_w, out_h)
        if key in self._cache:
            return self._cache[key]
        base = max(out_h / 720.0, 0.35) * self.scale
        font_scale = 0.75 * base
        thick = max(1, int(round(2 * base)))
        (tw, th), baseline = cv2.getTextSize(self.text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thick)
        pad = max(4, int(10 * base))
        w, h = min(tw + 2 * pad, out_w), min(th + baseline + 2 * pad, out_h)
        patch = np.zeros((h, w, 3), np.uint8)
        patch[:] = (24, 24, 24)
        cv2.putText(patch, self.text, (pad, pad + th), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), thick,
                    cv2.LINE_AA)
        alpha = np.full((h, w, 1), 0.78, np.float32)
        margin = max(6, int(16 * base))
        x = margin if "left" in self.position else out_w - w - margin
        y = margin if "top" in self.position else out_h - h - margin
        x, y = max(0, x), max(0, y)
        self._cache[key] = (patch.astype(np.float32), alpha, x, y)
        return self._cache[key]

    def apply(self, bgra: np.ndarray) -> np.ndarray:
        h, w = bgra.shape[:2]
        patch, alpha, x, y = self._patch(w, h)
        ph, pw = patch.shape[:2]
        roi = bgra[y:y + ph, x:x + pw, :3].astype(np.float32)
        bgra[y:y + ph, x:x + pw, :3] = (patch * alpha + roi * (1 - alpha)).astype(np.uint8)
        bgra[y:y + ph, x:x + pw, 3] = 255
        return bgra


def status_frame(width: int, height: int, title: str, detail: str = "") -> np.ndarray:
    """Neutral, face-free frame shown when output is stale, paused or failed."""
    img = np.zeros((height, width, 4), np.uint8)
    img[..., :3] = (40, 34, 30)
    img[..., 3] = 255
    s = max(height / 720.0, 0.35)
    for i, (txt, fs) in enumerate(((title, 1.1 * s), (detail, 0.7 * s))):
        if not txt:
            continue
        th = max(1, int(2 * s))
        (tw, tht), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, fs, th)
        y = int(height / 2 + i * 50 * s)
        cv2.putText(img, txt, ((width - tw) // 2, y), cv2.FONT_HERSHEY_SIMPLEX, fs, (230, 230, 230, 255), th,
                    cv2.LINE_AA)
    return img
