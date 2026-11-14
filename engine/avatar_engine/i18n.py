"""Texts the engine draws into the video (status screens, banners, hints): English or Spanish.

The language comes from the session config (`ui_language`, sent by the plugin from OBS's locale);
before a session exists, the Windows / system UI language is used.
"""

from __future__ import annotations

import locale
import sys

STRINGS = {
    "starting": {"en": "Starting engine...", "es": "Iniciando motor..."},
    "stopped": {"en": "Stopped", "es": "Detenido"},
    "launching": {"en": "Starting...", "es": "Iniciando..."},
    "start_failed": {"en": "Failed to start", "es": "Error al iniciar"},
    "paused": {"en": "Avatar Live paused", "es": "Avatar Live en pausa"},
    "degraded_title": {"en": "Avatar Live: degraded signal", "es": "Avatar Live: señal degradada"},
    "degraded_detail": {"en": "The avatar is not updating", "es": "El avatar no se está actualizando"},
    "slow_banner": {"en": "Slow update: image is not current", "es": "Actualización lenta: imagen no actual"},
    "faces_per_s": {"en": "faces/s", "es": "cuadros/s"},
    "webcam_unavailable": {"en": "Webcam not available: ", "es": "Webcam no disponible: "},
    "waiting_webcam": {"en": "Waiting for the webcam image...", "es": "Esperando imagen de la webcam..."},
    "inference_error": {"en": "Inference error: ", "es": "Error de inferencia: "},
    "first_frame": {"en": "Processing the first frame...", "es": "Procesando el primer cuadro..."},
    "no_face": {"en": "No face detected in the webcam (look at the camera, good light)",
                "es": "No se detecta un rostro en la webcam (mire a la cámara, buena luz)"},
}


def system_language() -> str:
    try:
        if sys.platform == "win32":
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "es" if (lang_id & 0x3FF) == 0x0A else "en"  # LANG_SPANISH primary id
        code = (locale.getlocale()[0] or "").lower()
        return "es" if code.startswith("es") else "en"
    except Exception:
        return "en"


def normalize(lang: str | None) -> str:
    lang = (lang or "").lower()
    if lang.startswith("es"):
        return "es"
    if lang.startswith("en"):
        return "en"
    return system_language()


def t(key: str, lang: str | None) -> str:
    entry = STRINGS[key]
    return entry.get(normalize(lang), entry["en"])
