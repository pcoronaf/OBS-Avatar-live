"""Startup benchmark per execution-provider candidate (spec §5 E2a).

For each candidate: build the pipeline (sessions + warm-up), run the full
per-frame pipeline on representative frames for a bounded time, and record
unique FPS, P50/P95 per stage and end-to-end, CPU %, RAM, and a correctness
check of the swap output against the CPU reference. Results are cached per
hardware + driver + ORT version + model hashes + resolution and invalidated
when any of these change.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from avatar_engine import paths
from avatar_engine.metrics import ProcessStats, percentile
from avatar_engine.performance.provider_probe import ProviderCandidate, fault
from avatar_engine.performance.system_info import memory_mb, system_info


@dataclass
class BenchmarkResult:
    candidate: dict
    ok: bool
    error: str | None = None
    init_ms: float = 0.0
    frames: int = 0
    unique_fps: float = 0.0
    p50_ms: float | None = None
    p95_ms: float | None = None
    stage_p95_ms: dict = field(default_factory=dict)
    cpu_pct: float | None = None
    ram_delta_mb: float | None = None
    max_abs_diff_vs_cpu: float | None = None
    face_found_pct: float = 0.0
    timestamp: str = ""

    @property
    def label(self) -> str:
        return f"{self.candidate['key']}:{self.candidate['device']}"


def cache_key(models: dict[str, str], width: int, height: int, input_face_size: int, extra: dict | None = None) -> str:
    info = system_info()
    basis = {
        "cpu": info["cpu"], "cores": info["logical_cores"], "flags": info["cpu_flags"],
        "gpus": [(g.get("name"), g.get("driver")) for g in info["gpus"]],
        "ort": info["onnxruntime"], "models": models, "res": [width, height], "face": input_face_size,
        "extra": extra or {},
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:24]


def load_cached(key: str) -> list[BenchmarkResult] | None:
    p = paths.benchmarks_dir() / f"{key}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [BenchmarkResult(**r) for r in data["results"]]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def save_cached(key: str, results: list[BenchmarkResult]) -> Path:
    p = paths.benchmarks_dir() / f"{key}.json"
    p.write_text(json.dumps({"key": key, "system": system_info(), "results": [asdict(r) for r in results]},
                            indent=2), encoding="utf-8")
    return p


def benchmark_candidate(candidate: ProviderCandidate, build: Callable[[ProviderCandidate], object],
                        frames: list[np.ndarray], duration_s: float, reference: np.ndarray | None = None,
                        probe_crop: np.ndarray | None = None) -> tuple[BenchmarkResult, np.ndarray | None]:
    """`build(candidate)` returns a ready SwapPipeline with its source identity set."""
    stats = ProcessStats()
    ram0 = stats.ram_mb()
    res = BenchmarkResult(candidate.to_dict(), ok=False,
                          timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
    t0 = time.perf_counter()
    try:
        pipe = build(candidate)
    except Exception as exc:
        res.error = f"init: {exc}"
        return res, None
    res.init_ms = (time.perf_counter() - t0) * 1000
    out_probe = None
    try:
        for f in frames[:3]:  # warm-up (graph compilation, allocations)
            pipe.process(f)
        if probe_crop is not None:
            out_probe = pipe.swapper.run(probe_crop, pipe.source_latent)
            if not np.all(np.isfinite(out_probe)):
                raise RuntimeError("non-finite output")
            if reference is not None:
                res.max_abs_diff_vs_cpu = float(np.abs(out_probe - reference).mean())
        totals, stages, found = [], {}, 0
        stats.cpu_pct()
        end = time.perf_counter() + duration_s
        i = 0
        while time.perf_counter() < end or i < 5:
            f = frames[i % len(frames)]
            s = time.perf_counter()
            r = pipe.process(f)
            if fault("latency"):
                time.sleep(0.6)
            totals.append((time.perf_counter() - s) * 1000)
            for k, v in r.timings_ms.items():
                stages.setdefault(k, []).append(v)
            found += int(r.face_found)
            i += 1
        res.cpu_pct = round(stats.cpu_pct(), 1)
        res.frames = i
        mean = sum(totals) / len(totals)
        res.unique_fps = round(1000.0 / mean, 2) if mean > 0 else 0.0
        res.p50_ms = round(percentile(totals, 50), 2)
        res.p95_ms = round(percentile(totals, 95), 2)
        res.stage_p95_ms = {k: round(percentile(v, 95), 2) for k, v in stages.items()}
        res.face_found_pct = round(100.0 * found / i, 1)
        res.ram_delta_mb = round(stats.ram_mb() - ram0, 1)
        res.ok = True
    except Exception as exc:
        res.error = f"run: {exc}"
    return res, out_probe


def run_benchmark(candidates: list[ProviderCandidate], build: Callable[[ProviderCandidate], object],
                  frames: list[np.ndarray], duration_s: float = 8.0,
                  probe_crop: np.ndarray | None = None) -> list[BenchmarkResult]:
    """Benchmarks CPU first (reference), then the others; total time ≈ duration_s."""
    ordered = sorted(candidates, key=lambda c: c.key != "cpu")
    per = max(1.5, duration_s / max(1, len(ordered)))
    results, reference = [], None
    for c in ordered:
        r, out = benchmark_candidate(c, build, frames, per, reference, probe_crop)
        if c.key == "cpu" and out is not None:
            reference = out
        results.append(r)
    return results


def select(results: list[BenchmarkResult], max_latency_ms: float, min_unique_fps: float,
           user_profile: str = "auto", max_diff: float = 0.06, min_free_ram_mb: float = 800.0) -> tuple[BenchmarkResult | None, list[str]]:
    """Pick the *viable* candidate with the best balance (not just peak FPS). Returns (choice, reasons)."""
    reasons: list[str] = []
    _, avail = memory_mb()
    viable = []
    for r in results:
        why = None
        if not r.ok:
            why = r.error or "failed"
        elif r.max_abs_diff_vs_cpu is not None and r.max_abs_diff_vs_cpu > max_diff:
            why = f"output differs from CPU reference ({r.max_abs_diff_vs_cpu:.3f})"
        elif r.p95_ms is not None and r.p95_ms > max_latency_ms:
            why = f"P95 {r.p95_ms:.0f} ms > {max_latency_ms:.0f} ms"
        elif avail and avail < min_free_ram_mb and r.candidate["key"] != "cpu":
            why = f"low free memory ({avail:.0f} MB)"
        if why:
            reasons.append(f"{r.label}: {why}")
        else:
            viable.append(r)
    if not viable:
        cpu = next((r for r in results if r.candidate["key"] == "cpu" and r.ok), None)
        if cpu:
            reasons.append("no candidate met the limits; using CPU in degraded mode")
        return cpu, reasons

    cpu_ref = next((r for r in viable if r.candidate["key"] == "cpu"), None)

    def score(r: BenchmarkResult) -> float:
        fps = r.unique_fps
        cpu = (r.cpu_pct or 0.0) / 100.0
        lat = (r.p95_ms or max_latency_ms) / max_latency_ms
        meets = 1.0 if fps >= min_unique_fps else 0.0
        if user_profile == "max_performance":
            return meets * 1000 + fps
        if user_profile == "low_power":
            return meets * 1000 - cpu * 100 + fps * 0.1
        s = meets * 1000 + fps * (1 - 0.5 * cpu) / (1 + 0.5 * lat)
        if user_profile == "intel_optimized" and r.candidate["key"] == "openvino":
            s += 5
        return s

    best = max(viable, key=score)
    # Hysteresis vs CPU: an accelerator must beat CPU clearly to be worth the extra risk.
    if cpu_ref is not None and best is not cpu_ref and best.unique_fps < cpu_ref.unique_fps * 1.1 \
            and user_profile != "intel_optimized":
        reasons.append(f"{best.label} not clearly faster than CPU; keeping CPU")
        best = cpu_ref
    return best, reasons


def benchmark_path(key: str) -> Path:
    return paths.benchmarks_dir() / f"{key}.json"
