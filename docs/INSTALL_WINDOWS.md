# Instalación en Windows

## 1. Usuario final

### Requisitos
- Windows 10 (21H2+) u 11, x64. 16 GB RAM recomendados. **No se requiere GPU dedicada** para la edición Lite;
  OBS sí necesita una capacidad gráfica que cumpla sus requisitos normales.
- OBS Studio **31 o superior**, x64.
- Webcam solo para los perfiles de face swap.

### Dónde descargar el instalador
- **Compilación de prueba:** pestaña *Actions* del repositorio → workflow **Build Windows Installer** → la ejecución
  más reciente → sección *Artifacts* → `AvatarLive-Setup-<versión>-Lite` (zip con el `.exe` y su `.sha256`).
- **Versión publicada:** al crear una etiqueta `v<versión>` el mismo workflow adjunta el instalador a un
  borrador de *Release* en GitHub, que el mantenedor revisa y publica.

El instalador no está firmado todavía: Windows SmartScreen puede mostrar un aviso. Compruebe el SHA-256 publicado.

### Pasos
1. Cierre OBS y ejecute `AvatarLive-Setup-<versión>-Lite.exe` (o `-Accelerated.exe`). El instalador detecta
   OBS desde `HKLM\SOFTWARE\OBS Studio` y comprueba la versión de `obs64.exe`.
2. Abra OBS → **Fuentes → + → Avatar Live**.
3. Propiedades: elija la foto, confirme el derecho de uso, elija el perfil y el micrófono/webcam.
4. **Iniciar** en las propiedades o en el Dock **Ver → Docks → Avatar Live Control**.
5. OBS → **Iniciar cámara virtual**.
6. En Meet / Teams / Zoom: cámara = **OBS Virtual Camera**; micrófono = su micrófono real.
   - Meet: Configuración → Video → Cámara.
   - Teams: Configuración → Dispositivos → Cámara.
   - Zoom: Configuración → Video → Cámara.

### Modelos para face swap (no incluidos)
Los perfiles `lite_cpu_swap`, `lite_adaptive` y `accelerated_swap` requieren modelos que **usted** aporta y cuya
licencia **usted** debe aceptar (ver [LICENSE_REVIEW.md](LICENSE_REVIEW.md)):

```
%LOCALAPPDATA%\AvatarLive\models\
  face_detection_yunet_2023mar.onnx   (detector; MIT — ya incluido en el instalador desde 0.1.1)
  recognition.onnx                    (embedding estilo ArcFace)
  swapper.onnx                        (compatible con inswapper, 128×128)
```

Los ids y nombres de archivo esperados están en [`models/MODEL_MANIFEST.json`](../models/MODEL_MANIFEST.json).

Opcional: `inswapper_128_fp16.onnx` (o `swapper_fp16.onnx`) junto al FP32. *Lite Adaptive* usa el FP16 con la
GPU/iGPU (DirectML), donde suele ser más rápido, y el FP32 con la CPU (`inference.swap_precision = auto`).

Puede dejar los archivos con su nombre original (`inswapper_128.onnx`, `w600k_r50.onnx`) o usar
`swapper.onnx` / `recognition.onnx`. Mientras un modelo no esté auditado (`sha256: null` en el manifiesto), el
motor **se niega a cargarlo** hasta que usted confirme su licencia para ese archivo concreto (queda ligada a su
SHA-256): en la fuente Avatar Live → **Modelo de rostro**, marque la casilla de licencias y pulse
**Confirmar licencias de modelos** (una sola vez; repítalo si cambia los archivos). Alternativa por línea de
comandos (PowerShell): `& "C:\ProgramData\obs-studio\plugins\obs-avatar-live\data\engine\avatar-engine.exe" models acknowledge <id>`.

Si el manifiesto fija un `sha256`, el archivo debe coincidir exactamente o el motor no arranca.
`Lite CPU Audio` no necesita ninguno de estos modelos (usa la cascada Haar de OpenCV si falta YuNet al crear el perfil).

### Modelo entrenado con su rostro (.dfm)
Si entrenó un modelo con DeepFaceLab o DeepFaceLive (archivo `.dfm`), puede usarlo en lugar de `recognition.onnx` +
`swapper.onnx`; el detector YuNet ya viene incluido en el instalador (desde 0.1.1).

1. En la fuente Avatar Live → **Modelo de rostro** → *Modelo entrenado (.dfm)*.
2. Elija el archivo `.dfm` y marque la confirmación de que el modelo es de su propio rostro (o tiene el
   consentimiento documentado de la persona).
3. Motor: *Lite Adaptive* (recomendado) o *Lite CPU Face Swap*; seleccione la webcam. El perfil con foto sigue siendo
   necesario (consentimiento, pose neutra y benchmark).
4. Si la cara aparece desplazada o con tamaño incorrecto, ajuste **Cobertura** (≈1,0 para modelos *whole face*,
   ≈0,75 para modelos *head*) y **Desplazamiento vertical**.

La alineación se aproxima desde 5 puntos (DeepFaceLab usa 68), por lo que la calidad puede ser algo menor que en
DeepFaceLive. Los modelos DFM (224–320 px) son más pesados que inswapper: sin GPU dedicada espere menos FPS únicos;
el benchmark de arranque lo medirá en su equipo. Prueba sin OBS:
`avatar-engine preview --engine lite_adaptive --profile <id> --video 0 --dfm C:\ruta\yo.dfm --i-have-rights --out prueba.mp4`

### Datos locales
```
%LOCALAPPDATA%\AvatarLive\
  profiles\    perfiles (foto normalizada + consentimiento), ACL solo del usuario
  models\      modelos aportados por el usuario
  benchmarks\  resultados de Lite Adaptive por hardware/driver/modelo/resolución
  logs\        registros sin imágenes ni audio
```

## 2. Compilar desde código fuente

### Herramientas
- Visual Studio 2022 (Desktop development with C++) con un Windows SDK instalado.
  Para Windows 10 puede usarse 10.0.19041.0 si las dependencias lo permiten; use el SDK que el preset detecte.
- CMake ≥ 3.28, Git, PowerShell 7.
- Python 3.11 x64.
- Inno Setup 6 (para el instalador).

### Plugin
El plugin sigue `obsproject/obs-plugintemplate`; `buildspec.json` fija la versión de OBS y de `obs-deps`/Qt6
que se descargan automáticamente. No copie a mano un SDK de OBS.

```powershell
cmake --preset windows-x64
cmake --build --preset windows-x64 --config RelWithDebInfo
```

Resultado: `build_x64\RelWithDebInfo\obs-avatar-live.dll`. El Dock Qt requiere `ENABLE_FRONTEND_API` y `ENABLE_QT`
(activados en los presets del proyecto).

### Motor
```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e engine[dev]
pytest -q tests\unit tests\integration
avatar-engine probe
avatar-engine selftest
```

Extras opcionales: `pip install -e engine[openvino]` o `engine[directml]` (no instale ambos en el mismo entorno:
los paquetes de ONNX Runtime se excluyen mutuamente).

Ejecutable empaquetado:
```powershell
installer\windows\build_engine.ps1 -Variant Lite      # o -Variant Accelerated
```

### Instalador
```powershell
installer\windows\build_installer.ps1 -Variant Lite -PluginBuildDir build_x64\RelWithDebInfo
```

## 3. Diagnóstico

| Síntoma | Comprobación |
|---|---|
| "Avatar Live" no aparece en Fuentes | Ver `%APPDATA%\obs-studio\logs`; comprobar que la DLL está en `C:\ProgramData\obs-studio\plugins\obs-avatar-live\` (OBS 31–32: en su subcarpeta `bin\64bit\`) y la versión de OBS (≥ 31). |
| El plugin aparece como **Legacy** en el *Plugin Manager* | Versión 0.1.0 instalada en Program Files. Instale 0.1.1 o posterior: usa la carpeta nueva y elimina la copia antigua. |
| Estado "engine not responding" | `avatar-engine selftest`; antivirus bloqueando `avatar-engine.exe`; ver `%LOCALAPPDATA%\AvatarLive\logs`. |
| Imagen degradada / FPS únicos bajos | `avatar-engine benchmark`; elegir `CPU puro` o `Bajo consumo`; reducir resolución de captura. |
| DirectML/OpenVINO no se elige | Es esperable si el benchmark no lo considera viable; ver `fallback_reason` en el Dock. |
| Eco o audio duplicado | En la reunión use el micrófono real; Avatar Live no emite audio. |
| La reunión no ve la cámara | Iniciar la cámara virtual en OBS **antes** de abrir la reunión. |
