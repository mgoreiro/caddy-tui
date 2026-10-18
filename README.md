# caddy-tui

A terminal UI (TUI) to manage a **Caddy server running in Docker**: edit its configuration
safely, reload it without downtime, add services with a wizard, watch live traffic and
diagnose the usual problems.

> [Versión en español](README.es.md)

No dependencies: just **Python 3.8+** (standard library, `curses`) and **Docker**.

## Install

Download the `.deb` from the [latest release](https://github.com/mgoreiro/caddy-tui/releases/latest)
and install it (Debian, Ubuntu, Raspberry Pi OS, Armbian… — the package is architecture independent):

```bash
sudo apt install ./caddy-tui_*_all.deb
caddy-tui
```

You can also run it without installing: it is a single Python file (`./caddy-tui.py`; run
`make mo` first if you want the Spanish translation from a checkout).

## No Caddy yet? Let caddy-tui set it up

On a machine without Caddy (or even without Docker) just run it:

```bash
sudo caddy-tui --install        # or simply `caddy-tui`: it offers it when it finds no Caddy container
```

The guided installer:

1. **Installs Docker** if it is missing (apt-based systems, as root, after asking; it picks `docker.io` plus a Docker Compose v2 package when the distribution has one — Debian 12 does not, then Caddy runs as a plain container).
2. Checks the machine: a **non-Docker Caddy** already running, **ports 80/443 in use** (and by what; it offers 8080/8443), whether the folder is free and whether your domain resolves.
3. Asks the **configuration folder**, your first **domain** and **where its application listens** (both optional) and an email for Let's Encrypt notices.
4. Writes a `Caddyfile` and a `docker-compose.yml` (image `caddy:2-alpine`, volumes for certificates, `host.docker.internal` available), **validates the configuration before touching the destination**, starts Caddy and checks that it stays up.
5. Opens the interface already attached to it; run `sudo caddy-tui --setup-alerts` afterwards if you want Telegram alerts.

If Docker is installed but cannot be used, it says so instead of offering a new Caddy: *no permission* → run it with `sudo` (or join the `docker` group); *service stopped* → it offers to start it (as root on systemd) or shows `sudo systemctl start docker`.

It never overwrites an existing `Caddyfile` / `docker-compose.yml`, and it only runs interactively (`--check` and `--notify` keep exiting with code 3 when there is no Caddy).

## Moving to another server (`--export` / `--import`)

```bash
caddy-tui --export                      # or `o` in the interface: writes caddy-tui-export-<host>-<date>.tar.gz
caddy-tui --export out.tar.gz --with-certs --with-alerts
sudo caddy-tui --import out.tar.gz      # on the new machine (installs Docker if needed)
```

The export holds your `Caddyfile`, the `docker-compose.yml`, the `.env` (and, if you ask, the **certificates and ACME account** with `--with-certs` — they contain private keys — and the **alerts settings** with `--with-alerts`), plus a manifest with a SHA-256 of each file. The file is created readable only by you.

`--import` verifies the checksums and refuses anything unexpected in the archive, validates the Caddyfile **before writing anything**, restores everything into a new folder (never over an existing configuration), puts the certificates back into the new container's data volume — so the **same certificates are served** and Let's Encrypt is not asked again — starts Caddy and opens the interface. Add `-y/--yes` to skip the questions. If the export had alerts settings, run `sudo caddy-tui --setup-alerts` afterwards to recreate the periodic check.

## Usage

It autodetects the Caddy container and, from it, the image, the mounted `Caddyfile` and the
`docker compose` project/service. If there are several containers you get to choose. Any value
can be forced:

| Option | Variable | Purpose |
|--------|----------|---------|
| `--container` | `CADDY_CONTAINER` | Caddy container |
| `--caddyfile` | `CADDY_CADDYFILE` | Caddyfile path on the host |
| `--dir` | `CADDY_DIR` | project folder (compose, `.env`, backups) |
| `--compose` | `CADDY_COMPOSE` | docker compose file |
| `--service` | `CADDY_SERVICE` | Caddy service in the compose file |
| `--image` | `CADDY_IMAGE` | image used to validate/format |
| `--lang` | `CADDY_TUI_LANG` | interface language |
| `--check`, `--json`, `-q` | | headless diagnostics (see *Monitoring and alerts*) |
| `--setup-alerts`, `--remove-alerts`, `--notify`, `--interval` | | Telegram alerts and periodic check |

If the `Caddyfile` belongs to another user (e.g. under `/root/`), run it with `sudo`.

## Language

The interface is available in **English** and **Spanish**. At startup the language is chosen in
this order:

1. `--lang en|es|auto` (`auto` ignores the saved preference),
2. the `CADDY_TUI_LANG` variable,
3. the preference saved in `~/.config/caddy-tui/config.json`,
4. the system language (`LANGUAGE`, `LC_ALL`, `LC_MESSAGES`, `LANG`),
5. English.

Inside the program, press **`L`** to switch language instantly; the choice is saved.

## Features

| Key | Action |
|-----|--------|
| `a` | **Add-service wizard**: pick the kind of site — *reverse proxy* (domain, upstream, HTTPS upstream, gzip, security headers, access log, optional user/password), *reverse proxy with paths* (`/api` → one app, `/static` → another, the rest → a default; with or without stripping the prefix), *redirect* (e.g. `www` → apex, permanent or temporary, keeping the path and query) or *maintenance page* (fixed message with a 503). Generates the block, validates it, shows a preview and appends it. `m` edits the blocks of every kind with the same forms |
| `m` | **Manage services**: lists the sites in the Caddyfile and lets you **edit** (with the same form as the wizard when the block is one the wizard could have written, otherwise in your editor, only that block) or **remove** one. Everything is validated in the context of the whole file and backed up first |
| `i` | **Blocked IPs**: block an IP or a range (CIDR) on every site, unblock, review suspicious addresses (see *Banning scanners*), or re-apply the list to all sites. It writes a snippet plus an `import` as the first line of each site, validated and backed up like any other change; blocked connections are dropped (`abort`). Loopback and ranges wider than /8 are refused, and a private (LAN) address asks for an extra confirmation. In the traffic monitor press `b` to pick one of the busiest addresses and block it. New or edited sites keep the protection automatically |
| `y` | **Update the Caddy image**: compares the digest of the image the container runs with the one the registry serves for the same tag (Docker Hub, GHCR, Quay…, anonymous), and offers to **pull it and recreate Caddy**. Before recreating it validates your Caddyfile with the new version, so an incompatible release never takes the site down; it also tells you which image to go back to. Detects an image that is already pulled but not applied. The diagnostics warn when an update is available (result cached for 6 h) |
| `p` | **Probe services**: requests every named site of the Caddyfile and reports the HTTP status, response time and certificate validity. Two checks per site: *local* (straight to this machine's Caddy with the site's name, certificate verified) and *public* (through DNS like a client; a failure here is only a warning because it can be hairpin NAT). The diagnostics (and so `--check` and the alerts) run the local check too, so a `502` or an expired certificate raises an alert |
| `t` | **Live traffic monitor**: requests/s, 2xx–5xx codes, latency, graph, top domains and IPs, latest requests (`p` pause, `w` window 1/5/15 min). Needs `log` in the site |
| `d` | **Diagnostics and repair** (see below) |
| `h` | **HTTPS certificates**: names, issuer and expiry of every certificate Caddy has stored; warns when one is about to expire (Caddy should have renewed it), when a domain has none yet, or when Let's Encrypt/ACME is failing (showing the error). The diagnostics include the same checks |
| `e` | Edit the Caddyfile: validated before saving, with a previous backup |
| `v` / `l` | Validate / hot reload (validates first) |
| `s` / `x` / `r` | Start or apply / stop / restart |
| `g` | View logs |
| `f` | Format the Caddyfile |
| `c` | View the Caddyfile |
| `u` | **Undo the last change**: shows the diff against the latest earlier version and restores it (backing up the current one first, so pressing `u` again redoes it) |
| `k` / `b` | Create / restore a backup (`Caddyfile` + `.env` in `.caddy-backups/`); the list shows how old each one is and how many lines would change, and you see the diff before confirming |
| `n` | Edit `.env` |
| `L` | Change language (English / Español) |
| `q` | Quit |

### What the diagnostics check

- Docker available, and the `{$VAR}` placeholders of the Caddyfile defined.
- Caddyfile valid (with a specific hint for an `import` without a snippet).
- The container uses the **same Caddyfile as the disk** (an editor that replaces the file leaves
  a single-file bind mount pointing at the old one).
- Site **ports** not published by the container.
- **Unreachable upstreams**: another Docker network (it finds which one), `host.docker.internal`
  without `extra_hosts`, connection refused, timeout.
- Recent errors in the logs, grouped.
- **Every site answers** (checked from this machine, over HTTPS with the certificate verified): `5xx`, timeouts and certificate problems are errors. Sites on Caddy's internal CA (`localhost`, `*.local`…) are reported as a warning about the certificate but their status is still checked.

Each problem comes with its fix and the TUI offers to apply it: it patches the `docker-compose`
file (ports, `extra_hosts`, external networks; idempotent, checked with `docker compose config`,
backup first) and recreates only Caddy with `--no-deps`. If it cannot write the compose file,
it shows the YAML to paste by hand.

## Monitoring and alerts (`--check`)

`caddy-tui --check` runs the same diagnostics once, without the interface, and exits. It is
read-only, so it is safe to schedule:

| Exit code | Meaning |
|-----------|---------|
| `0` | everything is fine |
| `1` | warnings only |
| `2` | at least one problem |
| `3` | could not run (no Docker, container or Caddyfile not found, several containers and none chosen) |

Add `--quiet` to print only warnings and problems (nothing if all is well) and `--json` for
machine-readable output (`status`, `problems`, `warnings`, `checks[]`). These exit codes follow the
Nagios/Icinga convention, so it also works as a monitoring plugin.

### Banning scanners (`--auto-block`)

Public addresses that look like scanners or brute-forcers can be banned — by hand or automatically, fail2ban-style:

- **What counts as suspicious** (looked at in the recent access log): at least **3 different typical scanner paths** (`/.env`, `/.git`, `/wp-login.php`, `/phpmyadmin`, `/xmlrpc.php`…), or **≥ 90 % failed requests** (4xx) over **30+ requests**, or **10+ failed logins** (401/403) with at most 2 successes. Private/LAN/loopback addresses are never considered, and you can exempt addresses or ranges with `AUTOBLOCK_ALLOW=1.2.3.4,5.6.7.0/24` in `/etc/caddy-tui/alert.env`.
- **By hand**: in **Blocked IPs** (`i`) choose *Review suspicious addresses…* (last 30 minutes) and ban one for 24 h, 7 days or permanently; in the **traffic monitor** (`b`) the suspicious addresses are listed first, marked with ⚠ and the reason. The list shows why and when each ban lifts, and expired bans are lifted when you open the screen.
- **Automatically**: `caddy-tui --auto-block` does one pass (`--window 15m`, `--ban-time 24h`, `--dry-run` to preview): bans the offenders (at most 20 per pass; it stops adding if the list passes 500), lifts expired bans, and tells you on Telegram if configured. `sudo caddy-tui --setup-alerts` asks whether to run it on every check.
- The ban is the same blocklist as before (`abort` on every site), so it is validated, backed up and hot-reloaded; when the last ban lifts, the Caddyfile is back to exactly how it was.

### Telegram alerts (`--setup-alerts`)

```bash
sudo caddy-tui --setup-alerts
```

A guided setup (it needs root) that:

1. detects the Caddy container,
2. asks for the **Telegram bot token** (create a bot with [@BotFather](https://t.me/BotFather)) and checks it,
3. finds your **chat id** for you: write any message to the bot and press Enter,
4. asks the **check interval** (5 min – 24 h, default 1 h) and whether warnings should also notify,
5. saves the settings in `/etc/caddy-tui/alert.env` (mode 600), installs a **systemd service + timer**
   (or a `/etc/cron.d` job if there is no systemd) running `caddy-tui --check --quiet --notify`, and
6. sends a test message and runs a first check.

Alerts are not noisy: a problem is reported once (again if it changes, or after 24 h if it persists),
and you get a "Caddy is OK again" message when it recovers. `caddy-tui --check --notify` can also be
used by hand or from your own cron job. Undo everything with `sudo caddy-tui --remove-alerts`.

## Design notes

- Validation writes the candidate next to the Caddyfile and mounts that folder in a throwaway
  container, so relative `import`s work and the running Caddy is never touched.
- The Caddyfile is saved over the same inode (never replaced) so single-file bind mounts keep
  working.
- Full backups include `.env` (possible secrets): they are created with mode 600.

## Development

```bash
make test        # compile the catalogs and run the tests
make pot         # re-extract translatable strings (po/caddy-tui.pot)
make update-po   # sync the .po files with the .pot
```

**Adding a language:** `cp po/caddy-tui.pot po/xx.po`, fill in the translations (header
`Language: xx`), run `make mo`. The tests check that no string is missing or obsolete and that the
`{placeholders}` match.

**Debian package:** `dpkg-buildpackage -us -uc -b` (needs `debhelper dh-python gettext python3`).
Publishing a GitHub release triggers a workflow that builds the `.deb` and attaches it.

## License

MIT
