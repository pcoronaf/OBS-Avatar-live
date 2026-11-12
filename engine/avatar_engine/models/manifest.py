"""Model manifest (models/MODEL_MANIFEST.json): origin, checksum, license per artifact.

Rules enforced at load time (spec §9):
* a model file must be listed in the manifest;
* if the manifest pins a sha256 the file must match it;
* if sha256 is null (not audited) the file is usable only after the user has
  acknowledged the license in `<models_dir>/ACKNOWLEDGED.json`, and a warning
  is reported in metrics/state.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from avatar_engine import paths


class ModelError(RuntimeError):
    pass


def _candidate_manifest_paths() -> list[Path]:
    here = Path(__file__).resolve()
    cands = [paths.models_dir() / "MODEL_MANIFEST.json"]
    if getattr(sys, "frozen", False):  # PyInstaller one-folder layout (datas live in _internal/ since v6)
        cands.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "models" / "MODEL_MANIFEST.json")
        cands.append(Path(sys.executable).parent / "models" / "MODEL_MANIFEST.json")
    cands.append(here.parents[3] / "models" / "MODEL_MANIFEST.json")  # source tree
    return cands


def bundled_dirs() -> list[Path]:
    """Folders holding models shipped with the engine (only redistributable ones, e.g. YuNet/MIT)."""
    here = Path(__file__).resolve()
    out = []
    if getattr(sys, "frozen", False):
        out.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "models" / "bundled")
        out.append(Path(sys.executable).parent / "models" / "bundled")
    out.append(here.parents[3] / "models" / "bundled")  # source tree
    return out


def find_model_file(filename: str, models_dir: Path | None = None, aliases: list[str] | None = None) -> Path | None:
    """User models directory first (lets users override), then the bundled copy. `aliases` are the
    original upstream file names (e.g. inswapper_128.onnx), accepted so users need not rename files."""
    for d in [models_dir or paths.models_dir(), *bundled_dirs()]:
        for name in [filename, *(aliases or [])]:
            p = d / name
            if p.is_file():
                return p
    return None


def entry_path(entry: dict, models_dir: Path | None = None) -> Path | None:
    if not entry.get("file"):
        return None
    return find_model_file(entry["file"], models_dir, entry.get("file_aliases"))


def acknowledge_present(models_dir: Path | None = None, manifest: dict | None = None) -> dict:
    """Acknowledge every present, unaudited model (the user confirmed the licenses in the UI)."""
    manifest = manifest or load_manifest()
    d = models_dir or paths.models_dir()
    done, missing = [], []
    for e in manifest.get("models", []):
        if not e.get("file") or e.get("sha256"):
            continue  # nothing to acknowledge (no file, or audited/pinned)
        p = entry_path(e, d)
        if p is None:
            missing.append({"id": e["id"], "file": e["file"], "aliases": e.get("file_aliases", []),
                            "hint": e.get("user_hint")})
            continue
        acknowledge(e["id"], sha256_file(p), d)
        done.append({"id": e["id"], "file": p.name, "license": e.get("license"),
                     "commercial_use": e.get("commercial_use")})
    return {"acknowledged": done, "missing": missing}


def load_manifest(path: Path | None = None) -> dict:
    for p in ([path] if path else _candidate_manifest_paths()):
        if p and p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise ModelError("MODEL_MANIFEST.json not found")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ResolvedModel:
    id: str
    path: Path
    sha256: str
    audited: bool
    license: str
    commercial_use: str
    warning: str | None


def acknowledgements(models_dir: Path) -> dict:
    p = models_dir / "ACKNOWLEDGED.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def acknowledge(model_id: str, sha256: str, models_dir: Path | None = None) -> None:
    d = models_dir or paths.models_dir()
    acks = acknowledgements(d)
    acks[model_id] = {"sha256": sha256}
    (d / "ACKNOWLEDGED.json").write_text(json.dumps(acks, indent=2), encoding="utf-8")


def resolve(model_id: str, manifest: dict | None = None, models_dir: Path | None = None) -> ResolvedModel:
    manifest = manifest or load_manifest()
    models_dir = models_dir or paths.models_dir()
    entry = next((m for m in manifest.get("models", []) if m.get("id") == model_id), None)
    if entry is None or not entry.get("file"):
        raise ModelError(f"model {model_id!r} is not declared in MODEL_MANIFEST.json")
    path = entry_path(entry, models_dir) or models_dir / entry["file"]
    if not path.exists():
        hint = entry.get("user_hint") or "see docs/INSTALL_WINDOWS.md; weights are not bundled"
        raise ModelError(f"model file missing: {path}. {hint}")
    digest = sha256_file(path)
    pinned = entry.get("sha256")
    if pinned:
        if digest.lower() != pinned.lower():
            raise ModelError(f"checksum mismatch for {path.name}: expected {pinned}, got {digest}")
        return ResolvedModel(model_id, path, digest, True, entry.get("license", ""), entry.get("commercial_use",
                                                                                                "unknown"), None)
    ack = acknowledgements(models_dir).get(model_id)
    if not ack or ack.get("sha256") != digest:
        raise ModelError(
            f"model {model_id!r} has not been audited (sha256 null in manifest) and its license has not been "
            f"acknowledged for this file. Press 'Confirm model licenses' in the Avatar Live source "
            f"(or run: avatar-engine models acknowledge {model_id})")
    return ResolvedModel(model_id, path, digest, False, entry.get("license", ""),
                         entry.get("commercial_use", "unknown"),
                         f"{model_id}: unaudited model used under user acknowledgement ({entry.get('license')})")
