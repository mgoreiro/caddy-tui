# Instalar caddy-tui

[English](install.md) · [Volver al README](../README.es.md)

caddy-tui es un único programa en Python 3. La forma **recomendada** de instalarlo en sistemas basados en Debian es su propio
**repositorio apt firmado**: lo añades una vez y, a partir de ahí, `apt upgrade` mantiene caddy-tui al día como cualquier otro paquete.

- [Requisitos](#requisitos)
- [Opción 1 · repositorio apt (recomendada)](#opción-1--repositorio-apt-recomendada)
- [Opción 2 · el fichero `.deb`](#opción-2--el-fichero-deb)
- [Opción 3 · ejecutarlo desde el código](#opción-3--ejecutarlo-desde-el-código)
- [Después de instalar](#después-de-instalar)
- [Actualizar](#actualizar)
- [Desinstalar](#desinstalar)
- [Solución de problemas](#solución-de-problemas)
- [Referencia de la clave y del repositorio](#referencia-de-la-clave-y-del-repositorio)

## Requisitos

| | |
|---|---|
| Sistema | Debian, Ubuntu, Raspberry Pi OS, Armbian o cualquier Linux con apt. Cualquier arquitectura de CPU: el paquete es `all` (es solo Python). |
| Python | 3.8 o superior (`python3` se instala como dependencia). No hace falta ninguna librería de Python. |
| Docker | Un Docker que funcione (`docker.io` o el `docker-ce` de Docker). El paquete depende de `docker.io \| docker-ce-cli`, así que apt instala `docker.io` solo si no tienes ninguno. |
| Privilegios | `sudo` para instalar. Para *usarlo* necesitas acceso al socket de Docker y al `Caddyfile` (suele ser de root): ejecútalo con `sudo` o pertenece al grupo `docker`. |

> **¿Aún no tienes Docker ni Caddy?** No hace falta preparar nada: `sudo caddy-tui --install` instala Docker si falta y monta Caddy por ti
> (ver [¿Aún no tienes Caddy?](../README.es.md#aún-no-tienes-caddy-caddy-tui-lo-instala)).

## Opción 1 · repositorio apt (recomendada)

El repositorio está en `https://mgoreiro.github.io/caddy-tui/apt` (suite `stable`, componente `main`) y está **firmado con una clave GPG**, así que
apt rechaza cualquier cosa que no venga de él.

### Paso 1 — instalar la clave de firma

```bash
curl -fsSL https://mgoreiro.github.io/caddy-tui/apt/caddy-tui.gpg | sudo tee /usr/share/keyrings/caddy-tui.gpg >/dev/null
```

Descarga la clave **pública** del repositorio y la guarda en `/usr/share/keyrings/`. (Usar un fichero de claves propio en vez del antiguo `apt-key`
hace que esa clave solo se considere de confianza para *este* repositorio.)

Opcional pero recomendable — comprueba que es la clave correcta:

```bash
gpg --show-keys --with-fingerprint /usr/share/keyrings/caddy-tui.gpg
```

La huella debe ser **`BA7E 2381 6880 1639 21BC  70DE 15C2 632D D390 10EE`**.

### Paso 2 — añadir el repositorio

```bash
echo "deb [signed-by=/usr/share/keyrings/caddy-tui.gpg] https://mgoreiro.github.io/caddy-tui/apt stable main" | sudo tee /etc/apt/sources.list.d/caddy-tui.list
```

Qué dice la línea: *una fuente de paquetes binarios (`deb`), cuyo índice debe estar firmado con la clave de `/usr/share/keyrings/caddy-tui.gpg`, en esa URL,
suite `stable`, componente `main`.*

<details>
<summary>¿Prefieres el formato moderno deb822 (<code>.sources</code>)?</summary>

Es lo mismo, en el formato que usan las versiones recientes de apt (Debian 12+, Ubuntu 22.04+). Usa **este o bien** el fichero `.list` de arriba, no los dos:

```bash
sudo tee /etc/apt/sources.list.d/caddy-tui.sources >/dev/null <<'EOT'
Types: deb
URIs: https://mgoreiro.github.io/caddy-tui/apt
Suites: stable
Components: main
Signed-By: /usr/share/keyrings/caddy-tui.gpg
EOT
```
</details>

### Paso 3 — instalar

```bash
sudo apt update
sudo apt install caddy-tui
```

`apt update` debe mostrar una línea `Get: … https://mgoreiro.github.io/caddy-tui/apt stable InRelease` **sin errores**. Después:

```bash
caddy-tui --version        # caddy-tui 1.x.y
sudo caddy-tui             # abre la interfaz (ver "Después de instalar")
```

Para confirmar que apt ve el repositorio y todas las versiones disponibles:

```bash
apt-cache policy caddy-tui     # muestra la versión candidata y de dónde viene
apt list -a caddy-tui          # todas las versiones publicadas
```

### Probado en

Debian 12 *bookworm* y Ubuntu 24.04, en arm64 (el índice también se ve desde amd64 y armhf). El paquete no depende de la arquitectura.

## Opción 2 · el fichero `.deb`

Cada [release](https://github.com/mgoreiro/caddy-tui/releases) incluye el paquete y un fichero `SHA256SUMS`. Sirve para máquinas que no llegan al
repositorio, o para instalar una versión concreta:

```bash
# descarga los dos ficheros de la página de la release y después:
sha256sum -c SHA256SUMS --ignore-missing      # debe decir: caddy-tui_X.Y.Z_all.deb: OK
sudo apt install ./caddy-tui_X.Y.Z_all.deb    # apt resuelve las dependencias
```

Un paquete instalado así **no** se actualiza con `apt upgrade` (apt no sabe de dónde vino). Añade el repositorio (Opción 1) para recibir actualizaciones.

## Opción 3 · ejecutarlo desde el código

caddy-tui es un solo fichero, `caddy-tui.py`, sin dependencias de terceros. Útil en sistemas sin apt o en macOS con Docker Desktop:

```bash
git clone https://github.com/mgoreiro/caddy-tui.git
cd caddy-tui
make mo               # compila la traducción al español (necesita msgfmt de gettext); sin ello la interfaz solo está en inglés
./caddy-tui.py        # o: python3 caddy-tui.py
```

Para instalarlo a mano: `sudo make install` (pone el programa en `/usr/bin`, la traducción en `/usr/share/locale` y el manual en `/usr/share/man/man1`;
con `PREFIX=/usr/local` cambia el destino).

## Después de instalar

```bash
sudo caddy-tui                  # lo habitual: el Caddyfile suele ser de root
caddy-tui --help                # todas las opciones
man caddy-tui                   # la página de manual
caddy-tui --lang es             # interfaz en español (o pulsa L dentro del programa)
```

- **Detecta tu contenedor de Caddy él solo** (y su Caddyfile, imagen y proyecto de docker compose). En [Uso](../README.es.md#uso) está cómo forzar cualquier valor.
- Qué instala el paquete: `dpkg -L caddy-tui` — el programa (`/usr/bin/caddy-tui`), el manual, el catálogo en español y la documentación.
- Qué crea después: `~/.config/caddy-tui/` (preferencia de idioma), `.caddy-backups/` junto a tu configuración (copias de seguridad) y, si activas las
  alertas, `/etc/caddy-tui/` y `/var/lib/caddy-tui/` (donde también está la base de estadísticas de tráfico, `stats.sqlite3`, y los baneos de escáneres).

## Actualizar

Con el repositorio configurado (Opción 1):

```bash
sudo apt update && sudo apt upgrade                  # todo, incluido caddy-tui
sudo apt install --only-upgrade caddy-tui            # solo caddy-tui
sudo apt install caddy-tui=1.7.0                     # una versión concreta (apt list -a caddy-tui las muestra)
sudo apt-mark hold caddy-tui                         # que no se actualice (apt-mark unhold caddy-tui lo deshace)
```

**Actualizaciones automáticas** con `unattended-upgrades`: permite el origen del repositorio añadiendo un fichero como `/etc/apt/apt.conf.d/52caddy-tui`:

```
Unattended-Upgrade::Origins-Pattern {
        "origin=caddy-tui,codename=stable";
};
```

caddy-tui también avisa cuando hay una **imagen de Caddy** más nueva (tecla `y`); es independiente de las actualizaciones de este paquete.

## Desinstalar

```bash
sudo apt remove caddy-tui            # quita el programa
sudo apt purge caddy-tui             # igual, más los ficheros de configuración del paquete (no tiene)
sudo rm /etc/apt/sources.list.d/caddy-tui.list /usr/share/keyrings/caddy-tui.gpg      # dejar de seguir el repositorio
```

Tu Caddy, su configuración y sus datos **no se tocan**. Los ficheros que creó caddy-tui se borran a mano si quieres: `~/.config/caddy-tui`,
las carpetas `.caddy-backups/` y, si usaste alertas, antes `sudo caddy-tui --remove-alerts` (quita el timer/cron) y luego `/etc/caddy-tui` y `/var/lib/caddy-tui`.

## Solución de problemas

| Lo que ves | Causa y arreglo |
|---|---|
| `NO_PUBKEY 15C2632DD39010EE` · `The repository … is not signed` | Falta la clave o la ruta de `signed-by=` es incorrecta. Repite el paso 1 y comprueba `ls -l /usr/share/keyrings/caddy-tui.gpg` (no debe estar vacío: si `curl` falló, el fichero queda vacío). |
| `EXPKEYSIG` · `The key has expired` | La clave de firma caduca el 17/10/2029 (o se renovó). Repite el paso 1 para bajar la clave actual. |
| `Conflicting values set for option Signed-By` | El repositorio está configurado dos veces (un `.list` **y** un `.sources`). Deja solo uno. |
| `E: Unable to locate package caddy-tui` | Falta `sudo apt update` o la línea de la fuente es incorrecta. `apt-cache policy caddy-tui` debería listar el repositorio. |
| `404 Not Found` al actualizar | Un error en la URL. Debe ser exactamente `https://mgoreiro.github.io/caddy-tui/apt` seguida de `stable main`. |
| `Release file … is not valid yet (invalid for another …)` | El reloj de la máquina está mal (típico en placas sin reloj de tiempo real tras un corte de luz). Arregla la hora: `sudo timedatectl set-ntp true`, espera un momento y `sudo apt update`. |
| `E: The repository 'http://… Release' does not have a Release file` de **otro** repositorio (una fuente de terceros como `packages.azlux.fr`) | No es caddy-tui: otro fichero de `/etc/apt/sources.list.d/` apunta a una suite que no existe para tu distribución (p. ej. una entrada `noble` en un sistema que no es Ubuntu 24.04). apt lo muestra como error y `apt update` termina con código distinto de cero, pero nuestro repositorio sí se actualiza. Búscalo con `grep -rl azlux /etc/apt/` y corrige o elimina ese fichero. |
| `N: Skipping acquire of configured file 'main/binary-armhf/Packages' … doesn't support architecture 'armhf'` | Un aviso inofensivo en equipos con varias arquitecturas habilitadas (`dpkg --print-foreign-architectures`). Se corrigió en el repositorio el 18/10/2026 (el fichero `Release` ahora declara las arquitecturas habituales): vuelve a ejecutar `sudo apt update` y desaparece. No hay que instalar ni quitar nada. |
| `Could not resolve 'mgoreiro.github.io'` | Sin DNS/Internet. Detrás de un proxy, añade `Acquire::https::Proxy "http://host:puerto";` en un fichero de `/etc/apt/apt.conf.d/`. |
| `unmet dependencies … docker.io` en Ubuntu | `docker.io` está en el repositorio *universe*: `sudo add-apt-repository universe && sudo apt update`. |
| `No tienes permiso para usar Docker` al ejecutarlo | Usa `sudo caddy-tui`, o añade tu usuario al grupo `docker` (`sudo usermod -aG docker $USER` y vuelve a iniciar sesión). |
| `Docker está instalado pero su servicio no está en marcha` | `sudo systemctl enable --now docker` (caddy-tui ofrece hacerlo si lo ejecutas como root). |

¿Sigues atascado? Abre un [issue](https://github.com/mgoreiro/caddy-tui/issues) con la salida de `apt-cache policy caddy-tui` y de `sudo apt update`.

## Referencia de la clave y del repositorio

| | |
|---|---|
| URL del repositorio | `https://mgoreiro.github.io/caddy-tui/apt` |
| Suite · componente · arquitectura | `stable` · `main` · `all` |
| Clave pública | `https://mgoreiro.github.io/caddy-tui/apt/caddy-tui.gpg` (binaria, lista para `signed-by=`) |
| Huella | `BA7E 2381 6880 1639 21BC  70DE 15C2 632D D390 10EE` (RSA 4096, caduca el 17/10/2029) |
| Origin / label (para pinning de apt y unattended-upgrades) | `caddy-tui` |
| Página web | <https://mgoreiro.github.io/caddy-tui/> |

Cómo se construye el repositorio y cómo publicar una release: [apt-repository.es.md](apt-repository.es.md).
