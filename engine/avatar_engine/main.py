"""Command-line entry point: `avatar-engine <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from avatar_engine import __version__


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    else:
        if isinstance(obj, dict):
            for k, v in obj.items():
                print(f"{k}: {v}")
        else:
            print(obj)


def cmd_serve(a) -> int:
    from avatar_engine import paths
    from avatar_engine.server import EngineServer

    logging.basicConfig(filename=str(paths.logs_dir() / "engine.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.pop("AVATAR_LIVE_AUTH_TOKEN", "")
    logging.getLogger("avatar_engine").info("engine %s started (session %s)", __version__, a.session[:8])
    srv = EngineServer(a.session, token, a.parent_pid, a.max_width, a.max_height)
    srv.serve_forever()
    return 0


def cmd_probe(a) -> int:
    from avatar_engine.performance.provider_probe import available_ort_providers, discover
    from avatar_engine.performance.system_info import system_info

    out = {"system": system_info(), "ort_providers_installed": available_ort_providers(),
           "candidates": [c.to_dict() for c in discover(cpu_only=a.cpu_only)],
           "note": "installed providers still need a benchmark before use"}
    _print(out, a.json)
    return 0


def cmd_benchmark(a) -> int:
    from avatar_engine import config as cfgmod
    from avatar_engine.engines.base import EngineUnavailable
    from avatar_engine.engines.lite_adaptive import LiteAdaptiveEngine
    from avatar_engine.performance import startup_benchmark as sb
    from avatar_engine.privacy import profile_store

    cfg = cfgmod.load({"engine": "lite_adaptive", "drive_mode": "webcam", "profile_id": a.profile or "benchmark",
                       "inference": {"cpu_only": a.cpu_only, "benchmark_duration_seconds": a.seconds}})
    eng = LiteAdaptiveEngine()
    try:
        eng.initialize(cfg, {})
        if a.profile:
            prof = profile_store.load_profile(a.profile)
            img, lm = prof.load_image(), prof.landmarks
        else:  # no profile given: drawn synthetic face (inference cost does not depend on identity)
            from avatar_engine.selftest import synthetic_face

            img, lm, _ = synthetic_face()
        eng._profile_img, eng._profile_lm = img, lm
        from avatar_engine.engines.lite_adaptive import representative_frames

        frames = representative_frames(img)
        results = sb.run_benchmark(eng.candidates, eng.build, frames, a.seconds, eng._probe_crop(frames))
    except (EngineUnavailable, profile_store.ProfileError) as exc:
        print(f"benchmark unavailable: {exc}", file=sys.stderr)
        return 2
    best, reasons = sb.select(results, cfg["inference"]["max_end_to_end_latency_ms"],
                              cfg["inference"]["minimum_unique_swap_fps"])
    out = {"results": [r.__dict__ for r in results], "selected": best.label if best else None, "reasons": reasons,
           "note": "measured on this machine only; not a guarantee"}
    _print(out, a.json)
    return 0


def cmd_profile(a) -> int:
    from avatar_engine.privacy import profile_store
    from avatar_engine.privacy.consent import CONSENT_STATEMENT_ES, ConsentError

    try:
        if a.action == "create":
            if not a.i_have_rights:
                print("Se requiere --i-have-rights.\n" + CONSENT_STATEMENT_ES, file=sys.stderr)
                return 2
            consent = {"rights_confirmed": True, "subject_is_self": a.subject_is_self,
                       "third_party_consent_reference": a.consent_ref}
            prof, warnings = profile_store.create_profile(a.photo, a.name, consent)
            _print({"profile_id": prof.profile_id, "warnings": warnings}, a.json)
        elif a.action == "list":
            _print(profile_store.list_profiles(), True)
        elif a.action == "delete":
            profile_store.delete_profile(a.profile_id)
            print(f"deleted {a.profile_id}")
    except (profile_store.ProfileError, ConsentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_models(a) -> int:
    from avatar_engine import paths
    from avatar_engine.models import manifest

    man = manifest.load_manifest()
    if a.action == "list":
        rows = []
        for m in man["models"]:
            f = manifest.entry_path(m)
            rows.append({"id": m["id"], "file": f.name if f else m.get("file"), "present": bool(f),
                         "license": m.get("license"), "commercial_use": m.get("commercial_use"),
                         "audited": bool(m.get("sha256"))})
        _print(rows, True)
        return 0
    entry = next((m for m in man["models"] if m["id"] == a.model_id), None)
    if entry is None or not entry.get("file"):
        print("unknown model id", file=sys.stderr)
        return 2
    f = manifest.entry_path(entry)
    if f is None:
        names = " or ".join([entry["file"], *entry.get("file_aliases", [])])
        print(f"missing file {paths.models_dir() / entry['file']} (accepted names: {names})", file=sys.stderr)
        return 2
    print(f"Model: {a.model_id}\nLicense: {entry.get('license')}\nCommercial use: {entry.get('commercial_use')}\n"
          f"URL: {entry.get('license_url')}")
    if not a.yes:
        if input("Type 'acepto' to confirm you have the right to use this file under its license: ").strip().lower() \
                not in ("acepto", "i accept"):
            return 1
    manifest.acknowledge(a.model_id, manifest.sha256_file(f))
    print("acknowledged")
    return 0


def cmd_preview(a) -> int:
    """Render a session to a video file / PNGs (no OBS needed)."""
    import cv2

    from avatar_engine import config as cfgmod
    from avatar_engine.session import Session

    user = {"engine": a.engine, "profile_id": a.profile,
            "drive_mode": "audio" if a.engine == "lite_cpu_audio" else "webcam"}
    if a.audio:
        user["audio_input"] = "file:" + a.audio
    if a.video is not None:
        user["video_input"] = a.video if a.video.isdigit() else "file:" + a.video
    if a.cpu_only:
        user["user_profile"] = "cpu_only"
    if a.dfm:
        if not a.i_have_rights:
            print("--dfm requires --i-have-rights (model trained on your own face or with consent)", file=sys.stderr)
            return 2
        user.setdefault("inference", {}).update({"swap_model": "dfm", "dfm_model": os.path.abspath(a.dfm),
                                                 "dfm_rights_confirmed": True, "dfm_coverage": a.dfm_coverage,
                                                 "dfm_offset_y": a.dfm_offset_y})
    events = []
    s = Session(user, None, lambda n, p: events.append((n, p)))
    writer = None
    frames = []

    def on_frame(img, flags):
        nonlocal writer
        if a.out and a.out.endswith(".mp4"):
            if writer is None:
                writer = cv2.VideoWriter(a.out, cv2.VideoWriter_fourcc(*"mp4v"), s.config["output"]["fps"],
                                         (img.shape[1], img.shape[0]))
            writer.write(img[..., :3])
        elif len(frames) < 1:
            frames.append(img)

    s.start()
    s.scheduler.on_frame = on_frame
    time.sleep(a.seconds)
    m = s.metrics()
    s.stop()
    if writer is not None:
        writer.release()
    elif a.out and frames:
        cv2.imwrite(a.out, frames[-1][..., :3])
    _print(m, True)
    return 0


def cmd_devices(a) -> int:
    """What the engine sees: cameras (with per-backend diagnostics) and microphones."""
    from avatar_engine.audio.capture import list_input_devices
    from avatar_engine.vision.capture import list_video_devices

    report: list[str] = []
    out = {"video": list_video_devices(report=report), "video_diagnostics": report,
           "audio": list_input_devices()}
    _print(out, True)
    return 0


def cmd_selftest(a) -> int:
    from avatar_engine.selftest import run

    ok, report = run()
    _print(report, True)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="avatar-engine", description="OBS Avatar Live engine")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the engine server (launched by the OBS plugin)")
    s.add_argument("--session", required=True)
    s.add_argument("--parent-pid", type=int, default=None)
    s.add_argument("--max-width", type=int, default=1920)
    s.add_argument("--max-height", type=int, default=1080)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("probe", help="list hardware and installed ONNX Runtime providers")
    s.add_argument("--json", action="store_true")
    s.add_argument("--cpu-only", action="store_true")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("benchmark", help="benchmark provider candidates for the swap engines")
    s.add_argument("--profile", default=None, help="profile id (default: built-in synthetic face)")
    s.add_argument("--seconds", type=float, default=8.0)
    s.add_argument("--cpu-only", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_benchmark)

    s = sub.add_parser("profile", help="create/list/delete local avatar profiles")
    ps = s.add_subparsers(dest="action", required=True)
    c = ps.add_parser("create")
    c.add_argument("--photo", required=True)
    c.add_argument("--name", default="Avatar")
    c.add_argument("--i-have-rights", action="store_true", help="confirm the right to use the photo")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--subject-is-self", action="store_true")
    g.add_argument("--consent-ref", default=None, help="reference to the third party's documented consent")
    c.add_argument("--json", action="store_true")
    ps.add_parser("list")
    d = ps.add_parser("delete")
    d.add_argument("profile_id")
    s.set_defaults(fn=cmd_profile)

    s = sub.add_parser("models", help="list models / acknowledge an unaudited model's license")
    ms = s.add_subparsers(dest="action", required=True)
    ms.add_parser("list")
    ack = ms.add_parser("acknowledge")
    ack.add_argument("model_id")
    ack.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_models)

    s = sub.add_parser("preview", help="render a session to a file without OBS")
    s.add_argument("--engine", default="lite_cpu_audio")
    s.add_argument("--profile", required=True)
    s.add_argument("--audio", help="16-bit WAV file instead of the microphone")
    s.add_argument("--video", help="camera index or video file")
    s.add_argument("--seconds", type=float, default=5.0)
    s.add_argument("--out", help="output .mp4 or .png")
    s.add_argument("--cpu-only", action="store_true")
    s.add_argument("--dfm", help="DeepFaceLive/DeepFaceLab .dfm face model (webcam engines)")
    s.add_argument("--i-have-rights", action="store_true", help="confirm the DFM depicts you or a consenting person")
    s.add_argument("--dfm-coverage", type=float, default=1.0)
    s.add_argument("--dfm-offset-y", type=float, default=0.0)
    s.set_defaults(fn=cmd_preview)

    s = sub.add_parser("devices", help="list cameras and microphones as the engine sees them (diagnostics)")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("selftest", help="offline checks of config, IPC, frame ring and the audio rig")
    s.set_defaults(fn=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
