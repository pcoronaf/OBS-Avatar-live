"""Local avatar profiles: photo validation, face analysis, consent, deletion.

Layout: <data>/profiles/<profile_id>/{profile.json, source.png, face.png}
Nothing is uploaded; directories are restricted to the current user.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from avatar_engine import paths
from avatar_engine.imaging import laplacian_sharpness
from avatar_engine.privacy.consent import ConsentError, make_consent
from avatar_engine.vision.face_detect import FaceDetector

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MAX_PHOTO_BYTES = 30 * 1024 * 1024
MAX_SIDE = 2048
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProfileError(ValueError):
    pass


@dataclass
class Profile:
    profile_id: str
    display_name: str
    dir: Path
    meta: dict

    @property
    def image_path(self) -> Path:
        return self.dir / "source.png"

    def load_image(self) -> np.ndarray:
        img = cv2.imread(str(self.image_path), cv2.IMREAD_COLOR)
        if img is None:
            raise ProfileError("profile image is missing or corrupt")
        return img

    @property
    def landmarks(self) -> np.ndarray:
        return np.array(self.meta["face"]["landmarks"], np.float32)

    @property
    def bbox(self) -> np.ndarray:
        return np.array(self.meta["face"]["bbox"], np.float32)


def _restrict_to_user(path: Path) -> None:
    try:
        if sys.platform == "win32":
            user = os.environ.get("USERNAME")
            if user:
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F",
                                "/grant:r", "SYSTEM:(OI)(CI)F"], check=False, capture_output=True,
                               creationflags=0x08000000)
        else:
            os.chmod(path, 0o700)
    except OSError:
        pass


def _store_root(root: Path | None) -> Path:
    r = root or paths.profiles_dir()
    r.mkdir(parents=True, exist_ok=True)
    return r


def analyse_photo(img: np.ndarray, detector: FaceDetector | None = None) -> tuple[dict, list[str]]:
    """Detect the face and check sharpness / alignment. Raises ProfileError if unusable."""
    detector = detector or FaceDetector(input_size=640)
    faces = detector.detect(img, max_faces=3)
    if not faces:
        raise ProfileError("no face detected; use a frontal, well-lit photo")
    warnings: list[str] = []
    if len(faces) > 1:
        warnings.append("several faces detected; the largest one is used")
    face = faces[0]
    x, y, w, h = face.bbox
    H, W = img.shape[:2]
    if w < 96 or h < 96:
        raise ProfileError(f"face too small ({int(w)}x{int(h)} px); at least 96 px is required")
    x0, y0 = max(0, int(x)), max(0, int(y))
    crop = cv2.cvtColor(img[y0:int(y + h), x0:int(x + w)], cv2.COLOR_BGR2GRAY)
    sharp = laplacian_sharpness(cv2.resize(crop, (256, 256)))
    if sharp < 15:
        raise ProfileError(f"photo too blurry (sharpness {sharp:.1f})")
    if sharp < 60:
        warnings.append(f"photo is soft (sharpness {sharp:.1f}); results may look blurry")
    roll = face.roll_deg
    if abs(roll) > 25:
        raise ProfileError(f"head is tilted {roll:.0f}°; use a frontal photo")
    if abs(roll) > 10:
        warnings.append(f"head tilt {roll:.0f}° will be corrected")
    if face.estimated_landmarks:
        warnings.append("landmarks estimated (YuNet model not installed); mouth/eye placement may be approximate")
    margin_ok = x > 0.05 * W and y > 0.03 * H and x + w < 0.95 * W and y + h < 0.97 * H
    if not margin_ok:
        warnings.append("face is close to the image border; framing may be limited")
    info = {
        "bbox": [float(v) for v in face.bbox],
        "landmarks": face.landmarks.astype(float).tolist(),
        "score": face.score,
        "estimated_landmarks": face.estimated_landmarks,
        "detector": detector.backend,
        "sharpness": round(sharp, 2),
        "roll_deg": round(roll, 2),
    }
    return info, warnings


def create_profile(photo_path: str | Path, display_name: str, consent: dict, root: Path | None = None,
                   detector: FaceDetector | None = None) -> tuple[Profile, list[str]]:
    record = make_consent(consent)  # raises ConsentError
    photo_path = Path(photo_path)
    if photo_path.suffix.lower() not in ALLOWED_EXT:
        raise ProfileError("photo must be PNG, JPEG or WebP")
    if not photo_path.is_file():
        raise ProfileError("photo not found")
    if photo_path.stat().st_size > MAX_PHOTO_BYTES:
        raise ProfileError("photo larger than 30 MB")
    raw = photo_path.read_bytes()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ProfileError("could not decode photo")
    scale = min(1.0, MAX_SIDE / max(img.shape[:2]))
    if scale < 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    face, warnings = analyse_photo(img, detector)
    profile_id = "p-" + uuid.uuid4().hex[:16]
    pdir = _store_root(root) / profile_id
    pdir.mkdir(parents=True)
    _restrict_to_user(pdir)
    cv2.imwrite(str(pdir / "source.png"), img)  # re-encoded: strips EXIF/GPS metadata
    meta = {
        "profile_id": profile_id,
        "display_name": (display_name or "Avatar")[:80],
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "photo_sha256": hashlib.sha256(raw).hexdigest(),
        "image_size": [int(img.shape[1]), int(img.shape[0])],
        "face": face,
        "consent": record.to_dict(),
        "schema": 1,
    }
    (pdir / "profile.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return Profile(profile_id, meta["display_name"], pdir, meta), warnings


def load_profile(profile_id: str, root: Path | None = None) -> Profile:
    if not PROFILE_ID_RE.match(profile_id or ""):
        raise ProfileError("invalid profile id")
    pdir = _store_root(root) / profile_id
    meta_path = pdir / "profile.json"
    if not meta_path.exists():
        raise ProfileError(f"profile {profile_id} not found")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    consent = meta.get("consent") or {}
    if consent.get("rights_confirmed") is not True:
        raise ConsentError("profile has no valid consent record")
    return Profile(profile_id, meta.get("display_name", profile_id), pdir, meta)


def list_profiles(root: Path | None = None) -> list[dict]:
    out = []
    for d in sorted(_store_root(root).iterdir()):
        mp = d / "profile.json"
        if d.is_dir() and mp.exists():
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
                out.append({"profile_id": m["profile_id"], "display_name": m.get("display_name"),
                            "created_at": m.get("created_at")})
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def delete_profile(profile_id: str, root: Path | None = None) -> None:
    if not PROFILE_ID_RE.match(profile_id or ""):
        raise ProfileError("invalid profile id")
    pdir = _store_root(root) / profile_id
    if not pdir.exists():
        raise ProfileError(f"profile {profile_id} not found")
    # Overwrite image files before removal (best effort; SSDs may retain copies).
    for f in pdir.glob("*"):
        if f.is_file():
            try:
                f.write_bytes(b"\0" * min(f.stat().st_size, 64 * 1024 * 1024))
            except OSError:
                pass
    shutil.rmtree(pdir, ignore_errors=False)
    # Derived caches keyed by this profile (e.g. identity embeddings)
    for cache in paths.data_dir().glob(f"cache/{profile_id}*"):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
        else:
            cache.unlink(missing_ok=True)


__all__ = ["Profile", "ProfileError", "ConsentError", "create_profile", "load_profile", "list_profiles",
           "delete_profile", "analyse_photo"]
