# caddy-tui

[![tests](https://github.com/mgoreiro/caddy-tui/actions/workflows/tests.yml/badge.svg)](https://github.com/mgoreiro/caddy-tui/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/mgoreiro/caddy-tui)](https://github.com/mgoreiro/caddy-tui/releases/latest)
[![license](https://img.shields.io/github/license/mgoreiro/caddy-tui)](LICENSE)

Interfaz de terminal (TUI) para gestionar un **Caddy que corre en Docker**: editar su
configuración con seguridad, recargarla sin cortes, añadir servicios con un asistente,
ver el tráfico en vivo y diagnosticar los problemas típicos.

> [English version](README.md)

Sin dependencias: solo **Python 3.8+** (librería estándar, `curses`) y **Docker**.

**Contenido:** [Instalación](#instalación) · [¿Aún no tienes Caddy?](#aún-no-tienes-caddy-caddy-tui-lo-instala) · [Llevar la configuración a otro servidor](#llevar-la-configuración-a-otro-servidor---export----import) · [Uso](#uso) · [Estadísticas](#estadísticas-de-tráfico) · [Varios Caddy](#varios-caddy-en-la-misma-máquina) · [Idioma](#idioma) · [Funciones](#funciones) · [Monitorización y alertas](#monitorización-y-alertas---check) · [Desarrollo](#desarrollo) · [Documentación](#documentación)

## Instalación

En Debian, Ubuntu, Raspberry Pi OS o Armbian, añade una sola vez el repositorio apt firmado y `apt upgrade` mantendrá caddy-tui al día:

```bash
curl -fsSL https://mgoreiro.github.io/caddy-tui/apt/caddy-tui.gpg | sudo tee /usr/share/keyrings/caddy-tui.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/caddy-tui.gpg] https://mgoreiro.github.io/caddy-tui/apt stable main" | sudo tee /etc/apt/sources.list.d/caddy-tui.list
sudo apt update && sudo apt install caddy-tui
sudo caddy-tui
```

La huella de la clave es `BA7E 2381 6880 1639 21BC  70DE 15C2 632D D390 10EE` (compruébala con `gpg --show-keys --with-fingerprint /usr/share/keyrings/caddy-tui.gpg`).
El paquete no depende de la arquitectura, así que sirve para cualquier CPU (x86-64, placas ARM…).

**Guía de instalación completa** — qué hace cada comando, el fichero `.deb`, ejecutarlo desde el código, actualizar, actualizaciones automáticas, desinstalar y solución de problemas:
[docs/install.es.md](docs/install.es.md).

## ¿Aún no tienes Caddy? caddy-tui lo instala

En una máquina sin Caddy (o incluso sin Docker) basta con ejecutarlo:

```bash
sudo caddy-tui --install        # o simplemente `caddy-tui`: lo ofrece cuando no encuentra ningún contenedor de Caddy
```

El instalador guiado:

1. **Instala Docker** si falta (sistemas con apt, como root y preguntando antes; elige `docker.io` más un paquete de Docker Compose v2 cuando la distribución lo tiene — Debian 12 no, y entonces Caddy se ejecuta como un contenedor simple).
2. Revisa la máquina: un **Caddy fuera de Docker** ya en marcha, **puertos 80/443 ocupados** (y por qué; ofrece 8080/8443), si la carpeta está libre y si tu dominio resuelve.
3. Pregunta la **carpeta de configuración**, tu primer **dominio** y **dónde escucha su aplicación** (ambos opcionales) y un email para los avisos de Let's Encrypt.
4. Escribe un `Caddyfile` y un `docker-compose.yml` (imagen `caddy:2-alpine`, volúmenes para los certificados, `host.docker.internal` disponible), **valida la configuración antes de tocar el destino**, arranca Caddy y comprueba que se mantiene en marcha.
5. Abre la interfaz ya conectada a él; después ejecuta `sudo caddy-tui --setup-alerts` si quieres alertas por Telegram.

Si Docker está instalado pero no se puede usar, lo dice en vez de ofrecer un Caddy nuevo: *sin permiso* → ejecútalo con `sudo` (o entra en el grupo `docker`); *servicio parado* → ofrece arrancarlo (como root con systemd) o muestra `sudo systemctl start docker`.

Nunca sobrescribe un `Caddyfile` / `docker-compose.yml` existente y solo funciona de forma interactiva (`--check` y `--notify` siguen saliendo con código 3 si no hay Caddy).

## Llevar la configuración a otro servidor (`--export` / `--import`)

```bash
caddy-tui --export                      # o `o` en la interfaz: escribe caddy-tui-export-<host>-<fecha>.tar.gz
caddy-tui --export salida.tar.gz --with-certs --with-alerts
sudo caddy-tui --import salida.tar.gz   # en la máquina nueva (instala Docker si hace falta)
```

La exportación contiene tu `Caddyfile`, el `docker-compose.yml`, el `.env` (y, si lo pides, los **certificados y la cuenta ACME** con `--with-certs` —contienen claves privadas— y los **ajustes de alertas** con `--with-alerts`), más un manifiesto con el SHA-256 de cada fichero. El fichero se crea legible solo por ti.

`--import` verifica las sumas de comprobación y rechaza cualquier cosa inesperada dentro del archivo, valida el Caddyfile **antes de escribir nada**, restaura todo en una carpeta nueva (nunca sobre una configuración existente), devuelve los certificados al volumen de datos del contenedor nuevo —así se **sirven los mismos certificados** y no se vuelve a pedir nada a Let's Encrypt—, arranca Caddy y abre la interfaz. Con `-y/--yes` no hace preguntas. Si la exportación traía ajustes de alertas, ejecuta después `sudo caddy-tui --setup-alerts` para recrear la comprobación periódica.

## Uso

Autodetecta el contenedor
de Caddy y de él deduce la imagen, el `Caddyfile` montado y el proyecto/servicio de
`docker compose`. Si hay varios contenedores te deja elegir. Puedes forzar cualquier valor:

| Opción | Variable | Para qué |
|--------|----------|----------|
| `--container` | `CADDY_CONTAINER` | contenedor de Caddy |
| `--caddyfile` | `CADDY_CADDYFILE` | ruta del Caddyfile en el host |
| `--dir` | `CADDY_DIR` | carpeta del proyecto (compose, `.env`, backups) |
| `--compose` | `CADDY_COMPOSE` | fichero docker compose |
| `--service` | `CADDY_SERVICE` | servicio de Caddy en el compose |
| `--image` | `CADDY_IMAGE` | imagen usada para validar/formatear |

Si el `Caddyfile` pertenece a otro usuario (por ejemplo `/root/...`), ejecútalo con `sudo`.

## Varios Caddy en la misma máquina

Si hay más de un contenedor de Caddy, caddy-tui los encuentra todos (`caddy-tui --list` los muestra con su estado, imagen y Caddyfile):

- **Interactivo**: al arrancar eliges uno (el último que usaste es el predeterminado, basta pulsar Enter); dentro del programa la cabecera muestra `2/3 contenedores de Caddy · [S] cambiar` y **`S`** cambia a otro sin salir. Todo —diagnóstico, asistente, baneos, copias, certificados, estadísticas— pasa a trabajar sobre ese Caddy, con su propio `Caddyfile`, proyecto de compose y `.env`.
- **Sin interfaz**: `--check`, `--notify` y `--auto-block` aceptan **`--all`** para ejecutarse en todos. La salida va agrupada (`== nombre ==`), el código de salida es el del peor, `--json` devuelve `{"status": …, "containers": […]}` y los mensajes de Telegram se etiquetan `equipo/contenedor`.
- **Alertas**: `sudo caddy-tui --setup-alerts` pregunta si quieres vigilarlos todos; si es así el timer se ejecuta con `--all`.
- El estado de las alertas y los baneos de escáneres se guardan **por contenedor**, así que un baneo en un Caddy no afecta a otro.

## Idioma

La interfaz está en **inglés** y **español**. Al arrancar se elige, por este orden:

1. `--lang es|en|auto` (`auto` ignora la preferencia guardada),
2. la variable `CADDY_TUI_LANG`,
3. la preferencia guardada en `~/.config/caddy-tui/config.json`,
4. el idioma del sistema (`LANGUAGE`, `LC_ALL`, `LC_MESSAGES`, `LANG`),
5. inglés.

Dentro del programa, la tecla **`L`** cambia de idioma al instante y lo guarda.

## Funciones

| Tecla | Acción |
|-------|--------|
| `a` | **Asistente para añadir un servicio**: eliges el tipo de sitio — *proxy inverso* (dominio, destino, HTTPS al destino, gzip, cabeceras de seguridad, log de acceso, usuario/contraseña opcional), *proxy inverso con rutas* (`/api` → una aplicación, `/static` → otra, el resto → una por defecto; con o sin quitar el prefijo), *redirección* (p. ej. `www` → dominio principal, permanente o temporal, conservando ruta y parámetros) o *página de mantenimiento* (mensaje fijo con un 503). Genera el bloque, lo valida, muestra una vista previa y lo añade. `m` edita los bloques de cualquier tipo con los mismos formularios |
| `m` | **Gestionar servicios**: lista los sitios del Caddyfile y permite **editar** (con el mismo formulario del asistente si el bloque es de los que el asistente podría haber escrito; si no, en tu editor, solo ese bloque) o **eliminar** uno. Todo se valida en el contexto del fichero completo y con copia previa |
| `i` | **IPs bloqueadas**: bloquea una IP o un rango (CIDR) en todos los sitios, desbloquea, revisa direcciones sospechosas (ver *Banear escáneres*) o reaplica la lista a todos los sitios. Escribe un snippet y un `import` como primera línea de cada sitio, validado y con copia previa como cualquier cambio; las conexiones bloqueadas se cortan (`abort`). Se rechazan loopback y rangos más amplios que /8, y una dirección privada (LAN) pide una confirmación extra. En el monitor de tráfico, `b` te deja elegir una de las direcciones más activas y bloquearla. Los sitios nuevos o editados conservan la protección automáticamente |
| `y` | **Actualizar la imagen de Caddy**: compara el digest de la imagen que usa el contenedor con el que sirve el registro para la misma etiqueta (Docker Hub, GHCR, Quay…, de forma anónima) y ofrece **descargarla y recrear Caddy**. Antes de recrear valida tu Caddyfile con la nueva versión, así que una versión incompatible nunca tumba el sitio; además te indica a qué imagen volver. Detecta una imagen ya descargada pero sin aplicar. El diagnóstico avisa cuando hay una actualización (resultado en caché 6 h) |
| `w` | **Estadísticas de tráfico** de las últimas 24 h / 7 / 30 / 90 días (ver *Estadísticas de tráfico*) |
| `p` | **Probar servicios**: hace una petición a cada sitio con nombre del Caddyfile e indica el estado HTTP, el tiempo de respuesta y la validez del certificado. Dos comprobaciones por sitio: *local* (directa al Caddy de esta máquina con el nombre del sitio, verificando el certificado) y *pública* (por DNS como un cliente; si falla solo es un aviso porque puede ser hairpin NAT). El diagnóstico (y con él `--check` y las alertas) hace también la local, así que un `502` o un certificado caducado generan alerta |
| `t` | **Monitor de tráfico** en vivo: peticiones/s, códigos 2xx–5xx, latencia, gráfica, top dominios e IPs y últimas peticiones (`p` pausa, `w` ventana 1/5/15 min). Requiere `log` en el sitio |
| `d` | **Diagnóstico y reparación** (ver abajo) |
| `h` | **Certificados HTTPS**: nombres, emisor y caducidad de cada certificado que Caddy tiene guardado; avisa si uno está a punto de caducar (Caddy debería haberlo renovado), si un dominio aún no tiene o si Let's Encrypt/ACME está fallando (mostrando el error). El diagnóstico incluye las mismas comprobaciones |
| `e` | Editar el Caddyfile: se valida antes de guardar, con copia previa |
| `v` / `l` | Validar / recargar en caliente (sin cortes, validando antes) |
| `s` / `x` / `r` | Arrancar o aplicar / parar / reiniciar |
| `g` | Ver logs |
| `f` | Formatear el Caddyfile |
| `u` | **Deshacer el último cambio**: muestra el diff con la última versión anterior y la restaura (haciendo antes copia de la actual, así que pulsar `u` otra vez lo rehace) |
| `k` / `b` | Crear / restaurar un backup (`Caddyfile` + `.env` en `.caddy-backups/`); la lista indica la antigüedad de cada uno y cuántas líneas cambiarían, y ves el diff antes de confirmar |
| `n` | Editar `.env` |
| `S` | Cambiar a otro contenedor de Caddy (si hay varios) |
| `L` | Cambiar de idioma (English / Español) |

### Qué comprueba el diagnóstico

- Docker disponible y variables `{$VAR}` del Caddyfile definidas.
- Caddyfile válido (con pista específica para `import` sin snippet).
- Que el contenedor use el **mismo Caddyfile que el disco** (un editor que reemplaza el
  fichero deja el bind-mount de fichero único apuntando al antiguo).
- **Puertos** de los sitios sin publicar en el contenedor.
- **Destinos inalcanzables**: otra red Docker (detecta cuál), `host.docker.internal` sin
  `extra_hosts`, conexión rechazada, timeout.
- Errores recientes en los logs, agrupados.
- **Que cada sitio responda** (comprobado desde esta máquina, por HTTPS verificando el certificado): los `5xx`, los timeouts y los problemas de certificado son errores. Los sitios con la CA interna de Caddy (`localhost`, `*.local`…) dan un aviso por el certificado pero se comprueba igualmente su estado.

Cada problema explica su solución y la TUI ofrece aplicarla: parchea el `docker-compose`
(puertos, `extra_hosts`, redes externas; idempotente, validado con `docker compose config`
y con copia previa) y recrea solo Caddy con `--no-deps`. Si no puede escribir en el
compose, te muestra el fragmento YAML para pegarlo a mano.

## Estadísticas de tráfico

`w` (o `caddy-tui --stats [24h|7d|30d|90d]`) muestra cómo se han usado tus sitios **a lo largo del tiempo**, no solo los últimos minutos como el monitor en vivo:
peticiones, tráfico enviado, latencia media y máxima, reparto de 2xx/3xx/4xx/5xx, la hora más activa, una barra por hora (últimas 24 h) o por día, una línea por
sitio (peticiones, porcentaje, latencia, tasas de error, tráfico) y las direcciones de cliente más activas.

- **Cómo funciona**: `caddy-tui --collect-stats` incorpora el log de accesos de Caddy **desde la última ejecución** a una pequeña base de datos SQLite
  (`/var/lib/caddy-tui/stats.sqlite3`, o `~/.local/state/caddy-tui/` si no se puede escribir; modo 600 porque contiene direcciones de clientes).
  Al abrir la pantalla se recoge primero lo nuevo. `sudo caddy-tui --setup-alerts` ofrece ejecutar la recogida en cada comprobación, lo que mantiene completo el historial
  aunque se recree el contenedor (Docker descarta los logs de un contenedor al recrearlo).
- **Qué se guarda**: totales por hora y sitio durante **90 días** y recuentos diarios por dirección de cliente durante **30 días**; nada más (ni URLs ni user agents).
  La latencia es la media y el máximo (no percentiles). Necesita la directiva `log` en los sitios.
- **Para scripts**: `--stats 7d --json`; `--all` para todos los Caddy; cada contenedor tiene su propio historial.

## Monitorización y alertas (`--check`)

`caddy-tui --check` ejecuta el mismo diagnóstico una sola vez, sin interfaz, y termina. Es de solo
lectura, así que se puede programar sin riesgo:

| Código de salida | Significado |
|------------------|-------------|
| `0` | todo correcto |
| `1` | solo avisos |
| `2` | al menos un problema |
| `3` | no se pudo ejecutar (sin Docker, contenedor o Caddyfile no encontrados, varios contenedores sin elegir) |

La comprobación periódica **no crea ni destruye contenedores**: valida la configuración dentro del propio Caddy en marcha (`docker exec`), así que las herramientas que vigilan los eventos de Docker —como docker-controller-bot— no avisan de nada por ella. Solo se usa un contenedor desechable cuando Caddy no está en marcha, cuando validas un candidato que necesita variables del `.env` o cuando importa ficheros por ruta relativa.

Con `--quiet` imprime solo avisos y problemas (nada si todo va bien) y con `--json` una salida para
máquinas (`status`, `problems`, `warnings`, `checks[]`). Los códigos siguen la convención de
Nagios/Icinga, así que sirve también como plugin de monitorización.

### Banear escáneres (`--auto-block`)

Las direcciones públicas que se comportan como escáneres o intentan fuerza bruta se pueden banear —a mano o automáticamente, al estilo fail2ban—:

- **Qué es sospechoso** (mirando el log de accesos reciente): al menos **3 rutas típicas de escáner distintas** (`/.env`, `/.git`, `/wp-login.php`, `/phpmyadmin`, `/xmlrpc.php`…), o **≥ 90 % de peticiones fallidas** (4xx) con **30+ peticiones**, o **10+ accesos fallidos** (401/403) con 2 aciertos como mucho. Nunca se consideran direcciones privadas/LAN/loopback, y puedes eximir direcciones o rangos con `AUTOBLOCK_ALLOW=1.2.3.4,5.6.7.0/24` en `/etc/caddy-tui/alert.env`.
- **A mano**: en **IPs bloqueadas** (`i`) elige *Revisar direcciones sospechosas…* (últimos 30 minutos) y banea una durante 24 h, 7 días o para siempre; en el **monitor de tráfico** (`b`) las sospechosas salen primero, marcadas con ⚠ y el motivo. La lista muestra por qué y cuándo se levanta cada baneo, y al abrir la pantalla se levantan los caducados.
- **Automáticamente**: `caddy-tui --auto-block` hace una pasada (`--window 15m`, `--ban-time 24h`, `--dry-run` para simular): banea a los infractores (20 por pasada como máximo; deja de añadir si la lista pasa de 500), levanta los baneos caducados y avisa por Telegram si está configurado. `sudo caddy-tui --setup-alerts` pregunta si quieres ejecutarlo en cada comprobación.
- El baneo es la misma lista de bloqueo de antes (`abort` en todos los sitios), así que se valida, se copia y se recarga en caliente; cuando se levanta el último, el Caddyfile queda exactamente como estaba.

### Alertas por Telegram (`--setup-alerts`)

```bash
sudo caddy-tui --setup-alerts
```

Una configuración guiada (necesita root) que:

1. detecta el contenedor de Caddy,
2. pide el **token del bot de Telegram** (créalo con [@BotFather](https://t.me/BotFather)) y lo comprueba,
3. encuentra tu **id de chat**: escribe cualquier mensaje al bot y pulsa Enter,
4. pregunta el **intervalo de comprobación** (5 min – 24 h, por defecto 1 h) y si los avisos también deben notificar,
5. guarda la configuración en `/etc/caddy-tui/alert.env` (modo 600), instala un **servicio + timer de
   systemd** (o una tarea en `/etc/cron.d` si no hay systemd) que ejecuta
   `caddy-tui --check --quiet --notify`, y
6. envía un mensaje de prueba y hace una primera comprobación.

Las alertas no molestan: un problema se avisa una vez (otra vez si cambia, o a las 24 h si persiste) y
recibes un "Caddy vuelve a estar bien" cuando se recupera. `caddy-tui --check --notify` también sirve a mano
o desde tu propio cron. Para deshacerlo todo: `sudo caddy-tui --remove-alerts`.

## Notas de diseño

- La validación escribe el candidato junto al Caddyfile y monta esa carpeta en un
  contenedor desechable, así los `import` a ficheros relativos funcionan y no se toca el
  Caddy en marcha.
- El Caddyfile se guarda sobre el mismo inode (no se reemplaza el fichero) para no romper
  bind-mounts de fichero único.
- Los backups completos incluyen `.env` (con posibles secretos): se crean con permisos 600.

## Desarrollo

```bash
make test        # compila los catálogos y ejecuta las pruebas
make pot         # re-extrae las cadenas traducibles (po/caddy-tui.pot)
make update-po   # sincroniza los .po con el .pot
```

**Añadir un idioma:** `cp po/caddy-tui.pot po/xx.po`, rellena las traducciones (cabecera `Language: xx`), `make mo` y listo; las pruebas verifican que no falte ni sobre ninguna cadena.

**Paquete Debian:** `dpkg-buildpackage -us -uc -b` (necesita `debhelper dh-python gettext python3`). Al publicar una release en GitHub, una acción construye el `.deb` y lo adjunta.

**Repositorio apt (mantenimiento):** el procedimiento completo está en [docs/apt-repository.es.md](docs/apt-repository.es.md). En resumen: al publicar una release de GitHub se construye el `.deb` y además se añade al repositorio firmado de la rama `gh-pages` (`tools/update-apt-repo.sh`, clave en el secreto `APT_GPG_PRIVATE_KEY`, clave pública `apt/caddy-tui.gpg`). La clave caduca en 2029-10; para renovarla o rotarla, genera una nueva, actualiza el secreto, publica una release y pide a los usuarios que reinstalen `caddy-tui.gpg`. `tests/apt_repo_smoke.sh` comprueba el script en el CI.

## Documentación

| | |
|---|---|
| [docs/install.es.md](docs/install.es.md) | Instalar y actualizar: repositorio apt, `.deb`, desde el código; solución de problemas |
| [docs/apt-repository.es.md](docs/apt-repository.es.md) | Mantenimiento: cómo se construye el repositorio apt, publicar una release, rotar la clave |
| `man caddy-tui` · `caddy-tui --help` | Opciones y teclas del programa instalado |
| [Releases](https://github.com/mgoreiro/caddy-tui/releases) | Qué cambia en cada versión (y los ficheros `.deb`) |

## Licencia

MIT
