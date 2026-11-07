"""Control-channel envelope helpers (docs/PROTOCOL.md §3)."""

from __future__ import annotations

import json
import struct
from typing import Any

from avatar_engine import PROTOCOL_VERSION

MAX_MESSAGE_BYTES = 1 << 20

REQUEST_TYPES = {
    "hello", "ping", "list_devices", "list_profiles", "create_profile", "delete_profile", "start_session",
    "stop_session", "pause", "resume", "freeze_neutral", "set_expression", "set_quality", "get_metrics",
    "shutdown",
}


class ProtocolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _json_default(o: Any) -> Any:
    """numpy scalars/arrays and paths appear in metrics; convert them to JSON types."""
    try:
        import numpy as np

        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return None if not np.isfinite(o) else float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(o)


def encode(msg: dict[str, Any]) -> bytes:
    try:
        body = json.dumps(msg, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
                          default=_json_default).encode("utf-8")
    except ValueError as exc:  # NaN/inf
        raise ProtocolError("bad_value", str(exc)) from exc
    if len(body) > MAX_MESSAGE_BYTES:
        raise ProtocolError("too_large", "message exceeds 1 MiB")
    return struct.pack("<I", len(body)) + body


def decode_length(header: bytes) -> int:
    (n,) = struct.unpack("<I", header)
    if n > MAX_MESSAGE_BYTES:
        raise ProtocolError("too_large", "message exceeds 1 MiB")
    return n


def decode_body(body: bytes) -> dict[str, Any]:
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("bad_json", str(exc)) from exc
    if not isinstance(msg, dict):
        raise ProtocolError("bad_envelope", "message must be an object")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("bad_version", f"unsupported protocol version {msg.get('v')!r}")
    if not isinstance(msg.get("type"), str):
        raise ProtocolError("bad_envelope", "missing type")
    if not isinstance(msg.get("payload", {}), dict):
        raise ProtocolError("bad_envelope", "payload must be an object")
    return msg


def request(msg_id: int, type_: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": msg_id, "type": type_, "payload": payload or {}}


def reply_ok(msg_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": msg_id, "type": "reply", "ok": True, "payload": payload or {}}


def reply_error(msg_id: int, code: str, message: str) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": msg_id, "type": "reply", "ok": False,
            "error": {"code": code, "message": message}}


def event(name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": 0, "type": "event", "event": name, "payload": payload or {}}
