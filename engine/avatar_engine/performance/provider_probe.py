"""Discover ONNX Runtime execution providers that are *installed*.

Availability ≠ usability: every candidate is still benchmarked and validated
against the CPU reference before it can be selected (spec §5 E2a).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from avatar_engine.performance.system_info import system_info

ORT_NAMES = {
    "cpu": "CPUExecutionProvider",
    "openvino": "OpenVINOExecutionProvider",
    "directml": "DmlExecutionProvider",
    "cuda": "CUDAExecutionProvider",
}


@dataclass
class ProviderCandidate:
    key: str  # cpu | openvino | directml | cuda
    ort_name: str
    device: str  # human-readable device the EP is asked to use
    options: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.key}:{self.device}"

    def providers(self) -> list:
        """ORT providers list; CPU always appended as the in-session fallback for unsupported nodes."""
        if self.key == "cpu":
            return ["CPUExecutionProvider"]
        return [(self.ort_name, self.options), "CPUExecutionProvider"]

    def to_dict(self) -> dict:
        return {"key": self.key, "ort_name": self.ort_name, "device": self.device, "options": self.options}


def available_ort_providers() -> list[str]:
    try:
        import onnxruntime as ort

        return list(ort.get_available_providers())
    except Exception:
        return ["CPUExecutionProvider"]


def discover(priority: list[str] | None = None, cpu_only: bool = False, allow_cuda: bool = False) -> list[ProviderCandidate]:
    """Candidates in priority order. `cpu_only` returns CPU alone and never touches GPU EPs."""
    cpu = ProviderCandidate("cpu", ORT_NAMES["cpu"], "CPU")
    if cpu_only:
        return [cpu]
    priority = priority or ["openvino", "directml", "cpu"]
    installed = set(available_ort_providers())
    info = system_info()
    gpus = info.get("gpus") or []
    intel_gpu = any("intel" in (g.get("name") or "").lower() for g in gpus)
    out: list[ProviderCandidate] = []
    for key in priority:
        if key == "cpu":
            out.append(cpu)
        elif key == "openvino" and ORT_NAMES["openvino"] in installed:
            if intel_gpu:
                out.append(ProviderCandidate("openvino", ORT_NAMES["openvino"], "GPU", {"device_type": "GPU"}))
            if "intel" in (info.get("cpu") or "").lower():
                out.append(ProviderCandidate("openvino", ORT_NAMES["openvino"], "CPU", {"device_type": "CPU"}))
        elif key == "directml" and ORT_NAMES["directml"] in installed:
            n = max(1, len(gpus))
            for i in range(min(n, 2)):
                name = (gpus[i].get("name") if i < len(gpus) else None) or f"adapter {i}"
                out.append(ProviderCandidate("directml", ORT_NAMES["directml"], name, {"device_id": i}))
        elif key == "cuda" and allow_cuda and ORT_NAMES["cuda"] in installed:
            out.append(ProviderCandidate("cuda", ORT_NAMES["cuda"], "GPU0", {"device_id": 0}))
    if not any(c.key == "cpu" for c in out):
        out.append(cpu)  # universal fallback
    return out


def fault(kind: str) -> bool:
    """Fault injection for acceptance tests: AVATAR_LIVE_FAULT=provider|device|latency."""
    return os.environ.get("AVATAR_LIVE_FAULT", "") == kind
