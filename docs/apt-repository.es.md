# El repositorio apt y cómo se publican las versiones

[English](apt-repository.md) · [Instalar caddy-tui](install.es.md) · [Volver al README](../README.es.md)

*Para quien mantiene el proyecto.* Los usuarios solo necesitan [install.es.md](install.es.md).

## Visión general

```
 tag / release de GitHub ──► workflow "release" ──┬─► construye y revisa el .deb ─► se adjunta a la release (+ SHA256SUMS)
                                                  └─► tools/update-apt-repo.sh ─► rama gh-pages ─► GitHub Pages
                                                         (firma con el secreto APT_GPG_PRIVATE_KEY)   https://mgoreiro.github.io/caddy-tui/apt
```

El repositorio es un **árbol estático de ficheros** —sin servidor ni base de datos—, así que vive en git y lo sirve GitHub Pages.

| Pieza | Dónde |
|---|---|
| Script de publicación | [`tools/update-apt-repo.sh`](../tools/update-apt-repo.sh) |
| Workflow | [`.github/workflows/release.yml`](../.github/workflows/release.yml) |
| Prueba rápida del script | [`tests/apt_repo_smoke.sh`](../tests/apt_repo_smoke.sh) (job `apt-repo` del CI) |
| El árbol publicado | rama **`gh-pages`**, carpeta `apt/` |
| Clave de firma | privada: secreto del repositorio **`APT_GPG_PRIVATE_KEY`** (+ la copia de seguridad del propietario) · pública: `apt/caddy-tui.gpg` |
| Pages | Settings → Pages: *Deploy from a branch* → `gh-pages` `/ (root)` |

Estructura de la rama `gh-pages`:

```
index.html  .nojekyll                      página de entrada con los tres comandos de instalación
apt/
  caddy-tui.gpg                            clave pública (binaria, para signed-by=)
  pool/main/c/caddy-tui/caddy-tui_X.Y.Z_all.deb     se conservan todas las versiones publicadas
  dists/stable/
    Release  Release.gpg  InRelease        índice de índices, firma separada y firma en claro
    main/binary-all/Packages  Packages.gz
```

Los paquetes son `Architecture: all`, así que un único índice `binary-all` sirve a cualquier CPU (comprobado desde clientes arm64, amd64 y armhf).
El `Release` declara `Origin: caddy-tui` y `Suite`/`Codename: stable`, que es lo que usan los patrones de `unattended-upgrades` y el pinning de apt.

## Qué hace el workflow de release

Se dispara al **publicar** una release de GitHub (o a mano, ver más abajo):

1. Comprueba que el tag, `__version__` de `caddy-tui.py` y la primera entrada de `debian/changelog` son la misma versión.
2. Construye el paquete (`dpkg-buildpackage`) y pasa `lintian --fail-on error`.
3. Sube `caddy-tui_X.Y.Z_all.deb` y `SHA256SUMS` a la release.
4. Obtiene `tools/` de `main` (así también se pueden republicar tags antiguos) y ejecuta `update-apt-repo.sh`, que: copia el `.deb` al pool,
   regenera `Packages(.gz)` y `Release` con `apt-ftparchive`, firma `Release` (`Release.gpg` + `InRelease`) con la clave importada en un llavero desechable
   y vuelve a exportar la clave pública.
5. Hace commit del resultado en `gh-pages` y lo sube. Pages se reconstruye en uno o dos minutos.

Las ejecuciones van de una en una (`concurrency: apt-repository`) porque cada una reescribe la rama. Si el secreto no está definido, los pasos 4–5 se saltan con un aviso.

## Publicar una versión nueva

1. Elige la versión `X.Y.Z` y actualiza **los tres sitios**: `__version__` en `caddy-tui.py`, una entrada nueva al principio de `debian/changelog`
   (misma versión; fecha con `date -R`) y la versión en la línea `.TH` de `man/caddy-tui.1`.
2. `make test` (los tests también comprueban que las traducciones estén completas) y merge a `main` con el CI en verde.
3. Crea la release **apuntando al hash completo del commit** (GitHub rechaza un hash abreviado):

   ```bash
   gh release create vX.Y.Z --target "$(git rev-parse HEAD)" --title "vX.Y.Z" --notes "…"
   ```
4. Vigílalo: `gh run list --workflow release` → success; `gh release view vX.Y.Z --json assets`.
5. Verifica el repositorio (ver abajo). En un cliente con el repositorio configurado, `sudo apt update && sudo apt upgrade` debe ofrecer la versión nueva
   (Pages puede tardar un par de minutos: `curl -s https://raw.githubusercontent.com/mgoreiro/caddy-tui/gh-pages/apt/dists/stable/main/binary-all/Packages | grep ^Version` muestra la rama al instante).

### Republicar una versión existente

```bash
gh workflow run release.yml -f tag=vX.Y.Z
```

Reconstruye el paquete desde ese tag, **reemplaza los ficheros de la release** y lo republica en el repositorio. El `.deb` reconstruido tiene el mismo contenido
pero distinta suma de comprobación (el archivo no es idéntico bit a bit), y `SHA256SUMS` se actualiza en consecuencia.

## Verificar el repositorio publicado

Desde cualquier Debian/Ubuntu limpio (basta un contenedor) — son exactamente los pasos del usuario:

```bash
docker run --rm -it debian:bookworm bash
apt-get update && apt-get install -y curl ca-certificates gnupg
curl -fsSL https://mgoreiro.github.io/caddy-tui/apt/caddy-tui.gpg -o /usr/share/keyrings/caddy-tui.gpg
gpg --show-keys --with-fingerprint /usr/share/keyrings/caddy-tui.gpg          # BA7E 2381 6880 1639 21BC  70DE 15C2 632D D390 10EE
echo "deb [signed-by=/usr/share/keyrings/caddy-tui.gpg] https://mgoreiro.github.io/caddy-tui/apt stable main" > /etc/apt/sources.list.d/caddy-tui.list
apt-get update && apt-cache policy caddy-tui && apt-get install -y caddy-tui && caddy-tui --version
```

## Publicar a mano (sin CI)

Necesita la copia de la clave, `apt-utils` y `gnupg`:

```bash
git clone --branch gh-pages https://github.com/mgoreiro/caddy-tui.git /tmp/site
APT_GPG_PRIVATE_KEY="$(cat caddy-tui-apt-PRIVATE.asc)" tools/update-apt-repo.sh /tmp/site/apt ruta/a/caddy-tui_X.Y.Z_all.deb
cd /tmp/site && git add -A && git commit -m "caddy-tui X.Y.Z" && git push origin gh-pages
```

### Retirar una versión

Borra su `.deb` de `apt/pool/main/c/caddy-tui/` en un clon de `gh-pages` y vuelve a ejecutar el script pasándole **una copia de un `.deb` que no esté en la
carpeta del pool** (el script necesita al menos un paquete como argumento; copia el más reciente a `/tmp` y pásalo), haz commit y push.

## La clave de firma

| | |
|---|---|
| Algoritmo · caducidad | RSA 4096 · **17/10/2029** |
| Huella | `BA7E 2381 6880 1639 21BC  70DE 15C2 632D D390 10EE` |
| Clave privada | secreto `APT_GPG_PRIVATE_KEY` (armada, sin contraseña) y la copia de seguridad fuera de línea del propietario — **nunca** en un commit |
| Nombre / email de la clave | `caddy-tui apt repository <caddy-tui-apt@users.noreply.github.com>` |

**Generar una clave nueva** (en un contenedor desechable, para no dejar nada en el equipo):

```bash
mkdir -m 700 out && docker run --rm -v "$PWD/out:/out" debian:bookworm bash -c '
  set -euo pipefail; apt-get update -qq && apt-get install -y -qq gnupg
  export GNUPGHOME=/tmp/g; mkdir -m 700 $GNUPGHOME
  printf "%%no-protection\nKey-Type: RSA\nKey-Length: 4096\nKey-Usage: sign\nName-Real: caddy-tui apt repository\nName-Email: caddy-tui-apt@users.noreply.github.com\nExpire-Date: 3y\n%%commit\n" > /tmp/p
  gpg --batch --gen-key /tmp/p
  gpg --armor --export-secret-keys "caddy-tui apt repository" > /out/caddy-tui-apt-PRIVATE.asc
  gpg --export "caddy-tui apt repository" > /out/caddy-tui.gpg'
gh secret set APT_GPG_PRIVATE_KEY --repo mgoreiro/caddy-tui < out/caddy-tui-apt-PRIVATE.asc
```

**Rotar o renovar** (antes de que caduque, o si se filtra):
1. Genera la clave nueva como arriba y actualiza el secreto.
2. Publica una release (o `gh workflow run release.yml -f tag=<última>`): el script vuelve a firmarlo todo y sustituye `apt/caddy-tui.gpg` por la clave pública nueva.
3. Actualiza la huella y la caducidad en [install.md](install.md), [install.es.md](install.es.md), los README y el `index.html` de `gh-pages`.
4. Avisa a los usuarios de que repitan el paso 1 de la instalación (`curl … | sudo tee /usr/share/keyrings/caddy-tui.gpg`). Hasta que lo hagan, `apt update` falla con
   `NO_PUBKEY`/`EXPKEYSIG` — para eso sirve firmar. Si la clave antigua se filtró, dilo en las notas de la release.

## Notas de seguridad

- El secreto solo se expone a las ejecuciones disparadas por `release` y `workflow_dispatch` en este repositorio; los pull requests (incluidos los de forks) nunca lo ven.
  Mantenlo así: no añadas disparadores como `pull_request_target` que ejecuten código de un PR con secretos.
- Quien pueda hacer push a `gh-pages` o leer el secreto puede publicar paquetes en los que confían todos los clientes. Limita quién tiene escritura en el repositorio.
- Los clientes confían en la clave solo para este repositorio (`signed-by=`), no para todo el sistema.

## Si falla una publicación

| Síntoma | Arreglo |
|---|---|
| `Release.target_commitish is invalid` al crear la release | Usa el hash **completo** del commit con `--target`. |
| El workflow terminó bien pero los clientes no ven la versión | Pages aún se está construyendo (≈1–2 min); mira la URL raw de la rama indicada arriba. |
| `git push … gh-pages` rechazado | Dos ejecuciones se solaparon; relanza la que falló (`gh run rerun`). |
| El paso "Publish to the apt repository" dice que el secreto no está definido | Vuelve a crearlo con `gh secret set APT_GPG_PRIVATE_KEY …`. |
| `gpg: can't connect to the gpg-agent` / `File name too long` | El script ya usa un llavero con ruta corta en `/tmp`; si cambias `GNUPGHOME`, mantén la ruta corta. |
| `apt update` en un cliente: `Hash Sum mismatch` justo tras publicar | Pages/CDN sirve durante un minuto una mezcla de ficheros viejos y nuevos; reintenta. |
