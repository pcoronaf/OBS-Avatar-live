# Publicar una versión

Las versiones se publican con el workflow **Build Windows Installer**
(`.github/workflows/build-installer.yaml`). Siempre crea un **borrador** de *Release*: nada se publica
hasta que un mantenedor pulsa **Publish release** en GitHub.

1. **Subir la versión** en un PR: `buildspec.json` (`version`), `engine/pyproject.toml` y
   `engine/avatar_engine/__init__.py`. Fusionar el PR en `main` con todas las comprobaciones en verde.
2. **Crear el borrador**, de una de dos formas:
   - *Actions → Build Windows Installer → Run workflow*, rama `main`, campo **version** = `0.1.2`
     (sin "v"). Lo puede lanzar también Claude Code desde una sesión (no necesita permisos de push).
   - o empujar una etiqueta: `git tag -a v0.1.2 origin/main -m "Avatar Live 0.1.2" && git push origin v0.1.2`.
3. El workflow compila el plugin y el motor, ejecuta el autodiagnóstico, instala el instalador en
   silencio y comprueba la instalación; después crea el borrador `v0.1.2` con
   `AvatarLive-Setup-0.1.2-Lite.exe` y su `.sha256`. Si la versión no coincide con `buildspec.json`,
   se detiene sin crear nada.
4. **Revisar y publicar** el borrador en *Releases*. Con la ejecución manual, la etiqueta `v0.1.2` se
   crea sobre el commit compilado en el momento de publicar.

Si el borrador ya existe (p. ej. se repite la ejecución), el workflow sustituye sus archivos en lugar de
fallar.
