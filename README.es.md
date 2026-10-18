# caddy-tui

Interfaz de terminal (TUI) para gestionar un **Caddy que corre en Docker**: editar su
configuración con seguridad, recargarla sin cortes, añadir servicios con un asistente,
ver el tráfico en vivo y diagnosticar los problemas típicos.

> [English version](README.md)

Sin dependencias: solo **Python 3.8+** (librería estándar, `curses`) y **Docker**.

## Instalación

Descarga el paquete `.deb` de la [última release](https://github.com/mgoreiro/caddy-tui/releases/latest) e instálalo (Debian, Ubuntu, Raspberry Pi OS, Armbian…; sirve para cualquier arquitectura):

```bash
sudo apt install ./caddy-tui_*_all.deb
caddy-tui
```

También puedes ejecutarlo sin instalar: es un único fichero Python (`./caddy-tui.py`; para tener el español desde el repositorio ejecuta antes `make mo`).

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
| `a` | **Asistente para añadir un servicio**: formulario (dominio, destino, HTTPS al destino, gzip, cabeceras de seguridad, log, usuario/contraseña opcional). Genera el bloque, lo valida, muestra vista previa y lo añade |
| `m` | **Gestionar servicios**: lista los sitios del Caddyfile y permite **editar** (con el mismo formulario del asistente si el bloque es de los que el asistente podría haber escrito; si no, en tu editor, solo ese bloque) o **eliminar** uno. Todo se valida en el contexto del fichero completo y con copia previa |
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

Cada problema explica su solución y la TUI ofrece aplicarla: parchea el `docker-compose`
(puertos, `extra_hosts`, redes externas; idempotente, validado con `docker compose config`
y con copia previa) y recrea solo Caddy con `--no-deps`. Si no puede escribir en el
compose, te muestra el fragmento YAML para pegarlo a mano.

## Monitorización y alertas (`--check`)

`caddy-tui --check` ejecuta el mismo diagnóstico una sola vez, sin interfaz, y termina. Es de solo
lectura, así que se puede programar sin riesgo:

| Código de salida | Significado |
|------------------|-------------|
| `0` | todo correcto |
| `1` | solo avisos |
| `2` | al menos un problema |
| `3` | no se pudo ejecutar (sin Docker, contenedor o Caddyfile no encontrados, varios contenedores sin elegir) |

Con `--quiet` imprime solo avisos y problemas (nada si todo va bien) y con `--json` una salida para
máquinas (`status`, `problems`, `warnings`, `checks[]`). Los códigos siguen la convención de
Nagios/Icinga, así que sirve también como plugin de monitorización.

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

## Licencia

MIT
