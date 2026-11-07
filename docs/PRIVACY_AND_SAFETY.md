# Privacidad y seguridad

## Uso previsto
Representar a **la propia persona** (o a alguien que ha dado su consentimiento verificable) con un avatar
**claramente etiquetado como sintético** en reuniones. No es una herramienta de prueba de presencia.

## Usos no admitidos
- Superar pruebas de vida (liveness), KYC, verificación biométrica o controles de identidad.
- Suplantar o engañar sobre la identidad de alguien.
- Usar fotos de terceros sin consentimiento.

El diseño refuerza esto: etiqueta no desactivable, sin modo "sin etiqueta", sin inyección en cámaras distintas de
OBS Virtual Camera y sin ocultar congelaciones.

## Flujo de consentimiento
1. Al crear un perfil se exige `rights_confirmed = true`.
2. Se declara si la persona de la foto es el usuario (`subject_is_self`).
3. Si no lo es, se exige una referencia de consentimiento del tercero (`third_party_consent_reference`).
4. El registro de consentimiento (fecha, declaración, hash de la foto) se guarda junto al perfil, localmente.
5. Sin consentimiento, el motor rechaza `create_profile` y `start_session`.

## Etiqueta «Avatar generado por IA»
- El motor la graba en **cada** cuadro antes de escribirlo en el ring; los cuadros llevan además la marca
  `synthetic_label` y el plugin rechaza los que no la tienen.
- Se puede elegir esquina y escala (accesibilidad); el texto puede traducirse, no vaciarse.

## Datos
| Dato | Dónde | Persistencia |
|---|---|---|
| Foto y perfil | `%LOCALAPPDATA%\AvatarLive\profiles\` (ACL del usuario) | Hasta que el usuario lo borra |
| Audio del micrófono | Memoria (búfer corto) | Nunca se guarda por defecto |
| Video de webcam | Memoria | Nunca se guarda por defecto |
| Benchmarks | `benchmarks\` | Sin imágenes; solo tiempos y huella de hardware |
| Logs | `logs\` | Sin rostros, audio ni embeddings |

- Sin telemetría ni conexiones de red durante el uso local.
- Borrado: Dock / propiedades → "Eliminar perfil", `avatar-engine profile delete <id>`, o la opción del desinstalador
  "Eliminar mis perfiles locales".

## Seguridad del sistema
- Named pipe con `PIPE_REJECT_REMOTE_CLIENTS`, DACL del usuario + SYSTEM y token por sesión en variable de entorno.
- Memoria compartida en el espacio `Local\` de la sesión.
- Modelos verificados por `sha256` contra el manifiesto cuando el hash está auditado.
- Sin ejecución remota; sin drivers de cámara adicionales. Firma de binarios cuando sea viable.
