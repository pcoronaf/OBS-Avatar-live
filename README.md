# OBS Avatar Live

> **Estado:** código de Fase 1 y Fase 2 escrito, **no validado aún en hardware Windows de referencia**.
> Las cifras de rendimiento de este repositorio son **metas de diseño, no mediciones**.
> Ver [Estado por fase](#estado-por-fase) y [Licencias](#licencias--bloqueo-antes-de-distribuir).

**Avatar Live** es una fuente nativa para OBS Studio (Windows 10/11 x64) que genera un video de
avatar a partir de una **fotografía autorizada** y lo entrega a reuniones (Meet, Teams, Zoom) a
través de la **OBS Virtual Camera** ya incluida en OBS. Todo el procesamiento es local: no se suben
imágenes, audio ni video a la nube.

Cada cuadro emitido lleva una etiqueta visible **«Avatar generado por IA»** que no se puede desactivar.
Este proyecto **no** está pensado para superar pruebas de vida (liveness), KYC, verificación
biométrica ni para suplantar a nadie.

## Qué es y qué no es

| Lo que hace | Lo que **no** hace |
|---|---|
| Foto + micrófono → avatar 2D/2.5D animado (boca por visemas heurísticos, parpadeo, mirada, microgestos). | No es fotorrealista; no es un "talking head" neuronal. |
| Foto + webcam → sustitución facial (face swap) en CPU o con aceleración validada. | El face swap **necesita un video conductor** (webcam). Una foto + micrófono no equivale a un video conductor. |
| Salida estable a 30 FPS en OBS. | 30 FPS de salida **no** significa 30 caras nuevas por segundo: se informa por separado el FPS de cuadros únicos y el % de cuadros repetidos. |
| Selección automática OpenVINO / DirectML / CPU por benchmark real (Lite Adaptive). | No asume que una GPU integrada sea más rápida que la CPU: lo mide. |
| Indicadores de estado, latencia P50/P95 y degradación visible. | No oculta congelaciones: si la imagen tiene >500 ms de antigüedad se muestra estado degradado. |

## Perfiles de procesamiento

| Perfil | Entrada | Modelos neuronales | Meta (no garantía) |
|---|---|---|---|
| **Lite CPU Audio** (`lite_cpu_audio`) | Foto + micrófono | Ninguno (detector facial solo al crear el perfil) | 1280×720 a 30 FPS de salida en CPU |
| **Lite CPU Face Swap** (`lite_cpu_swap`) | Foto + webcam | Detector + reconocimiento + swap (aportados por el usuario) | 5–15 caras nuevas/s en CPU, sujeto a benchmark |
| **Lite Adaptive** (`lite_adaptive`, recomendado) | Foto + webcam | Igual que el anterior | CPU puro 5–10, Intel optimizado 10–15 caras nuevas/s; P95 < 250 ms |
| **Accelerated Swap** (`accelerated_swap`, experimental) | Foto + webcam | Igual, con CUDA/DirectML/OpenVINO | ≥24 caras nuevas/s en GPU media |
| **Synthetic Head** (`synthetic_head`, Fase 4) | Foto + micrófono | Por definir | Solo esqueleto; no funcional |

Perfiles de usuario de Lite Adaptive: `Auto`, `CPU puro` (nunca inicializa un proveedor acelerado),
`Intel optimizado`, `Máximo rendimiento`, `Bajo consumo`.

## Inicio rápido (usuario)

1. Instale OBS Studio ≥ 31 (x64) y ejecute el instalador `AvatarLive-Setup-<versión>-Lite.exe`.
2. Abra OBS → **Fuentes → + → Avatar Live**.
3. En las propiedades, cargue su foto (PNG/JPEG/WebP) y **confirme que tiene derecho a usarla**
   (o el consentimiento del titular).
4. Elija el perfil (`Lite CPU Audio` si no tiene webcam) y pulse **Iniciar**.
5. OBS → **Iniciar cámara virtual**. En la aplicación de reuniones seleccione **OBS Virtual Camera**
   como cámara y su **micrófono real** como micrófono (Avatar Live no reemite audio).

Para face swap debe colocar usted mismo los modelos ONNX en `%LOCALAPPDATA%\AvatarLive\models\`
y aceptar sus licencias; ver [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md) y
[models/MODEL_MANIFEST.json](models/MODEL_MANIFEST.json).

## Inicio rápido (desarrollo)

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e engine[dev]
pytest -q tests/unit tests/integration
avatar-engine probe              # proveedores ONNX Runtime, CPU, memoria, adaptadores
avatar-engine profile create --photo foto.jpg --name "Yo" --i-have-rights --subject-is-self
avatar-engine preview --engine lite_cpu_audio --profile <profile_id> --audio voz.wav --out preview.mp4
avatar-engine selftest           # comprobaciones offline (config, IPC, frame ring, rig 2D)
```

Pruebas del núcleo C++ del plugin sin OBS (Linux/macOS, requiere CMake): `pytest -q tests/native`.

Plugin (Windows, Visual Studio 2022, CMake ≥ 3.28), siguiendo los presets de `obs-plugintemplate`:

```bash
cmake --preset windows-x64
cmake --build --preset windows-x64 --config RelWithDebInfo
```

Detalles: [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md).

## Privacidad, consentimiento y etiqueta

- Consentimiento explícito al crear un perfil; si la foto es de un tercero se pide referencia de consentimiento.
- Etiqueta **«Avatar generado por IA»** grabada en cada cuadro por el motor; puede moverse o agrandarse, no ocultarse.
- Perfiles guardados solo en `%LOCALAPPDATA%\AvatarLive\profiles\` con permisos del usuario; se pueden borrar.
- No se graba micrófono ni video por defecto. Los registros no contienen rostros, audio ni datos biométricos.
- Sin tráfico de red durante el uso local; sin drivers de cámara adicionales.

Ver [docs/PRIVACY_AND_SAFETY.md](docs/PRIVACY_AND_SAFETY.md).

## Licencias — bloqueo antes de distribuir

- El código de este repositorio es **GPL-2.0-or-later** (el plugin enlaza `libobs`).
- **Deep-Live-Cam** (AGPL-3.0) se usó solo como referencia de análisis; **no se copió su código**.
- **No se incluyen pesos de modelos.** Los modelos de swap compatibles con `inswapper` y de
  reconocimiento de InsightFace tienen, según su documentación, restricciones de **uso no comercial
  / investigación**. Cada artefacto debe auditarse antes de cualquier entrega pública o comercial.
- Revisión completa (no es asesoría legal): [docs/LICENSE_REVIEW.md](docs/LICENSE_REVIEW.md) y
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Estado por fase

| Fase | Contenido | Estado |
|---|---|---|
| 0 | Revisión de licencias, benchmark base | Documentado; benchmark de referencia **pendiente** |
| 1 | Plugin + IPC + Lite CPU Audio + instalador Lite | Código implementado; **no validado en Windows** |
| 2 | Lite CPU Face Swap + Lite Adaptive | Código implementado; requiere modelos aportados por el usuario; **no validado** |
| 3 | Accelerated (CUDA/DirectML/OpenVINO) | Experimental |
| 4 | Synthetic Talking Head | Esqueleto (stub) |
| 5 | Endurecimiento, instalador firmado | Pendiente |

## Estructura del repositorio

```text
plugin/          Fuente OBS nativa (C++17, libobs, Dock Qt opcional)
engine/          Motor Python avatar_engine (proceso separado, ONNX Runtime)
contracts/       Esquema de configuración y .proto de referencia
docs/            Arquitectura, requisitos, protocolo IPC, instalación, licencias, aceptación
models/          MODEL_MANIFEST.json (sin pesos)
installer/       PyInstaller + Inno Setup para Windows
tests/           unit / integration / performance
cmake/, build-aux/, buildspec.json, CMakePresets.json   Basados en obsproject/obs-plugintemplate
```

---

## English summary

OBS Avatar Live is a native OBS Studio source for Windows that turns an **authorized photo** into a live
avatar and outputs it through OBS Virtual Camera. Engines: a CPU-only 2D audio-driven rig (photo + mic, not
photorealistic), a CPU face swap (photo + webcam — a driving video is required), Lite Adaptive (benchmarks
OpenVINO/DirectML/CPU at startup and picks the viable one), plus experimental accelerated and stub
talking-head engines. Inference runs in a separate `avatar-engine` process; frames reach OBS through a
shared-memory ring and a named-pipe control channel ([docs/PROTOCOL.md](docs/PROTOCOL.md)). Every frame carries
a non-removable "AI-generated avatar" label. No model weights are shipped; InsightFace-derived weights are
non-commercial. Output FPS and unique (newly generated) FPS are reported separately; all performance figures
are goals, not measurements. Phases 1–2 are implemented but not yet validated on Windows hardware.
License: GPL-2.0-or-later.
