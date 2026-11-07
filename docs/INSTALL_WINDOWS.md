# Instalación en Windows

## 1. Usuario final

### Requisitos
- Windows 10 (21H2+) u 11, x64. 16 GB RAM recomendados. **No se requiere GPU dedicada** para la edición Lite;
  OBS sí necesita una capacidad gráfica que cumpla sus requisitos normales.
- OBS Studio **31 o superior**, x64.
- Webcam solo para los perfiles de face swap.

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
  face_detection_yunet_2023mar.onnx   (detector; MIT, opencv_zoo)
  recognition.onnx                    (embedding estilo ArcFace)
  swapper.onnx                        (compatible con inswapper, 128×128)
```

Los ids y nombres de archivo esperados están en [`models/MODEL_MANIFEST.json`](../models/MODEL_MANIFEST.json).

Mientras un modelo no esté auditado (`sha256: null` en el manifiesto), el motor **se niega a cargarlo** hasta que
usted confirme su licencia para ese archivo concreto (la confirmación queda ligada al hash SHA-256 del archivo):

```
avatar-engine models list
avatar-engine models acknowledge arcface_recognition
avatar-engine models acknowledge inswapper_compatible_swapper
avatar-engine models acknowledge yunet_face_detector
```

Si el manifiesto fija un `sha256`, el archivo debe coincidir exactamente o el motor no arranca.
`Lite CPU Audio` no necesita ninguno de estos modelos (usa la cascada Haar de OpenCV si falta YuNet al crear el perfil).

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
| "Avatar Live" no aparece en Fuentes | Ver `%APPDATA%\obs-studio\logs`; comprobar que la DLL está en `obs-plugins\64bit` y la versión de OBS. |
| Estado "engine not responding" | `avatar-engine selftest`; antivirus bloqueando `avatar-engine.exe`; ver `%LOCALAPPDATA%\AvatarLive\logs`. |
| Imagen degradada / FPS únicos bajos | `avatar-engine benchmark`; elegir `CPU puro` o `Bajo consumo`; reducir resolución de captura. |
| DirectML/OpenVINO no se elige | Es esperable si el benchmark no lo considera viable; ver `fallback_reason` en el Dock. |
| Eco o audio duplicado | En la reunión use el micrófono real; Avatar Live no emite audio. |
| La reunión no ve la cámara | Iniciar la cámara virtual en OBS **antes** de abrir la reunión. |
