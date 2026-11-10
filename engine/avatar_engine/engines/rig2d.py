"""2D/2.5D photo rig for the Lite CPU Audio engine (spec §5 E1).

Preparation (once per profile/output size): roll correction, auto-framing into
the 16:9 canvas, optional offline background segmentation (GrabCut), and
precomputed regions for mouth, eyes and head.

Per frame (cheap, CPU): small remaps of the mouth and eye regions (jaw drop,
lip rounding/spreading, smile, eyelid curtain, gaze), mouth-interior fill, then
one weighted affine remap of the head region for micro head motion. The body
and background stay still. No neural inference runs per frame.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from avatar_engine.imaging import hex_to_bgr


@dataclass
class RigParams:
    jaw: float = 0.0  # 0..1
    round: float = 0.0
    spread: float = 0.0
    smile: float = 0.0
    blink_l: float = 0.0  # image-left eye closure 0..1
    blink_r: float = 0.0
    gaze_x: float = 0.0  # -1..1
    gaze_y: float = 0.0
    head_roll_deg: float = 0.0
    head_tx: float = 0.0  # in units of inter-ocular distance
    head_ty: float = 0.0
    head_scale: float = 0.0  # relative, e.g. 0.01

    def clamp(self) -> "RigParams":
        c = np.clip
        return RigParams(float(c(self.jaw, 0, 1)), float(c(self.round, 0, 1)), float(c(self.spread, 0, 1)),
                         float(c(self.smile, 0, 1)), float(c(self.blink_l, 0, 1)), float(c(self.blink_r, 0, 1)),
                         float(c(self.gaze_x, -1, 1)), float(c(self.gaze_y, -1, 1)),
                         float(c(self.head_roll_deg, -4, 4)), float(c(self.head_tx, -0.12, 0.12)),
                         float(c(self.head_ty, -0.12, 0.12)), float(c(self.head_scale, -0.03, 0.03)))


def _roi(cx, cy, hw, hh, W, H):
    x0, y0 = int(max(0, np.floor(cx - hw))), int(max(0, np.floor(cy - hh)))
    x1, y1 = int(min(W, np.ceil(cx + hw))), int(min(H, np.ceil(cy + hh)))
    return x0, y0, x1, y1


class PhotoRig:
    def __init__(self, image_bgr: np.ndarray, landmarks: np.ndarray, bbox: np.ndarray, width: int, height: int,
                 zoom: float = 1.0, offset_x: float = 0.0, offset_y: float = 0.0, background: str = "photo",
                 background_color: str = "#202020") -> None:
        self.W, self.H = width, height
        lm = landmarks.astype(np.float32).copy()
        eye_mid = (lm[0] + lm[1]) / 2
        roll = np.degrees(np.arctan2(lm[1][1] - lm[0][1], lm[1][0] - lm[0][0]))
        R = cv2.getRotationMatrix2D((float(eye_mid[0]), float(eye_mid[1])), float(roll), 1.0)
        d_src = float(np.linalg.norm(lm[1] - lm[0]))
        face_h_src = max(float(bbox[3]), 2.2 * d_src)
        face_c_src = eye_mid + np.array([0, 0.45 * d_src], np.float32)
        # Auto-framing: face ≈ 45% of output height, centred a bit above middle.
        ih, iw = image_bgr.shape[:2]
        scale = 0.45 * height / face_h_src * max(zoom, 0.25)
        cover = max(width / iw, height / ih)
        bg = background
        if bg == "photo":
            scale = max(scale, cover)
        tx = width / 2 + offset_x * width / 2 - face_c_src[0] * scale
        ty = 0.47 * height + offset_y * height / 2 - face_c_src[1] * scale
        S = np.array([[scale, 0, 0], [0, scale, 0], [0, 0, 1]], np.float32)
        Rm = np.vstack([R, [0, 0, 1]]).astype(np.float32)
        M = (S @ Rm)[:2]
        M[0, 2] += tx
        M[1, 2] += ty
        self.M = M
        color = hex_to_bgr(background_color)
        border = cv2.BORDER_REPLICATE if bg == "photo" else cv2.BORDER_CONSTANT
        canvas = cv2.warpAffine(image_bgr, M, (width, height), flags=cv2.INTER_LINEAR, borderMode=border,
                                borderValue=color)
        alpha = np.full((height, width), 255, np.uint8)
        if bg in ("solid", "transparent"):
            fg = self._segment(image_bgr, bbox)
            fg = cv2.warpAffine(fg, M, (width, height), flags=cv2.INTER_LINEAR, borderValue=0)
            a = fg.astype(np.float32)[..., None] / 255.0
            if bg == "solid":
                canvas = (canvas * a + np.array(color, np.float32) * (1 - a)).astype(np.uint8)
            else:
                alpha = fg
        self.base = canvas
        self.alpha = alpha
        self._opaque = bool(alpha.min() == 255)
        pts = np.hstack([lm, np.ones((5, 1), np.float32)]) @ M.T
        self.lm = pts.astype(np.float32)
        self.d = float(np.linalg.norm(self.lm[1] - self.lm[0]))
        self._prepare_regions()

    # ------------------------------------------------------------------ preparation
    @staticmethod
    def _segment(img: np.ndarray, bbox: np.ndarray) -> np.ndarray:
        """Offline head-and-shoulders segmentation (GrabCut), feathered; 0..255 mask."""
        ih, iw = img.shape[:2]
        x, y, w, h = bbox
        rect = (int(max(1, x - 0.9 * w)), int(max(1, y - 0.6 * h)), 0, 0)
        rect = (rect[0], rect[1], int(min(iw - rect[0] - 1, 2.8 * w)), int(min(ih - rect[1] - 1, ih)))
        mask = np.zeros((ih, iw), np.uint8)
        try:
            s = min(1.0, 512 / max(ih, iw))
            small = cv2.resize(img, None, fx=s, fy=s) if s < 1 else img
            m = np.zeros(small.shape[:2], np.uint8)
            r = tuple(int(v * s) for v in rect)
            bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
            cv2.grabCut(small, m, r, bgd, fgd, 4, cv2.GC_INIT_WITH_RECT)
            m = np.where((m == cv2.GC_FGD) | (m == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
            mask = cv2.resize(m, (iw, ih), interpolation=cv2.INTER_LINEAR) if s < 1 else m
        except cv2.error:
            cv2.ellipse(mask, (int(x + w / 2), int(y + h * 0.6)), (int(w * 0.75), int(h * 1.1)), 0, 0, 360, 255, -1)
        k = max(3, int(min(ih, iw) * 0.01) | 1)
        return cv2.GaussianBlur(mask, (k, k), 0)

    def _prepare_regions(self) -> None:
        d, W, H = self.d, self.W, self.H
        lm = self.lm
        self.mouth_l, self.mouth_r = lm[3], lm[4]
        mc = (lm[3] + lm[4]) / 2
        self.mc = mc
        self.mw = max(float(np.linalg.norm(lm[4] - lm[3])), 0.6 * d)
        # Mouth/jaw region: from just below the nose to below the chin.
        self.mouth_roi = _roi(mc[0], mc[1] + 0.35 * self.mw, 1.05 * self.mw, 1.25 * self.mw, W, H)
        x0, y0, x1, y1 = self.mouth_roi
        ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        self.m_xs, self.m_ys = xs, ys
        t = (xs - self.mouth_l[0]) / max(self.mouth_r[0] - self.mouth_l[0], 1)
        self.m_line = self.mouth_l[1] + t * (self.mouth_r[1] - self.mouth_l[1])  # lip line per column
        u = (xs - mc[0]) / (0.5 * self.mw)
        self.m_lip = np.sqrt(np.clip(1 - u * u, 0, 1))  # elliptical lip profile, 0 beyond corners
        self.m_jaw = np.exp(-((xs - mc[0]) / (0.95 * self.mw)) ** 2)
        nose_y = float(lm[2][1])
        chin_y = mc[1] + 0.95 * self.mw
        ramp_up = np.clip((ys - nose_y) / max(mc[1] - nose_y, 1), 0, 1)
        ramp_up = ramp_up * ramp_up * (3 - 2 * ramp_up)
        ramp_down = np.clip(1 - (ys - chin_y) / (0.45 * self.mw), 0, 1)
        self.m_vert = np.where(ys < mc[1], ramp_up * 0.35, ramp_down)  # jaw follows below the lip line
        self.m_below = (ys >= self.m_line).astype(np.float32) * ramp_down
        g = np.exp(-(((xs - mc[0]) / (0.75 * self.mw)) ** 2 + ((ys - mc[1]) / (0.45 * self.mw)) ** 2))
        self.m_local = g.astype(np.float32)
        self.m_corner = sum(np.exp(-(((xs - c[0]) / (0.22 * self.mw)) ** 2 + ((ys - c[1]) / (0.2 * self.mw)) ** 2))
                            for c in (self.mouth_l, self.mouth_r)).astype(np.float32)
        lip_sample = self.base[int(np.clip(mc[1] + 0.12 * self.mw, 0, H - 1)),
                               int(np.clip(mc[0], 0, W - 1))].astype(np.float32)
        self.interior = np.clip(np.array([30, 22, 48], np.float32) * 0.8 + lip_sample * 0.12, 0, 255)
        self.teeth = np.array([205, 208, 215], np.float32)
        # Eyes
        self.eyes = []
        for e in (lm[0], lm[1]):
            roi = _roi(e[0], e[1] - 0.05 * d, 0.36 * d, 0.34 * d, W, H)
            ex0, ey0, ex1, ey1 = roi
            ys_e, xs_e = np.mgrid[ey0:ey1, ex0:ex1].astype(np.float32)
            u = (xs_e - e[0]) / (0.27 * d)
            prof = np.sqrt(np.clip(1 - u * u, 0, 1)).astype(np.float32)
            iris = np.exp(-(((xs_e - e[0]) / (0.11 * d)) ** 2 + ((ys_e - e[1]) / (0.08 * d)) ** 2)).astype(np.float32)
            self.eyes.append({"roi": roi, "xs": xs_e, "ys": ys_e, "prof": prof, "iris": iris, "c": e.copy()})
        # Head region with feathered weight (1 on the head, 0 on shoulders/background)
        fc = (lm[0] + lm[1]) / 2 + np.array([0, 0.55 * d], np.float32)
        self.head_c = fc
        self.pivot = np.array([mc[0], mc[1] + 1.4 * self.mw], np.float32)  # neck
        self.head_roi = _roi(fc[0], fc[1], 1.75 * d, 2.1 * d, W, H)
        hx0, hy0, hx1, hy1 = self.head_roi
        wmask = np.zeros((hy1 - hy0, hx1 - hx0), np.float32)
        cv2.ellipse(wmask, (int(fc[0] - hx0), int(fc[1] - hy0 - 0.1 * d)), (int(1.15 * d), int(1.55 * d)), 0, 0, 360,
                    1.0, -1)
        k = max(3, int(0.9 * d) | 1)
        full_w = cv2.GaussianBlur(wmask, (k, k), 0)
        # The head displacement field is smooth: evaluate it at half resolution and upsample.
        self.h_size = (hx1 - hx0, hy1 - hy0)
        sw, sh = max(2, self.h_size[0] // 2), max(2, self.h_size[1] // 2)
        gx = np.linspace(hx0, hx1 - 1, sw, dtype=np.float32)
        gy = np.linspace(hy0, hy1 - 1, sh, dtype=np.float32)
        self.h_xs, self.h_ys = np.meshgrid(gx, gy)
        self.h_w = cv2.resize(full_w, (sw, sh), interpolation=cv2.INTER_AREA)
        self.h_wx, self.h_wy = self.h_w * self.h_xs, self.h_w * self.h_ys

    # ------------------------------------------------------------------ per frame
    def render(self, p: RigParams) -> np.ndarray:
        p = p.clamp()
        img = self.base.copy()
        self._mouth(img, p)
        for i, e in enumerate(self.eyes):
            self._eye(img, e, p.blink_l if i == 0 else p.blink_r, p.gaze_x, p.gaze_y)
        img = self._head(img, p)
        out = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        if not self._opaque:
            out[..., 3] = self.alpha
        return out

    def _mouth(self, img: np.ndarray, p: RigParams) -> None:
        if p.jaw < 0.01 and p.round < 0.01 and p.spread < 0.01 and p.smile < 0.01:
            return
        x0, y0, x1, y1 = self.mouth_roi
        xs, ys, mc, mw = self.m_xs, self.m_ys, self.mc, self.mw
        a = p.jaw * 0.42 * mw
        jaw_dy = a * 0.45 * self.m_jaw * self.m_vert
        lip_dy = a * 0.55 * self.m_lip * self.m_below
        k = (p.round * 0.22 - p.spread * 0.12) * self.m_local
        map_x = xs + (xs - mc[0]) * k
        map_y = ys - jaw_dy - lip_dy + p.smile * 0.12 * mw * self.m_corner
        map_x -= p.smile * 0.06 * mw * np.sign(xs - mc[0]) * self.m_corner
        src = self.base
        region = cv2.remap(src, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        # Mouth interior: between the (jaw-shifted) lip line and the displaced lower lip.
        open_h = a * 0.55 * self.m_lip
        top = self.m_line + a * 0.45 * self.m_jaw * 0.35
        lens_w = 1 - (p.round * 0.22 - p.spread * 0.12)
        u = (xs - mc[0]) / (0.5 * mw * lens_w)
        lens = np.sqrt(np.clip(1 - u * u, 0, 1))
        # Only rows that can contain the opening are blended.
        r0 = max(0, int(np.floor(top.min() - y0)) - 1)
        r1 = min(ys.shape[0], int(np.ceil((top + open_h).max() - y0)) + 2)
        if a > 0.5 and r1 > r0:
            sl = slice(r0, r1)
            ys_b, top_b, u_b = ys[sl], top[sl], u[sl]
            open_b = open_h[sl] * lens[sl]
            gap = np.clip(np.minimum(ys_b - top_b, top_b + open_b - ys_b) / 1.5, 0, 1)
            depth = np.clip((ys_b - top_b) / np.maximum(open_h[sl], 1), 0, 1)
            shade = 0.65 + 0.35 * np.abs(depth - 0.5) * 2
            color = self.interior[None, None, :] * shade[..., None]
            if p.jaw > 0.25:
                teeth_band = np.clip(1 - (ys_b - top_b) / (0.07 * mw + 1), 0, 1) * (np.abs(u_b) < 0.75)
                color = color * (1 - teeth_band[..., None] * 0.85) + self.teeth * (teeth_band[..., None] * 0.85)
            gm = gap[..., None]
            region[sl] = (region[sl] * (1 - gm) + color * gm).astype(np.uint8)
        img[y0:y1, x0:x1] = region

    def _eye(self, img: np.ndarray, e: dict, closure: float, gx: float, gy: float) -> None:
        if closure < 0.02 and abs(gx) < 0.02 and abs(gy) < 0.02:
            return
        x0, y0, x1, y1 = e["roi"]
        xs, ys, prof, iris, c = e["xs"], e["ys"], e["prof"], e["iris"], e["c"]
        d = self.d
        ho = 0.12 * d
        top = c[1] - 2.3 * ho
        lid0 = c[1] - ho
        cx = closure * prof
        lid = lid0 + cx * 2.0 * ho
        map_x = xs - (gx * 0.05 * d) * iris
        map_y = ys - (gy * 0.03 * d) * iris
        above = (ys < lid) & (ys >= top)
        stretch = np.where(above, top + (ys - top) * (lid0 - top) / np.maximum(lid - top, 1e-3), map_y)
        map_y = np.where(above, stretch, map_y).astype(np.float32)
        map_x = np.where(above, xs, map_x).astype(np.float32)
        region = cv2.remap(self.base, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        lash = np.exp(-((ys - lid) / (0.025 * d + 0.5)) ** 2) * prof * min(1.0, closure * 2) * 0.55
        region = (region * (1 - lash[..., None])).astype(np.uint8)
        img[y0:y1, x0:x1] = region

    def _head(self, img: np.ndarray, p: RigParams) -> np.ndarray:
        if abs(p.head_roll_deg) < 0.01 and abs(p.head_tx) < 1e-4 and abs(p.head_ty) < 1e-4 and abs(p.head_scale) < 1e-4:
            return img
        x0, y0, x1, y1 = self.head_roi
        A = cv2.getRotationMatrix2D((float(self.pivot[0]), float(self.pivot[1])), p.head_roll_deg, 1 + p.head_scale)
        A[0, 2] += p.head_tx * self.d
        A[1, 2] += p.head_ty * self.d
        Ai = cv2.invertAffineTransform(A).astype(np.float32)
        # map = p + w·(A⁻¹p − p), expanded so only precomputed w·x, w·y, w terms are needed
        wx, wy, w = self.h_wx, self.h_wy, self.h_w
        map_x = self.h_xs + (Ai[0, 0] - 1) * wx + Ai[0, 1] * wy + Ai[0, 2] * w
        map_y = self.h_ys + Ai[1, 0] * wx + (Ai[1, 1] - 1) * wy + Ai[1, 2] * w
        map_x = cv2.resize(map_x, self.h_size, interpolation=cv2.INTER_LINEAR)
        map_y = cv2.resize(map_y, self.h_size, interpolation=cv2.INTER_LINEAR)
        img[y0:y1, x0:x1] = cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        return img
