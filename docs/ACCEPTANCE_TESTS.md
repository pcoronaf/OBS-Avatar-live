# Pruebas de aceptación

Cada prueba remite a requisitos de [REQUIREMENTS.md](REQUIREMENTS.md) y a la sección §12 de la especificación.
Los umbrales marcados **TBD** se fijan tras el benchmark del equipo de referencia (ADR-008); hasta entonces se registra
el valor medido sin declarar cumplimiento.

## Equipo de referencia (completar)

| Campo | Valor |
|---|---|
| CPU | TBD |
| RAM | 16 GB (objetivo) |
| GPU | iGPU Intel, sin GPU dedicada |
| Windows | 10 22H2 x64 / 11 x64 |
| OBS | 31.x |
| ONNX Runtime / OpenVINO / DirectML | TBD |
| Hash de modelos | ver MODEL_MANIFEST.json |

## Herramientas de medición

- Métricas del motor: `get_metrics` (Dock o `avatar-engine benchmark --json`).
- Latencia cámara→OBS: cronómetro LED/pantalla con milisegundos frente a la webcam, grabación a 120/240 FPS de la
  pantalla de OBS y del cronómetro; latencia = diferencia de lectura. Mínimo 30 muestras; informar P50/P95.
- Audio→labios: clic/palmada grabados junto a la pantalla; desfase entre inicio de onda y apertura de boca.
- Memoria: Performance Monitor (`Private Bytes` de `obs64.exe` y `avatar-engine.exe`) muestreado cada 10 s durante 60 min.
  "Sin crecimiento no acotado" = pendiente de regresión lineal de los últimos 40 min < **TBD** MB/h.
- Tráfico: Resource Monitor / `netstat -b` o Wireshark filtrando los PID de OBS y del motor.

## A. Instalación y OBS

| ID | Procedimiento | Criterio |
|---|---|---|
| AT-A1 | Instalar en Windows 10 x64 limpio con OBS 31. Abrir OBS, agregar "Avatar Live", cerrar y reabrir OBS 5 veces. | Aparece en Fuentes; ningún fallo al cerrar/reabrir; render sin bloqueos (log sin "video thread stalled"). |
| AT-A2 | Iniciar cámara virtual; seleccionar OBS Virtual Camera en la app de Cámara de Windows y en Teams. | Imagen visible con etiqueta. No se instaló ningún driver de cámara nuevo (Administrador de dispositivos). |
| AT-A3 | Con sesión activa, matar `avatar-engine.exe` desde el Administrador de tareas. | OBS no se bloquea ni cierra; estado visible "engine not responding"; motor relanzado y sesión recuperada en < 10 s (objetivo). |

Automatizado: `tests/integration` (ring + pipe extremo a extremo, relanzamiento del motor simulado), test C++ de lectura del ring.

## B. Lite CPU Audio

| ID | Procedimiento | Criterio |
|---|---|---|
| AT-B1 | Sin webcam, sin GPU dedicada: crear perfil desde foto autorizada, perfil `lite_cpu_audio`, 1280×720@30. | Avatar reconocible; `output_fps` ≥ 29 sostenido (meta); parpadeo no periódico. |
| AT-B2 | Leer un texto de 60 s y alternar silencio. Medir desfase audio→boca. Variar `audio.sensitivity`. | Boca se abre al hablar y cierra en silencio; desfase P95 **TBD** ms; la sensibilidad cambia la respuesta. |
| AT-B3 | Sesión de 60 min con Teams y OBS abiertos y `cpu_budget_pct` configurado. | Sin crecimiento de memoria no acotado; CPU dentro del presupuesto ± **TBD**; degradación controlada si se excede. |

Automatizado: `tests/unit` (VAD, visemas, rig, overlay, scheduler), `tests/performance` (cadencia de salida del motor de audio).

## C. Lite CPU Swap

| ID | Procedimiento | Criterio |
|---|---|---|
| AT-C1 | Webcam 640×480, perfil `lite_cpu_swap`, girar la cabeza ±30°, abrir/cerrar la boca. | Rostro sustituido; orientación básica y apertura de boca preservadas; FPS únicos informados. |
| AT-C2 | Forzar tasas de inferencia de 5, 10 y 15 FPS (`set_quality`). Sesión de 60 min a la tasa viable. | Se registran calidad percibida y métricas a cada tasa; sin crecimiento de memoria no acotado. Umbral mínimo **TBD** (meta ≥ 5). |
| AT-C3 | Durante AT-C2 observar `output_fps` y `repeated_frame_pct`. | Salida a 30 FPS aunque los cuadros únicos sean menos; `repeated_frame_pct` coherente con `face_swap_unique_fps`. |

## C2. Lite Adaptive

| ID | Procedimiento | Criterio |
|---|---|---|
| AT-C2-1 | `avatar-engine probe` y arranque `lite_adaptive` con proveedor `auto` en equipos con y sin OpenVINO/DirectML. | Cada proveedor disponible se prueba realmente (benchmark registrado) antes de ser seleccionado. |
| AT-C2-2 | Arrancar con `cpu_only=true` / perfil `CPU puro`. | `execution_provider_selected = CPUExecutionProvider`; el log confirma que no se inicializó ningún EP acelerado. |
| AT-C2-3 | Revisar `%LOCALAPPDATA%\AvatarLive\benchmarks\`; cambiar resolución de captura o modelo y reiniciar. | Benchmark guardado por hardware+driver+modelo+resolución e invalidado al cambiar; métricas muestran FPS únicos, % repetidos, P95 y `fallback_reason`. |
| AT-C2-4 | 60 min con OBS + reunión + carga de CPU sintética (p. ej. compresión en segundo plano). | Degradación con histéresis (sin oscilar más de 1 cambio por 3 ventanas); OBS responsivo; memoria acotada. |
| AT-C2-5 | Simular: desconectar la webcam; forzar error del proveedor (`AVATAR_LIVE_FAULT=provider` o modelo corrupto); retrasar la captura > 500 ms. | Fallback visible y controlado, `fallback_reason` informado, OBS no se cierra; nunca se muestra una cara congelada como actual. |

Automatizado: `tests/unit` (selector, controlador adaptativo con histéresis, caché de benchmark, política de cuadro obsoleto).

## D. Accelerated (experimental)

| ID | Procedimiento | Criterio |
|---|---|---|
| AT-D1 | Con CUDA/DirectML disponible, provocar un fallo del EP a mitad de sesión. | Vuelve a CPU sin reiniciar OBS; `fallback` emitido. |
| AT-D2 | Benchmark Accelerated vs Lite en ≥ 2 equipos con la misma foto y el mismo video grabado. | Informe comparativo; meta ≥ 24 FPS únicos en GPU media (no garantizada). |

## E. Seguridad y licencias

| ID | Procedimiento | Criterio |
|---|---|---|
| AT-E1 | Revisar 100 cuadros aleatorios de la cámara virtual en todos los perfiles y posiciones de etiqueta. | Etiqueta «Avatar generado por IA» presente y legible en todos; no existe opción para desactivarla. |
| AT-E2 | Intentar crear un perfil sin confirmar derechos (UI, pipe y CLI). | Rechazado. |
| AT-E3 | Crear y borrar un perfil; borrar cachés. | Archivos eliminados de `profiles\` y `benchmarks\` según corresponda. |
| AT-E4 | Sesión de 15 min monitorizando red y disco. | Sin conexiones salientes de OBS/motor por Avatar Live; ningún archivo de audio/video creado; logs sin imágenes. |
| AT-E5 | Validar `MODEL_MANIFEST.json` antes de un release. | Cada modelo con origen, versión, `sha256`, licencia y `commercial_use` revisados. |
| AT-E6 | Intentar conectar a la pipe desde otro usuario y con token incorrecto. | Conexión rechazada (`unauthorized`). |

## Anexo — Cobertura automatizada (archivos concretos)

Las pruebas automatizadas verifican lógica y contratos en CI; **no sustituyen** las pruebas manuales en el equipo de
referencia Windows (rendimiento, cámara virtual, aplicaciones de reunión).

| Criterio | Pruebas |
|---|---|
| A (IPC, recuperación, cierre) | `tests/integration/test_engine_server.py` (hello/token, ciclo de sesión, shutdown, salida al morir el proceso padre), `tests/integration/test_native_engine_interop.py` (lector C++ ↔ motor real), `tests/native/test_native_interop.py` (reinicio del motor por el controlador C++, recuperación < 10 s, cierre acotado) |
| B (Lite CPU Audio) | `tests/unit/test_audio.py` (VAD, visemas, sensibilidad, fricativas), `tests/unit/test_rig_and_audio_engine.py` (rig, parpadeo aperiódico, pose neutra), `tests/performance/test_performance.py` (presupuesto de render 720p, soak; `AVATAR_SOAK_SECONDS=3600` para AT-B-3) |
| C (Lite CPU Swap) | `tests/unit/test_swap.py` (alineación, paste-back, pipeline con modelos ONNX falsos, descarte de captura atrasada), `tests/unit/test_scheduler.py` (salida a ritmo fijo, cuadros repetidos, nunca cara congelada > 500 ms) |
| C2 (Lite Adaptive) | `tests/unit/test_adaptive.py` (escalera de degradación, histéresis de 3 ventanas, selector), `tests/unit/test_swap.py` (CPU puro sin EP acelerados, benchmark + caché, fallo de proveedor → CPU, `AVATAR_LIVE_FAULT=provider`) |
| E (seguridad/licencias) | `tests/unit/test_privacy.py` (consentimiento, borrado de perfil y cachés, etiqueta no vaciable), `tests/unit/test_config.py` (`disclosure_overlay` y `local_only` no desactivables), `tests/unit/test_swap.py` (manifiesto: checksum, modelos no auditados requieren aceptación), `tests/unit/test_frame_ring.py` (bit `synthetic_label` forzado) |

Inyección de fallos disponible (`AVATAR_LIVE_FAULT`): `provider` (fallan los EP no-CPU), `device` (cámara perdida),
`latency` (captura atrasada 600 ms).
