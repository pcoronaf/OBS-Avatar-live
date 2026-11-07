"""`avatar-engine serve`: control server + frame ring writer, launched by the OBS plugin."""

from __future__ import annotations

import hmac
import logging
import os
import sys
import threading
import time
from typing import Any

from avatar_engine import PROTOCOL_VERSION, __version__, engine_factory, frame_ring as fr, ipc, protocol
from avatar_engine.config import ConfigError
from avatar_engine.engines.base import EngineUnavailable
from avatar_engine.overlay import DisclosureLabel, status_frame
from avatar_engine.privacy import profile_store
from avatar_engine.privacy.consent import ConsentError

log = logging.getLogger("avatar_engine.server")

LONG_OPS = {"start_session", "create_profile"}


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        from avatar_engine.winsec import process_alive as _alive

        return _alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class EngineServer:
    def __init__(self, session: str, auth_token: str, parent_pid: int | None = None, max_width: int = 1920,
                 max_height: int = 1080, profile_root=None) -> None:
        if not auth_token or len(auth_token) < 16:
            raise ValueError("AVATAR_LIVE_AUTH_TOKEN missing or too short")
        self.session_name = session
        self.token = auth_token
        self.parent_pid = parent_pid
        self.profile_root = profile_root
        self.writer = fr.FrameRingWriter(session, max_width, max_height)
        self.server = ipc.create_server(session)
        self.conn: ipc.Connection | None = None
        self.session = None
        self._stop = threading.Event()
        self._op_lock = threading.Lock()
        self._label = DisclosureLabel()
        self._idle_detail = "Detenido"
        self._shutdown_requested = False

    # -- events ---------------------------------------------------------------------------
    def emit(self, name: str, payload: dict) -> None:
        c = self.conn
        if c is None:
            return
        try:
            c.send(protocol.event(name, payload))
        except ipc.ConnectionClosed:
            pass

    # -- background threads ---------------------------------------------------------------
    def _idle_loop(self) -> None:
        """Heartbeat + a face-free status frame whenever no session is running."""
        frame = None
        detail = None
        while not self._stop.wait(0.2):
            if self.session is None:
                if detail != self._idle_detail:
                    detail = self._idle_detail
                    frame = self._label.apply(status_frame(1280, 720, "Avatar Live", detail))
                self.writer.write(frame, fr.FLAG_SYNTHETIC | fr.FLAG_PAUSED)
            else:
                self.writer.heartbeat()

    def _watch_parent(self) -> None:
        while not self._stop.wait(1.0):
            if self.parent_pid and not process_alive(self.parent_pid):
                log.warning("parent process %s exited; shutting down", self.parent_pid)
                self.stop()

    def _metrics_loop(self) -> None:
        while not self._stop.wait(1.0):
            s = self.session
            if s is not None and self.conn is not None:
                try:
                    self.emit("metrics", s.metrics())
                except Exception as exc:  # never let metrics kill the server
                    log.debug("metrics failed: %s", exc)

    # -- request handling -----------------------------------------------------------------
    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        t, p, mid = msg["type"], msg.get("payload", {}), msg.get("id", 0)
        try:
            return protocol.reply_ok(mid, self._dispatch(t, p))
        except protocol.ProtocolError as exc:
            return protocol.reply_error(mid, exc.code, exc.message)
        except ConsentError as exc:
            return protocol.reply_error(mid, "consent_required", str(exc))
        except (ConfigError, profile_store.ProfileError, ValueError) as exc:
            return protocol.reply_error(mid, "invalid_request", str(exc))
        except EngineUnavailable as exc:
            return protocol.reply_error(mid, "engine_unavailable", str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("request %s failed", t)
            return protocol.reply_error(mid, "internal", f"{type(exc).__name__}: {exc}")

    def _dispatch(self, t: str, p: dict) -> dict:
        if t == "ping":
            return {"now_ns": fr.now_ns()}
        if t == "list_devices":
            from avatar_engine.audio.capture import list_input_devices
            from avatar_engine.vision.capture import list_video_devices

            return {"video": list_video_devices(), "audio": list_input_devices()}
        if t == "list_profiles":
            return {"profiles": profile_store.list_profiles(self.profile_root)}
        if t == "create_profile":
            prof, warnings = profile_store.create_profile(p.get("photo_path", ""), p.get("display_name", "Avatar"),
                                                          p.get("consent") or {}, self.profile_root)
            return {"profile_id": prof.profile_id, "warnings": warnings}
        if t == "delete_profile":
            if self.session is not None and self.session.profile.profile_id == p.get("profile_id"):
                raise ValueError("stop the session before deleting its profile")
            profile_store.delete_profile(p.get("profile_id", ""), self.profile_root)
            return {}
        if t == "start_session":
            return self._start(p)
        if t == "stop_session":
            self._stop_session("Detenido")
            return {}
        if t in ("pause", "resume", "freeze_neutral", "set_expression", "set_quality", "get_metrics"):
            s = self.session
            if s is None:
                raise protocol.ProtocolError("no_session", "no session is running")
            if t == "pause":
                s.pause(True)
            elif t == "resume":
                s.pause(False)
            elif t == "freeze_neutral":
                s.freeze_neutral(bool(p.get("enabled", True)))
            elif t == "set_expression":
                s.engine.set_expression(str(p.get("expression_id", "neutral")), float(p.get("intensity", 0.5)),
                                        int(p.get("duration_ms", 1000)))
            elif t == "set_quality":
                s.engine.set_quality(**{k: v for k, v in p.items() if k in
                                        ("profile", "cpu_threads", "detection_interval", "enhancement", "sensitivity")})
            elif t == "get_metrics":
                return s.metrics()
            return {}
        if t == "shutdown":
            self._shutdown_requested = True  # acted on after the reply has been sent
            return {}
        raise protocol.ProtocolError("unknown_type", f"unknown request type {t!r}")

    def _start(self, cfg: dict) -> dict:
        from avatar_engine.session import Session

        with self._op_lock:
            self._stop_session(None)
            s = Session(cfg, self.writer, self.emit, profile_root=self.profile_root)
            self._idle_detail = "Iniciando..."
            try:
                result = s.start()
            except Exception as exc:
                s.stop()
                self._idle_detail = "Error al iniciar"
                self.emit("error", {"code": "start_failed", "message": str(exc), "recoverable": True})
                raise
            self.session = s
            return result

    def _stop_session(self, detail: str | None) -> None:
        s, self.session = self.session, None
        if s is not None:
            s.stop()
        if detail:
            self._idle_detail = detail

    def _hello(self, conn: ipc.Connection) -> bool:
        try:
            msg = conn.recv()
        except (ipc.ConnectionClosed, protocol.ProtocolError):
            return False
        p = msg.get("payload", {})
        token_ok = isinstance(p.get("auth_token"), str) and hmac.compare_digest(p["auth_token"], self.token)
        peer_ok = self.parent_pid is None or conn.peer_pid in (None, self.parent_pid)
        if msg.get("type") != "hello" or not token_ok or not peer_ok:
            conn.send(protocol.reply_error(msg.get("id", 0), "unauthorized", "authentication failed"))
            return False
        conn.send(protocol.reply_ok(msg.get("id", 0), {
            "engine_version": __version__, "protocol_version": PROTOCOL_VERSION,
            "engines": engine_factory.describe(),
            "frame_ring": {"name": fr.ring_name(self.session_name), "event_name": fr.event_name(self.session_name),
                           "slot_count": self.writer.slot_count, "max_width": self.writer.max_width,
                           "max_height": self.writer.max_height},
        }))
        return True

    def _serve_connection(self, conn: ipc.Connection) -> None:
        if not self._hello(conn):
            conn.close()
            return
        self.conn = conn
        try:
            while not self._stop.is_set():
                try:
                    msg = conn.recv()
                except protocol.ProtocolError as exc:
                    conn.send(protocol.reply_error(0, exc.code, exc.message))
                    continue
                if msg.get("type") in LONG_OPS:
                    threading.Thread(target=lambda m=msg: self._reply(conn, m), daemon=True).start()
                else:
                    self._reply(conn, msg)
        except ipc.ConnectionClosed:
            pass
        finally:
            self.conn = None
            conn.close()

    def _reply(self, conn: ipc.Connection, msg: dict) -> None:
        r = self.handle(msg)
        try:
            try:
                conn.send(r)
            except protocol.ProtocolError as exc:  # unencodable payload: still answer the request
                conn.send(protocol.reply_error(msg.get("id", 0), "internal", exc.message))
        except ipc.ConnectionClosed:
            pass
        if self._shutdown_requested:
            self.stop()

    def serve_forever(self) -> None:
        for target in (self._idle_loop, self._watch_parent, self._metrics_loop):
            threading.Thread(target=target, daemon=True, name=target.__name__).start()
        try:
            while not self._stop.is_set():
                conn = self.server.accept()
                if conn is not None:
                    self._serve_connection(conn)  # one client (the plugin) at a time
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._stop.set()
        c = self.conn
        if c is not None:
            c.close()
        try:
            self.server.close()
        except Exception:
            pass

    def _shutdown(self) -> None:
        self._stop_session(None)
        time.sleep(0.05)
        self.server.close()
        self.writer.close()
