# Revisión de licencias

> **No es asesoría legal.** Todo lo marcado "verificar" debe confirmarlo un abogado antes de cualquier
> distribución pública o comercial. Este documento es un bloqueo previo a distribución (spec §9).

## 1. Licencia del proyecto

- Plugin: enlaza `libobs` (GPL-2.0-or-later) → el plugin se distribuye como **GPL-2.0-or-later**.
- Motor: proceso separado, también publicado bajo GPL-2.0-or-later para simplificar. La separación en procesos
  **no garantiza** por sí sola aislar obligaciones de licencia.
- El texto de `LICENSE` es GPL-2.0, tomado de `obs-plugintemplate`.

## 2. Deep-Live-Cam

- Licencia: **GNU AGPL-3.0**.
- Uso en este proyecto: **solo análisis** de su arquitectura (detección → alineación → swap → pegado).
  **No se copió código.** Todo el motor está escrito desde cero.
- Si en el futuro se reutiliza código, la combinación GPL-2.0-only no es compatible con AGPL-3.0; habría que
  relicenciar a GPL-3.0-or-later/AGPL-3.0 y cumplir la obligación de ofrecer fuente a usuarios remotos. Verificar.

## 3. Modelos — auditoría por artefacto

| Artefacto | Rol | Origen típico | Licencia declarada | Uso comercial | Incluido | Estado |
|---|---|---|---|---|---|---|
| YuNet `face_detection_yunet_2023mar.onnx` | Detector 5 puntos | opencv/opencv_zoo | MIT | Sí (verificar) | No | Verificar hash y licencia |
| Cascadas Haar de OpenCV | Detector de respaldo | Incluidas en opencv-python | Licencia Intel tipo BSD (cabecera del XML) | Sí (verificar) | Vía opencv-python | Verificar |
| Reconocimiento estilo ArcFace (`recognition.onnx`) | Embedding de identidad | p. ej. InsightFace `buffalo_l/w600k_r50` | InsightFace: pesos **no comerciales / investigación** | **No** según InsightFace | No | Aportado por el usuario |
| Swap compatible `inswapper` (`swapper.onnx`) | Sustitución facial | InsightFace `inswapper_128` | **No comercial**; distribución restringida | **No** | No | Aportado por el usuario |
| Enhancer (GFPGAN, CodeFormer…) | Restauración | Varios | Varía (CodeFormer: S-Lab no comercial) | Varía | No | **No soportado en v1** |

Notas:
- Las licencias del **código** de InsightFace (MIT) no se extienden a sus **pesos**; el README de Deep-Live-Cam
  también advierte sobre uso no comercial. Auditar términos del hospedaje (Hugging Face, GitHub releases) por artefacto.
- `models/MODEL_MANIFEST.json` registra origen, versión, `sha256`, licencia y uso comercial. `sha256` queda en `null`
  hasta la auditoría; el motor debe rechazar o advertir sobre modelos no auditados.

## 4. Dependencias de software

| Componente | Licencia | Notas |
|---|---|---|
| OBS Studio / libobs | GPL-2.0-or-later | Enlace dinámico |
| obs-plugintemplate (cmake/, .github/) | GPL-2.0 | Base de compilación |
| Qt 6 (Dock) | LGPL-3.0 | Enlace dinámico vía obs-deps; usar las DLL de OBS |
| ONNX Runtime | MIT | |
| onnxruntime-openvino / OpenVINO | MIT (ORT) / Apache-2.0 (OpenVINO) | Verificar binarios redistribuibles |
| onnxruntime-directml / DirectML | MIT (ORT); DirectML: términos de Microsoft para redistribución | **Verificar** términos de `DirectML.dll` |
| CUDA / cuDNN (solo Accelerated) | EULA NVIDIA | No incluido en Lite; verificar redistribución |
| OpenCV (opencv-python-headless) | Apache-2.0 (≥ 4.5) | |
| NumPy | BSD-3-Clause | |
| sounddevice / PortAudio | MIT / MIT-like | |
| PyInstaller (bootloader) | GPL-2.0 con excepción | La excepción permite empaquetar |
| Inno Setup | Licencia Inno Setup | Solo herramienta de compilación |

## 5. Estrategias

- **A — Prototipo interno de investigación:** permitido usar modelos no comerciales localmente, con aviso de límites y sin distribuir pesos.
- **B — Producto distribuible:** código propio + modelos con licencia explícita para distribución/uso comercial (p. ej. modelos entrenados internamente con datos con derechos).
- **C — Licencias adicionales:** negociar licencias comerciales de los pesos necesarios.

Estado actual: **A**. Ninguna versión pública/comercial antes de completar esta revisión y el manifiesto.

## 6. Artefactos de cumplimiento

- `THIRD_PARTY_NOTICES.md`
- `models/MODEL_MANIFEST.json`
- SBOM (pendiente: generar con `pip-licenses`/CycloneDX en el build del motor)
