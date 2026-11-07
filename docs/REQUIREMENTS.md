# Requisitos — OBS Avatar Live 2.1

IDs trazables a [ACCEPTANCE_TESTS.md](ACCEPTANCE_TESTS.md). "DEBE" = obligatorio para v1.

## Funcionales

| ID | Requisito | Aceptación |
|---|---|---|
| FR-01 | Cargar PNG/JPEG/WebP, detectar exactamente un rostro, validar nitidez, tamaño y alineación, y guardar perfil local. | AT-B1, AT-E3 |
| FR-02 | Exigir confirmación de derecho de uso antes de crear un perfil; consentimiento de tercero si aplica. | AT-E2 |
| FR-03 | Elegir fuente conductora: micrófono, webcam o sintética. | AT-B1, AT-C1 |
| FR-04 | Perfil Lite CPU Audio: animación 2D/2.5D con visemas heurísticos, parpadeo, mirada y microgestos sin webcam. | AT-B1..B3 |
| FR-05 | Perfil Lite CPU Swap con `CPUExecutionProvider`, detección espaciada y tracking. | AT-C1..C3 |
| FR-06 | Perfil Lite Adaptive: descubrir proveedores, benchmark de arranque, selección y degradación con histéresis; modo `cpu_only`. | AT-C2-1..C2-5 |
| FR-07 | Perfil Accelerated con fallback a CPU sin reiniciar OBS (experimental). | AT-D1, AT-D2 |
| FR-08 | Fuente OBS "Avatar Live" configurable (16:9, fondo/alfa, recorte, zoom, encuadre). | AT-A1 |
| FR-09 | Salida por OBS Virtual Camera; no instalar driver de cámara. | AT-A2 |
| FR-10 | Audio del micrófono no se reemite por la fuente ni se graba por defecto. | AT-E4 |
| FR-11 | Indicadores: motor, proveedor, FPS de inferencia, FPS de salida, % repetidos, P50/P95, cuadros perdidos, CPU/RAM, aviso sintético. | AT-C2-3 |
| FR-12 | Pausa, congelar a pose neutra, detener, recuperar motor caído, fallback. | AT-A3, AT-C2-5 |
| FR-13 | Etiqueta «Avatar generado por IA» visible en todos los cuadros; movible/escalable, no desactivable. | AT-E1 |
| FR-14 | Borrado de perfiles y cachés. | AT-E3 |

## No funcionales

| ID | Requisito | Aceptación |
|---|---|---|
| NFR-01 | Windows 10/11 x64, OBS Studio ≥ 31 x64. | AT-A1 |
| NFR-02 | Funcionar sin GPU dedicada (Lite). | AT-B1, AT-C2-2 |
| NFR-03 | Procesamiento 100 % local; sin tráfico externo durante el uso. | AT-E4 |
| NFR-04 | Un fallo del motor no bloquea ni cierra OBS; recuperación objetivo < 10 s. | AT-A3 |
| NFR-05 | Salida a 30 FPS independientemente de la cadencia de inferencia. | AT-B1, AT-C3 |
| NFR-06 | Lite Audio: 1280×720 @ 30 FPS de salida en equipo de referencia (meta). | AT-B1 |
| NFR-07 | Lite Swap: ≥5 caras nuevas/s en CPU de referencia (meta, ajustar tras benchmark). | AT-C2 |
| NFR-08 | Lite Adaptive: P95 extremo a extremo < 250 ms (meta). | AT-C2-3 |
| NFR-09 | 60 minutos sin crecimiento de memoria no acotado. | AT-B3, AT-C2, AT-C2-4 |
| NFR-10 | Cuadro de más de 500 ms se marca degradado; no se muestra una cara congelada como actual. | AT-C2-5 |
| NFR-11 | Hilo de render de OBS nunca bloquea en IPC. | AT-A1 |
| NFR-12 | Registros sin rostros, audio ni datos biométricos derivados. | AT-E4 |
| NFR-13 | Manifiesto de modelos con origen, checksum y licencia validados antes de distribución. | AT-E5 |
| NFR-14 | Named pipe restringida al usuario de la sesión; autenticación por token. | AT-E6 |

Las metas NFR-06..08 se convertirán en umbrales firmes solo tras medir en el equipo de referencia (ADR-008).
