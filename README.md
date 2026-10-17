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
| `a` | **Add-service wizard**: form (domain, upstream, HTTPS upstream, gzip, security headers, access log, optional user/password). Generates the block, validates it, shows a preview and appends it |
| `t` | **Live traffic monitor**: requests/s, 2xx–5xx codes, latency, graph, top domains and IPs, latest requests (`p` pause, `w` window 1/5/15 min). Needs `log` in the site |
| `d` | **Diagnostics and repair** (see below) |
| `h` | **HTTPS certificates**: names, issuer and expiry of every certificate Caddy has stored; warns when one is about to expire (Caddy should have renewed it), when a domain has none yet, or when Let's Encrypt/ACME is failing (showing the error). The diagnostics include the same checks |
| `e` | Edit the Caddyfile: validated before saving, with a previous backup |
| `v` / `l` | Validate / hot reload (validates first) |
| `s` / `x` / `r` | Start or apply / stop / restart |
| `g` | View logs |
| `f` | Format the Caddyfile |
| `c` | View the Caddyfile |
| `k` / `b` | Create / restore a backup (`Caddyfile` + `.env` in `.caddy-backups/`) |
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
