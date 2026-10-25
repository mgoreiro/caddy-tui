#!/usr/bin/env python3
"""
caddy-tui — terminal UI (TUI) to manage a Caddy running in Docker.

No dependencies: only Python 3 (standard library, curses) and Docker.

    caddy-tui                       # autodetects the Caddy container
    caddy-tui --container my-caddy  # or say which one

Features: status, guided diagnostics and repair of common problems, a wizard to
add services, live traffic monitor, Caddyfile editing (validated before saving,
with backup), validate, format, start, stop, restart, hot reload, logs, and
backup/restore.

Everything is deduced from the container (image, mounted Caddyfile, docker
compose project and service); any value can be forced with options or variables:
    --container / CADDY_CONTAINER   Caddy container
    --caddyfile / CADDY_CADDYFILE   Caddyfile path on the host
    --dir       / CADDY_DIR         project folder (compose, .env, backups)
    --compose   / CADDY_COMPOSE     docker compose file
    --service   / CADDY_SERVICE     Caddy service in the compose file
    --image     / CADDY_IMAGE       image used to validate/format
    --lang      / CADDY_TUI_LANG    interface language (auto, en, es, ...)

Headless mode for cron / systemd / monitoring (read-only):
    caddy-tui --check [--quiet] [--json]   exit 0 ok, 1 warnings, 2 problems, 3 cannot run
    caddy-tui --check --notify             also alerts through Telegram
    sudo caddy-tui --setup-alerts          guided setup: Telegram bot + systemd timer (or cron)
    EDITOR                          editor to use (default nano / vi)
"""
import argparse
import concurrent.futures
import curses
import difflib
import getpass
import gettext
import hashlib
import io
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from datetime import datetime, timezone

__version__ = "1.10.0"

# Filled in by configure() from the options and the container.
BASE = os.getcwd()          # project folder: compose, .env, backups
CONTAINER = ""
SERVICE = "caddy"
COMPOSE = ""                # empty if the container was not created with docker compose
IMAGE = "caddy:2-alpine"
CADDYFILE = ""
CADDYDIR = ""
ENVFILE = ""
BACKUPS = ""
CONF_IN_CONTAINER = "/etc/caddy/Caddyfile"

# ---------------------------------------------------------------- i18n -----
APP = "caddy-tui"
LANG_NAMES = {"en": "English", "es": "Español"}
LANG = "en"
_TR = gettext.NullTranslations()


def _(message):
    """Translate a message into the current interface language."""
    return _TR.gettext(message)


def N_(message):
    """Mark a message for extraction; translated later with _() at display time."""
    return message


def _locale_roots():
    return [os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale"),
            os.path.join(sys.base_prefix, "share", "locale"), "/usr/share/locale", "/usr/local/share/locale"]


def available_languages():
    langs = {"en"}
    for root in _locale_roots():
        try:
            for d in os.listdir(root):
                if os.path.isfile(os.path.join(root, d, "LC_MESSAGES", APP + ".mo")):
                    langs.add(d)
        except OSError:
            pass
    return sorted(langs, key=lambda c: (c != "en", c))


def set_language(lang):
    """Activate `lang` (falls back to English). Returns the language in use."""
    global LANG, _TR
    LANG, _TR = "en", gettext.NullTranslations()
    if lang != "en":
        for root in _locale_roots():
            try:
                _TR, LANG = gettext.translation(APP, localedir=root, languages=[lang]), lang
                break
            except OSError:
                continue
    return LANG


def _config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP, "config.json")


def load_saved_lang():
    try:
        with open(_config_path(), encoding="utf-8") as f:
            return json.load(f).get("lang")
    except (OSError, ValueError, AttributeError):
        return None


def load_config():
    try:
        with open(_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_config(**values):
    """Merge `values` into ~/.config/caddy-tui/config.json (language, last Caddy used...)."""
    try:
        data = load_config()
        data.update(values)
        os.makedirs(os.path.dirname(_config_path()), exist_ok=True)
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def save_lang(lang):
    update_config(lang=lang)


def system_lang():
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for part in (os.environ.get(var) or "").split(":"):
            code = part.split(".")[0].split("_")[0].lower()
            if code and code not in ("c", "posix"):
                return code
    return "en"


def pick_language(cli=None):
    """Order: --lang > CADDY_TUI_LANG > saved preference > system locale > English.
    '--lang auto' ignores the saved preference."""
    avail = available_languages()
    cands = [cli, os.environ.get("CADDY_TUI_LANG"), None if cli == "auto" else load_saved_lang()]
    for c in cands:
        if c and c != "auto" and c in avail:
            return c
    sl = system_lang()
    return sl if sl in avail else "en"


def state_label(state):
    return {"running": _("running"), "exited": _("stopped"), "created": _("created"), "paused": _("paused"),
            "restarting": _("restarting"), "dead": _("dead"), "missing": _("not found"),
            "nodocker": _("Docker unavailable")}.get(state, state)


MENU = [
    ("a", N_("Add service (wizard)")),
    ("m", N_("Manage services")),
    ("t", N_("Traffic monitor")),
    ("i", N_("Blocked IPs")),
    ("d", N_("Diagnostics & repair")),
    ("h", N_("HTTPS certificates")),
    ("p", N_("Probe services")),
    ("w", N_("Traffic statistics")),
    ("y", N_("Update Caddy image")),
    ("e", N_("Edit Caddyfile")),
    ("v", N_("Validate configuration")),
    ("l", N_("Reload (no downtime)")),
    ("s", N_("Start / apply")),
    ("x", N_("Stop")),
    ("r", N_("Restart")),
    ("g", N_("View logs")),
    ("f", N_("Format Caddyfile")),
    ("c", N_("View Caddyfile")),
    ("k", N_("Create backup (Caddyfile+.env)")),
    ("o", N_("Export configuration")),
    ("u", N_("Undo last change")),
    ("b", N_("Restore backup")),
    ("n", N_("Edit .env")),
    ("q", N_("Quit")),
    ("S", N_("Switch Caddy")),
    ("L", "Language / Idioma"),
]


# ----------------------------------------------------------------- helpers ---
def run(cmd, stdin=None, timeout=120, cwd=None):
    """Run a command and return (exit code, combined output)."""
    try:
        p = subprocess.run(
            cmd, cwd=cwd or BASE, input=stdin, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, (p.stdout + p.stderr).rstrip()
    except FileNotFoundError:
        return 127, _("Command not found: {cmd}").format(cmd=cmd[0])
    except subprocess.TimeoutExpired:
        return 124, _("Timed out running: {cmd}").format(cmd=" ".join(cmd))


class NoCaddyContainer(SystemExit):
    """No Caddy container found (an interactive run offers to install one)."""


def list_caddy_containers():
    rc, out = run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Image}}|{{.State}}"], timeout=20)
    rows = [l.split("|") for l in out.splitlines() if l.count("|") == 2]
    return [r for r in rows if "caddy" in r[1].lower() or "caddy" in r[0].lower()]


def inspect_container(name):
    rc, out = run(["docker", "inspect", name], timeout=20)
    try:
        return json.loads(out)[0] if rc == 0 else None
    except ValueError:
        return None



# ------------------------------------------------------------- several Caddy ---
CONFIG_GLOBALS = ("BASE", "CONTAINER", "SERVICE", "COMPOSE", "IMAGE", "CADDYFILE", "CADDYDIR", "ENVFILE", "BACKUPS",
                  "CONF_IN_CONTAINER")
MULTI = False       # True while running over several containers (labels the notifications)


def snapshot_config():
    g = globals()
    return {k: g[k] for k in CONFIG_GLOBALS}


def restore_config(snap):
    globals().update(snap)


def configure_other(args, name):
    """Point the program at another Caddy container. -> (True, "") or (False, message); on failure the previous one stays active.
    Options that described the first container (--dir, --caddyfile, --compose...) are not applied to the others."""
    snap = snapshot_config()
    ns = argparse.Namespace(**vars(args))
    ns.container = name
    ns.caddyfile = ns.dir = ns.compose = ns.service = ns.image = None
    try:
        configure(ns)
    except SystemExit as e:
        restore_config(snap)
        return False, str(e.code)
    return True, ""


def run_for_all(args, func, collect=None):
    """Headless: run func(args) for every Caddy container. -> the worst exit code (3 if none was found)."""
    global MULTI
    cands = list_caddy_containers()
    if not cands:
        print(_("No Caddy container found. Specify one with --container NAME."), file=sys.stderr)
        return 3
    MULTI, worst = len(cands) > 1, 0
    try:
        for name, __, __ in cands:
            ok, msg = configure_other(args, name)
            if not ok:
                print(f"== {name} ==\n{msg}", file=sys.stderr)
                worst = max(worst, 3)
                continue
            if MULTI and collect is None:
                print(f"== {name} ==")
            worst = max(worst, func(args))
            if MULTI and collect is None:
                print()
    finally:
        MULTI = False
    return worst


def print_caddies():
    """--list: every Caddy container with its state, image and Caddyfile."""
    rows = list_caddy_containers()
    if not rows:
        print(_("No Caddy container found. Specify one with --container NAME."))
        return 1
    for name, image, state in rows:
        info = inspect_container(name) or {}
        src = host_path(next((m["Source"] for m in info.get("Mounts", []) if m.get("Destination", "").endswith("Caddyfile")), "-"))
        print(f"{name:<24} {state:<10} {image:<28} {src}")
    return 0


def host_path(path):
    """A bind-mount source as Docker reports it -> the path on this machine.
    Docker Desktop (macOS) reports the paths of its VM: /host_mnt/Users/me/x for /Users/me/x."""
    if path and not os.path.exists(path):
        for prefix in ("/host_mnt", "/run/desktop/mnt/host"):
            if path.startswith(prefix + "/") and os.path.exists(path[len(prefix):]):
                return path[len(prefix):]
    return path


def configure(args):
    """Deduce container, Caddyfile, compose project and image; any of them can be forced with options."""
    global BASE, CONTAINER, SERVICE, COMPOSE, IMAGE, CADDYFILE, CADDYDIR, ENVFILE, BACKUPS, CONF_IN_CONTAINER
    name = args.container
    if not name:
        cands = list_caddy_containers()
        if not cands:
            raise NoCaddyContainer(_("No Caddy container found. Specify one with --container NAME."))
        if len(cands) == 1:
            name = cands[0][0]
        elif not sys.stdin.isatty():
            sys.exit(_("There are several Caddy containers; choose one with --container NAME."))
        else:
            print(_("There are several Caddy containers:"))
            saved = load_config().get("container")
            default = next((i for i, row in enumerate(cands, 1) if row[0] == saved), 1)
            for i, (n, img, st) in enumerate(cands, 1):
                print(f"  {i}) {n}  ({img}, {st})" + ("  ←" if i == default else ""))
            try:
                choice = input(_("Choose a number [{default}]: ").format(default=default)).strip()
                name = cands[int(choice or default) - 1][0]
            except (ValueError, IndexError, EOFError):
                sys.exit(_("Invalid selection. Use --container NAME."))
    info = inspect_container(name)
    if not info:
        sys.exit(_("Container '{name}' does not exist.").format(name=name))
    CONTAINER = name
    labels = info["Config"].get("Labels") or {}
    img = info["Config"].get("Image", "")
    IMAGE = args.image or (img if img and not img.startswith("sha256:") else IMAGE)
    SERVICE = args.service or labels.get("com.docker.compose.service") or SERVICE

    src = None
    for m in info.get("Mounts", []):
        if m.get("Type") != "bind":
            continue
        dest = m["Destination"]
        if dest.endswith("Caddyfile"):
            CONF_IN_CONTAINER, src = dest, m["Source"]
        elif dest == "/etc/caddy" and not src:
            src = os.path.join(m["Source"], "Caddyfile")
    CADDYFILE = os.path.abspath(args.caddyfile) if args.caddyfile else (host_path(src) if src else os.path.join(os.getcwd(), "Caddyfile"))
    if not os.path.isfile(CADDYFILE):
        sys.exit(_("Caddyfile not found at {path}.\nIf it belongs to another user, run with sudo; "
                   "or give its path with --caddyfile.").format(path=CADDYFILE))
    if not os.access(CADDYFILE, os.R_OK | os.W_OK):
        sys.exit(_("No read/write permission on {path}. Run with sudo or as another user.").format(path=CADDYFILE))
    CADDYDIR = os.path.dirname(CADDYFILE)

    wd = labels.get("com.docker.compose.project.working_dir")
    BASE = os.path.abspath(args.dir) if args.dir else (wd if wd and os.path.isdir(wd) else CADDYDIR)
    cfg = (labels.get("com.docker.compose.project.config_files") or "").split(",")[0]
    if args.compose:
        COMPOSE = args.compose
    elif cfg and os.path.isfile(cfg):
        COMPOSE = cfg
    elif cfg and os.path.isfile(os.path.join(BASE, os.path.basename(cfg))):
        COMPOSE = os.path.join(BASE, os.path.basename(cfg))
    ENVFILE = os.path.join(BASE, ".env")
    BACKUPS = os.path.join(BASE, ".caddy-backups")


def referenced_vars(text):
    """[(name, has_default)] for the {$VAR} / {$VAR:def} placeholders in the Caddyfile."""
    seen = {}
    for m in re.finditer(r"\{\$([A-Za-z_][A-Za-z0-9_]*)(:[^}]*)?\}", text):
        seen[m.group(1)] = seen.get(m.group(1), False) or m.group(2) is not None
    return sorted(seen.items())


def container_env():
    rc, out = run(["docker", "inspect", "-f", "{{json .Config.Env}}", CONTAINER], timeout=15)
    try:
        return dict(e.split("=", 1) for e in (json.loads(out) or []) if "=" in e) if rc == 0 else {}
    except ValueError:
        return {}


def runtime_env(text):
    """Values of the variables the Caddyfile uses: container first, then .env."""
    cenv, env = container_env(), {}
    for name, has_default in referenced_vars(text):
        val = cenv.get(name) or read_env(name)
        if val:
            env[name] = val
        elif not has_default:
            env[name] = "example.com"  # only so the syntax can be validated
    return env


def compose(*args):
    """Command to manage Caddy; without docker compose, plain `docker` equivalents."""
    if COMPOSE:
        return ["docker", "compose", "-f", COMPOSE, *args]
    if args and args[0] in ("stop", "restart"):
        return ["docker", args[0], CONTAINER]
    if args and args[0] == "up" and "--force-recreate" not in args:
        return ["docker", "start", CONTAINER]
    return ["sh", "-c", 'echo "$1"; exit 1', "sh",
            _("This action needs docker compose (the container was not created with it).")]


def read_env(key):
    try:
        with open(ENVFILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def container_state():
    """Return (state, since): a Docker state, or 'missing' / 'nodocker'."""
    rc, out = run(
        ["docker", "inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}", CONTAINER],
        timeout=10,
    )
    if rc != 0:
        if "docker" in out.lower() and "daemon" in out.lower():
            return "nodocker", ""
        return "missing", ""
    state, __, started = out.partition("|")
    return state, started


def uptime(started):
    try:
        t = datetime.strptime(started[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - t).total_seconds())
    except ValueError:
        return ""
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return (f"{d}d " if d else "") + f"{h}h {m}m"


def _validate_with_throwaway_container(text):
    """Validate Caddyfile text in a throw-away container (docker run --rm).

    The candidate is written as a file next to the Caddyfile and that folder is
    mounted at /etc/caddy, so relative "import" statements work (they do not
    resolve when the config comes from stdin)."""
    cand = os.path.join(CADDYDIR, f".Caddyfile.candidate.{os.getpid()}.{time.monotonic_ns()}")
    try:
        with open(cand, "w", encoding="utf-8") as f:
            f.write(text)
        envs = [x for k, v in runtime_env(text).items() for x in ("-e", f"{k}={v}")]
        cmd = ["docker", "run", "--rm", *envs,
               "-v", f"{CADDYDIR}:/etc/caddy:ro", "-w", "/etc/caddy", IMAGE,
               "caddy", "validate", "--config", f"/etc/caddy/{os.path.basename(cand)}",
               "--adapter", "caddyfile"]
        rc, out = run(cmd, timeout=60)
    except OSError as e:
        return False, _("Could not prepare validation: {err}").format(err=e)
    finally:
        if os.path.exists(cand):
            os.unlink(cand)
    return rc == 0, pretty_logs(out)


def _validate_in_running_container(text):
    """Validate inside the running Caddy container with `docker exec` (no container is created or destroyed).
    -> (ok, output), or (None, "") when this way cannot give an answer (no shell in the image, relative file imports of a candidate...).

    If `text` is exactly the file the container is serving (the usual case of the periodic checks) the container validates that very
    file, so imports and variables behave exactly as they do for the real server."""
    rc, live = run(["docker", "exec", CONTAINER, "cat", CONF_IN_CONTAINER], timeout=15)
    if rc == 0 and live.strip() == text.strip():
        rc, out = run(["docker", "exec", CONTAINER, "caddy", "validate", "--config", CONF_IN_CONTAINER, "--adapter", "caddyfile"], timeout=60)
        return rc == 0, pretty_logs(out)
    script = ('f=$(mktemp /tmp/caddy-tui.XXXXXX) || exit 125; cat > "$f"; '
              'caddy validate --config "$f" --adapter caddyfile; rc=$?; rm -f "$f"; exit $rc')
    rc, out = run(["docker", "exec", "-i", CONTAINER, "sh", "-c", script], stdin=text, timeout=60)
    if rc in (125, 126, 127) or "executable file not found" in out or "File to import not found" in out:
        return None, ""
    return rc == 0, pretty_logs(out)


def validate_text(text):
    """Validate Caddyfile text.

    With the container running this is done *inside it* (`docker exec`): no container is created, so the periodic
    checks do not make Docker emit create/start/die/destroy events (which tools such as docker-controller-bot report).
    A throw-away container is only used when Caddy is not running, when the text uses variables the container does not
    have (they come from the .env), or when it imports files by relative path."""
    if container_state()[0] == "running":
        cenv = container_env()
        if all(name in cenv or has_default for name, has_default in referenced_vars(text)):
            ok, out = _validate_in_running_container(text)
            if ok is not None:
                return ok, out
    return _validate_with_throwaway_container(text)


def pretty_logs(raw):
    """Turn Caddy JSON log lines into readable text; leave anything else as is."""
    lines = []
    for line in raw.splitlines():
        try:
            j = json.loads(line)
            ts = datetime.fromtimestamp(j.pop("ts")).strftime("%H:%M:%S")
            lvl = str(j.pop("level", "")).upper()
            logger = j.pop("logger", "")
            msg = j.pop("msg", "")
            extra = " ".join(f"{k}={v}" for k, v in j.items() if k in ("error", "file", "host", "uri", "status"))
            lines.append(f"{ts} {lvl:5} {logger} {msg} {extra}".rstrip())
        except (ValueError, KeyError, TypeError):
            lines.append(line)
    return "\n".join(lines)


def backup_caddyfile():
    os.makedirs(BACKUPS, exist_ok=True)
    dest = os.path.join(BACKUPS, "Caddyfile." + datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(CADDYFILE, dest)
    return dest


def write_in_place(text):
    """Write over the same inode: a single-file Docker bind mount stops seeing
    changes if the file is replaced by a new one."""
    with open(CADDYFILE, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.truncate()
        f.write(text)


# ------------------------------------------------------------ history / undo ---
def read_backups():
    """Caddyfile versions kept in .caddy-backups, newest first: [{name, path, kind, mtime}]."""
    out = []
    if not os.path.isdir(BACKUPS):
        return out
    for name in sorted(os.listdir(BACKUPS), reverse=True):
        if name.startswith("Caddyfile."):
            kind = "caddyfile"
        elif name.startswith("full-") and name.endswith(".tar.gz"):
            kind = "full"
        else:
            continue                                    # e.g. docker-compose backups
        path = os.path.join(BACKUPS, name)
        out.append({"name": name, "path": path, "kind": kind, "mtime": os.path.getmtime(path)})
    return out


def backup_text(entry):
    """-> (Caddyfile text, .env text or None) of a backup entry."""
    if entry["kind"] == "full":
        with tarfile.open(entry["path"], "r:gz") as tar:
            text = tar.extractfile("Caddyfile").read().decode("utf-8")
            env = tar.extractfile(".env").read().decode("utf-8") if ".env" in tar.getnames() else None
        return text, env
    with open(entry["path"], encoding="utf-8") as f:
        return f.read(), None


def unified_diff(old, new, old_name, new_name):
    return list(difflib.unified_diff(old.splitlines(), new.splitlines(), old_name, new_name, lineterm="", n=2))


def diff_stats(old, new):
    """(added, removed) lines going from `old` to `new`."""
    lines = unified_diff(old, new, "a", "b")
    return (sum(1 for l in lines if l.startswith("+") and not l.startswith("+++")),
            sum(1 for l in lines if l.startswith("-") and not l.startswith("---")))


def ago(ts, now=None):
    secs = max(0, (now if now is not None else time.time()) - ts)
    if secs < 60:
        return _("just now")
    if secs < 3600:
        return _("{n} min ago").format(n=int(secs // 60))
    if secs < 86400:
        return _("{n} h ago").format(n=int(secs // 3600))
    return _("{n} d ago").format(n=int(secs // 86400))


# ----------------------------------------------------------- IP blocklist ---
BLOCK_NAME = "caddy_tui_blocklist"
BLOCK_MATCHER = "caddy_tui_blocked"
BLOCK_MARK = "# caddy-tui: blocked IPs (managed from the [i] screen, do not edit by hand)"


def normalize_network(text):
    """'1.2.3.4' / '198.51.100.0/24' -> canonical text. Raises ValueError for anything unsafe or invalid."""
    net = ipaddress.ip_network(text.strip(), strict=False)
    if net.is_loopback or net.is_unspecified or net.is_multicast:
        raise ValueError(text)
    if net.prefixlen < (8 if net.version == 4 else 32):            # refuse absurdly wide ranges
        raise ValueError(text)
    return str(net.network_address) if net.num_addresses == 1 else str(net)


def get_blocked_ips(text):
    m = re.search(rf"^\s*@{BLOCK_MATCHER}\s+client_ip\s+(.+)$", text, re.M)
    return m.group(1).split() if m else []


def strip_blocklist(text):
    """Remove caddy-tui's blocklist snippet, its marker comment and every `import` of it."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        st = lines[i].strip()
        if st == BLOCK_MARK or st == f"import {BLOCK_NAME}":
            i += 1
        elif st == f"({BLOCK_NAME}) {{":
            depth, i = 1, i + 1
            while i < len(lines) and depth > 0:
                depth += lines[i].count("{") - lines[i].count("}")
                i += 1
            if i < len(lines) and not lines[i].strip():
                i += 1                                   # the blank line that separated it from what follows
        else:
            out.append(lines[i])
            i += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip("\n") + "\n"


def set_blocked_ips(text, ips):
    """Rewrite the blocklist: `import` as the first line of every site, and the snippet defined BEFORE
    the first site (Caddy only resolves an import to a snippet that is already defined)."""
    text = strip_blocklist(text)
    if not ips:
        return text
    lines = text.split("\n")
    blocks = find_site_blocks(text)
    for blk in reversed(blocks):
        lines.insert(blk["start"] + 1, f"\timport {BLOCK_NAME}")
    snippet = [BLOCK_MARK, f"({BLOCK_NAME}) {{", f"\t@{BLOCK_MATCHER} client_ip {' '.join(ips)}",
               f"\tabort @{BLOCK_MATCHER}", "}", ""]
    if not blocks:
        return "\n".join(lines).rstrip("\n") + "\n\n" + "\n".join(snippet[:-1]) + "\n"
    at = blocks[0]["marker"] if blocks[0]["marker"] is not None else blocks[0]["start"]
    while at > 0 and lines[at - 1].strip().startswith("#"):
        at -= 1                                          # keep the site's own comments attached to it
    if at > 0 and lines[at - 1].strip():
        snippet.insert(0, "")
    lines[at:at] = snippet
    return "\n".join(lines)


def sync_blocklist(text):
    """Make sure every site imports the blocklist (new sites, edited blocks)."""
    ips = get_blocked_ips(text)
    return set_blocked_ips(text, ips) if ips else text


def sites_without_blocklist(text):
    lines = text.split("\n")
    return [" ".join(b["addr"]) for b in find_site_blocks(text)
            if not any(l.strip() == f"import {BLOCK_NAME}" for l in lines[b["start"]:b["end"] + 1])]


# ------------------------------------------------- scanners and timed bans ---
SCAN_PATHS = re.compile(
    r"(?i)(/\.env\b|/\.git\b|/\.aws\b|/\.ssh\b|/wp-(login|admin|content|includes)|/xmlrpc\.php|/phpmyadmin|/pma/|"
    r"/phpinfo|/cgi-bin|/boaform|/hnap1|/actuator|/vendor/phpunit|/eval-stdin\.php|/setup\.php|/administrator|"
    r"/server-status|/manager/html|/\.ds_store|\.sql(\.gz)?$|/config\.(php|json|ya?ml)$)")
ERROR_STATUSES = (400, 401, 403, 404, 405)


def parse_span(text):
    """'30m' / '24h' / '7d' / '0' -> seconds (0 = no expiry). Raises ValueError."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhd]?)\s*", text.lower())
    if not m:
        raise ValueError(text)
    return int(m.group(1)) * {"": 60, "m": 60, "h": 3600, "d": 86400}[m.group(2)] if m.group(1) != "0" else 0


def is_public_ip(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified)


def ip_allowed(ip, allow):
    try:
        a = ipaddress.ip_address(ip)
        return any(a in ipaddress.ip_network(n, strict=False) for n in allow)
    except ValueError:
        return False


def allow_list():
    return [x for x in re.split(r"[,\s]+", os.environ.get("AUTOBLOCK_ALLOW", "")) if x]


def find_scanners(events, allow=(), blocked=(), min_requests=30, error_ratio=0.9, auth_fails=10, scan_hits=3):
    """Addresses that look like scanners / brute-forcers. -> [{ip, requests, reasons}] busiest first.
    Reasons: ('probes', n) distinct scanner paths; ('errors', requests, percent) almost only 4xx;
    ('auth', n) repeated 401/403 with (almost) no success. Private addresses and allow-listed ones are never reported."""
    by_ip = {}
    for e in events:
        by_ip.setdefault(e[4], []).append(e)
    out = []
    for ip, ev in by_ip.items():
        if ip in blocked or not is_public_ip(ip) or ip_allowed(ip, allow):
            continue
        n = len(ev)
        errors = sum(1 for e in ev if e[5] in ERROR_STATUSES)
        success = sum(1 for e in ev if 200 <= e[5] < 400)
        fails = sum(1 for e in ev if e[5] in (401, 403))
        probes = {e[3].split("?")[0].lower() for e in ev if SCAN_PATHS.search(e[3])}
        reasons = []
        if len(probes) >= scan_hits:
            reasons.append(("probes", len(probes)))
        if n >= min_requests and errors / n >= error_ratio:
            reasons.append(("errors", n, round(100 * errors / n)))
        if fails >= auth_fails and success <= 2:
            reasons.append(("auth", fails))
        if reasons:
            out.append({"ip": ip, "requests": n, "reasons": reasons})
    return sorted(out, key=lambda o: -o["requests"])


def reason_text(reasons):
    parts = []
    for r in reasons:
        if r[0] == "probes":
            parts.append(_("{n} scanner paths probed").format(n=r[1]))
        elif r[0] == "errors":
            parts.append(_("{pct}% of {n} requests failed").format(pct=r[2], n=r[1]))
        elif r[0] == "auth":
            parts.append(_("{n} failed logins (401/403)").format(n=r[1]))
    return " · ".join(parts)


def bans_path():
    base = os.environ.get("CADDY_TUI_STATE_DIR") or "/var/lib/caddy-tui"
    ok = os.access(base, os.W_OK) if os.path.isdir(base) else os.access(os.path.dirname(base), os.W_OK)
    if not ok:
        base = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"), APP)
    return os.path.join(base, "bans.json")


def _read_bans_file():
    try:
        with open(bans_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_bans():
    """Bans of the active Caddy container. (Before 1.8 the file was a flat {ip: ban} shared by all: it is adopted by
    whichever container reads it first.)"""
    data = _read_bans_file()
    if "containers" in data:
        return data["containers"].get(CONTAINER, {})
    return data


def save_bans(bans):
    data = _read_bans_file()
    if "containers" not in data:
        data = {"containers": {}}
    data["containers"][CONTAINER] = bans
    try:
        os.makedirs(os.path.dirname(bans_path()), exist_ok=True)
        with open(bans_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError as e:
        print(e, file=sys.stderr)


def register_ban(ip, reason="", seconds=0, now=None):
    now = time.time() if now is None else now
    bans = load_bans()
    bans[ip] = {"reason": reason, "ts": now, "until": now + seconds if seconds else None}
    save_bans(bans)


def forget_ban(ip):
    bans = load_bans()
    if bans.pop(ip, None) is not None:
        save_bans(bans)


def expired_bans(bans, blocked, now=None):
    now = time.time() if now is None else now
    return [ip for ip, b in bans.items() if b.get("until") and b["until"] <= now and ip in blocked]


def time_left(until, now=None):
    secs = max(0, until - (time.time() if now is None else now))
    if secs < 3600:
        return _("in {n} min").format(n=max(1, int(secs // 60)))
    if secs < 86400:
        return _("in {n} h").format(n=int(secs // 3600))
    return _("in {n} d").format(n=int(secs // 86400))


def commit_blocklist(ips, reload=True):
    """Headless: rewrite the blocklist, validate, back up, save and hot-reload. -> (ok, message)."""
    with open(CADDYFILE, encoding="utf-8") as f:
        text = f.read()
    new = set_blocked_ips(text, ips)
    if new == text:
        return True, ""
    ok, out = validate_text(new)
    if not ok:
        return False, out
    backup_caddyfile()
    write_in_place(new)
    if reload and container_state()[0] == "running":
        rc, out = run(["docker", "exec", CONTAINER, "caddy", "reload", "--config", CONF_IN_CONTAINER,
                       "--adapter", "caddyfile"], timeout=60)
        if rc != 0:
            return False, pretty_logs(out)
    return True, ""


def run_auto_block(args):
    """Headless fail2ban-style pass: ban scanners seen in the recent logs, lift expired bans. Exit 0 ok, 2 failure."""
    MAX_PER_RUN, MAX_TOTAL = 20, 500
    try:
        window = parse_span(args.window or os.environ.get("AUTOBLOCK_WINDOW") or "15m") or 900
        ban = parse_span(args.ban_time or os.environ.get("AUTOBLOCK_BAN") or "24h")
    except ValueError:
        print(_("Invalid time span. Use e.g. 15m, 24h, 7d."), file=sys.stderr)
        return 2
    with open(CADDYFILE, encoding="utf-8") as f:
        blocked = get_blocked_ips(f.read())
    bans, now = load_bans(), time.time()
    lifted = expired_bans(bans, blocked, now)
    offenders = find_scanners(read_access_events(f"{max(1, window // 60)}m"), allow=allow_list(),
                              blocked=set(blocked))[:MAX_PER_RUN]
    keep = [ip for ip in blocked if ip not in lifted]
    if len(keep) + len(offenders) > MAX_TOTAL:
        print(_("The blocklist is too long (over {n}); not adding more automatically.").format(n=MAX_TOTAL), file=sys.stderr)
        offenders = []
    lines = [_("Banned {ip}: {why}").format(ip=o["ip"], why=reason_text(o["reasons"])) for o in offenders]
    lines += [_("Ban lifted: {ip}").format(ip=ip) for ip in lifted]
    if not lines:
        if not args.quiet:
            print("✔ " + _("Nothing to do."))
        return 0
    if args.dry_run:
        print("\n".join([_("(dry run, nothing changed)")] + lines))
        return 0
    ok, out = commit_blocklist(keep + [o["ip"] for o in offenders])
    if not ok:
        print("✘ " + _("Could not update the blocklist:") + "\n" + out, file=sys.stderr)
        return 2
    for ip in lifted:
        forget_ban(ip)
    for o in offenders:
        register_ban(o["ip"], reason_text(o["reasons"]), ban, now)
    print("\n".join(lines))
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat and offenders:
        try:
            telegram_send(token, chat, "🛡 " + socket.gethostname() + ": " + _("Caddy blocked {n} address(es).").format(n=len(offenders))
                          + "\n\n" + "\n".join(lines))
        except RuntimeError as e:
            print(_("Could not send the Telegram message: {error}").format(error=e), file=sys.stderr)
    return 0


# -------------------------------------------------- services (wizard) ----
HOST_RE = re.compile(r"^(?:https?://)?[A-Za-z0-9*]([A-Za-z0-9.\-*]*[A-Za-z0-9])?(:\d{1,5})?$|^\{\$[A-Z_]+\}$")


def find_site_blocks(text):
    """Top-level site blocks: [{addr: [...], start, end, marker}] (line indexes, end inclusive).
    `marker` is the line of caddy-tui's own '# Service ...' comment right above, if any."""
    lines = text.split("\n")
    blocks, depth, cur = [], 0, None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if depth == 0 and stripped.endswith("{"):
            head = stripped[:-1].strip()
            if head and not head.startswith("("):      # a bare global "{" and "(name)" snippets are not sites
                prev = lines[i - 1] if i else ""
                cur = {"addr": [a.strip(",") for a in head.split() if a.strip(",")], "start": i,
                       "marker": i - 1 if prev.lstrip().startswith("# Service ") and "with caddy-tui" in prev else None}
        depth = max(depth + line.count("{") - line.count("}"), 0)
        if cur is not None and depth == 0:
            cur["end"] = i
            blocks.append(cur)
            cur = None
    return blocks


def site_addresses(text):
    """Addresses of the top-level site blocks in the Caddyfile."""
    return [a for b in find_site_blocks(text) for a in b["addr"]]


def hash_password(plain):
    rc, out = run(["docker", "run", "--rm", "-i", IMAGE, "caddy", "hash-password"],
                  stdin=plain + "\n", timeout=60)
    return out.strip() if rc == 0 and out.startswith("$") else None


def build_site_block(v, pw_hash=None, verb="added"):
    upstream = v["upstream"].strip()
    if v["https_upstream"] and "://" not in upstream:
        upstream = "https://" + upstream
    lines = [f"# Service {verb} with caddy-tui ({datetime.now():%Y-%m-%d %H:%M})",
             f"{v['host'].strip()} {{"]
    if v["user"] and pw_hash:
        lines += ["\tbasic_auth {", f"\t\t{v['user'].strip()} {pw_hash}", "\t}"]
    if v["https_upstream"] and v["skip_verify"]:
        lines += [f"\treverse_proxy {upstream} {{", "\t\ttransport http {",
                  "\t\t\ttls_insecure_skip_verify", "\t\t}", "\t}"]
    else:
        lines.append(f"\treverse_proxy {upstream}")
    if v["gzip"]:
        lines.append("\tencode gzip")
    if v["headers"]:
        lines += ["\theader {",
                  '\t\tStrict-Transport-Security "max-age=31536000; includeSubDomains"',
                  '\t\tX-Content-Type-Options "nosniff"',
                  '\t\tX-Frame-Options "DENY"', "\t}"]
    if v["log"]:
        lines.append("\tlog")
    lines.append("}")
    return "\n".join(lines) + "\n"


def parse_simple_block(lines):
    """-> (values, password_hash) if the block is exactly what the wizard form can produce, else None."""
    lines = [l for l in lines if l.strip() != f"import {BLOCK_NAME}"]
    body = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    m = re.match(r"^(\S+)\s*\{$", body[0]) if body else None
    if not m:
        return None
    v = {"host": m.group(1), "upstream": "", "https_upstream": False, "skip_verify": False, "gzip": False,
         "headers": False, "log": False, "user": "", "password": ""}
    pw_hash = None
    for l in body[1:-1]:
        if l.startswith("reverse_proxy "):
            up = l[len("reverse_proxy "):].rstrip("{").strip()
            if up.startswith("https://"):
                v["https_upstream"], up = True, up[len("https://"):]
            v["upstream"] = up
        elif l == "tls_insecure_skip_verify":
            v["skip_verify"] = True
        elif l == "encode gzip":
            v["gzip"] = True
        elif l.startswith("Strict-Transport-Security"):
            v["headers"] = True
        elif l == "log":
            v["log"] = True
        else:
            ma = re.match(r"^(\S+)\s+(\$2[aby]\$\S+)$", l)
            if ma:
                v["user"], pw_hash = ma.group(1), ma.group(2)
    if not v["upstream"]:
        return None
    norm = lambda ls: sorted(x.strip() for x in ls if x.strip() and not x.strip().startswith("#"))
    same = norm(build_site_block(v, pw_hash).split("\n")) == norm(lines)
    return (v, pw_hash) if same else None


# ------------------------------------------------------------ site templates ---
def _marker(verb):
    return f"# Service {verb} with caddy-tui ({datetime.now():%Y-%m-%d %H:%M})"


def _body(lines):
    """Meaningful lines of a block: stripped, without comments and without caddy-tui's blocklist import."""
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#") and l.strip() != f"import {BLOCK_NAME}"]


def _same(built, lines):
    norm = lambda ls: sorted(x.strip() for x in ls if x.strip() and not x.strip().startswith("#"))
    return norm(built.split("\n")) == norm(_body(lines))


def _head(body):
    m = re.match(r"^(\S+)\s*\{$", body[0]) if body else None
    return m.group(1) if m and body[-1] == "}" else None


def build_redirect_block(v, pw_hash=None, verb="added"):
    target = v["target"].strip() + ("{uri}" if v["keep_path"] else "")
    lines = [_marker(verb), f"{v['host'].strip()} {{", f"\tredir {target} {'permanent' if v['permanent'] else 'temporary'}"]
    if v["log"]:
        lines.append("\tlog")
    return "\n".join(lines + ["}"]) + "\n"


def parse_redirect_block(lines):
    body = _body(lines)
    host = _head(body)
    m = re.fullmatch(r"redir (\S+) (permanent|temporary)", body[1]) if host and len(body) >= 3 else None
    if not m or body[2:-1] not in ([], ["log"]):
        return None
    target = m.group(1)
    keep = target.endswith("{uri}")
    v = {"host": host, "target": target[:-5] if keep else target, "keep_path": keep,
         "permanent": m.group(2) == "permanent", "log": body[2:-1] == ["log"]}
    return v if _same(build_redirect_block(v), lines) else None


def build_maintenance_block(v, pw_hash=None, verb="added"):
    msg = v["message"].replace("\\", "\\\\").replace('"', '\\"')
    lines = [_marker(verb), f"{v['host'].strip()} {{", f'\trespond "{msg}" {int(v["status"])}']
    if v["log"]:
        lines.append("\tlog")
    return "\n".join(lines + ["}"]) + "\n"


def parse_maintenance_block(lines):
    body = _body(lines)
    host = _head(body)
    m = re.fullmatch(r'respond "((?:[^"\\]|\\.)*)" (\d{3})', body[1]) if host and len(body) >= 3 else None
    if not m or body[2:-1] not in ([], ["log"]):
        return None
    v = {"host": host, "message": re.sub(r"\\(.)", r"\1", m.group(1)), "status": int(m.group(2)), "log": body[2:-1] == ["log"]}
    return v if _same(build_maintenance_block(v), lines) else None


def route_pattern(path):
    return path.strip().rstrip("*").rstrip("/") + "/*"


def build_routes_block(v, pw_hash=None, verb="added"):
    lines = [_marker(verb), f"{v['host'].strip()} {{"]
    for path, upstream, strip in v["rules"]:
        lines += [f"\t{'handle_path' if strip else 'handle'} {route_pattern(path)} {{", f"\t\treverse_proxy {upstream.strip()}", "\t}"]
    lines += ["\thandle {", f"\t\treverse_proxy {v['upstream'].strip()}", "\t}"]
    if v["gzip"]:
        lines.append("\tencode gzip")
    if v["log"]:
        lines.append("\tlog")
    return "\n".join(lines + ["}"]) + "\n"


def parse_routes_block(lines):
    body = _body(lines)
    host = _head(body)
    if not host:
        return None
    rules, default, i, end = [], None, 1, len(body) - 1
    while i + 2 < end + 1 and body[i].startswith("handle") and body[i].endswith("{"):
        m = re.fullmatch(r"(handle_path|handle)(?: (\S+))? \{", body[i])
        up = re.fullmatch(r"reverse_proxy (\S+)", body[i + 1]) if m else None
        if not m or not up or body[i + 2] != "}":
            return None
        if m.group(2):
            rules.append((m.group(2)[:-2] if m.group(2).endswith("/*") else m.group(2), up.group(1), m.group(1) == "handle_path"))
        else:
            default = up.group(1)
        i += 3
    rest = body[i:end]
    if not rules or not default or any(x not in ("encode gzip", "log") for x in rest):
        return None
    v = {"host": host, "upstream": default, "rules": rules, "gzip": "encode gzip" in rest, "log": "log" in rest}
    return v if _same(build_routes_block(v), lines) else None


def parse_any_block(lines):
    """-> (kind, values, password_hash) for blocks a caddy-tui form can edit, else None."""
    simple = parse_simple_block(lines)
    if simple:
        return "proxy", simple[0], simple[1]
    for kind, parser in (("redirect", parse_redirect_block), ("maintenance", parse_maintenance_block), ("routes", parse_routes_block)):
        v = parser(lines)
        if v:
            return kind, v, None
    return None


def build_block_for(kind, v, pw_hash=None, verb="added"):
    builder = {"proxy": build_site_block, "redirect": build_redirect_block,
               "maintenance": build_maintenance_block, "routes": build_routes_block}[kind]
    return builder(v, pw_hash, verb)


def remove_block(text, blk):
    lines = text.split("\n")
    a = blk["marker"] if blk["marker"] is not None else blk["start"]
    del lines[a:blk["end"] + 1]
    if 0 < a < len(lines) and not lines[a].strip() and not lines[a - 1].strip():
        del lines[a]                                  # do not leave a double blank line behind
    return "\n".join(lines).rstrip("\n") + "\n"


def replace_block(text, blk, new_lines, with_marker=False):
    lines = text.split("\n")
    a = blk["marker"] if with_marker and blk["marker"] is not None else blk["start"]
    lines[a:blk["end"] + 1] = new_lines
    return "\n".join(lines)


def block_label(lines):
    """One-line description for the service list."""
    head = lines[0].strip().rstrip("{").strip()
    ups = ", ".join(f"{h}:{p}" for h, p, __ in parse_upstreams("\n".join(lines)))
    parsed = parse_any_block(lines)
    kind = parsed[0] if parsed else None
    tag = {"redirect": _("redirect"), "maintenance": _("maintenance"), "routes": _("routes")}.get(kind, "")
    if kind == "redirect":
        text = head + f"  →  {parsed[1]['target']}  [{tag}]"
    elif kind == "maintenance":
        text = head + f"  [{tag}]"
    else:
        text = head + (f"  →  {ups}" if ups else "")
        if kind == "routes":
            text += f"  [{tag}]"
    if any(l.strip().startswith(("basic_auth", "basicauth")) for l in lines):
        text += "  [auth]"
    if parsed is None:
        text += "  " + _("(custom block)")
    return text


# -------------------------------------------------- traffic monitor ------
SPARK = "▁▂▃▄▅▆▇█"


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def parse_access_line(line):
    """Caddy JSON access-log line -> (ts, host, method, uri, ip, status, bytes, seconds), or None."""
    try:
        j = json.loads(line)
        if not str(j.get("logger", "")).startswith("http.log.access"):
            return None
        r = j.get("request", {})
        return (float(j.get("ts", time.time())), r.get("host", "-"), r.get("method", "-"), r.get("uri", "-"),
                r.get("client_ip") or r.get("remote_ip", "-"),
                int(j.get("status", 0)), int(j.get("size", 0)), float(j.get("duration", 0)))
    except (ValueError, TypeError, AttributeError):
        return None


def read_access_events(since="15m"):
    """Access-log events of the last `since` (docker logs syntax) from the container."""
    rc, out = run(["docker", "logs", "--since", since, CONTAINER], timeout=60)
    return [e for e in map(parse_access_line, out.splitlines()) if e] if rc == 0 else []


class TrafficFeed:
    """Reads `docker logs -f` in the background and extracts the HTTP accesses."""

    def __init__(self, preload="15m"):
        self.events = deque(maxlen=50000)  # (ts, host, method, uri, ip, status, bytes, seconds)
        self.q = queue.Queue()
        self.alive = True
        self.proc = subprocess.Popen(
            ["docker", "logs", "-f", "--since", preload, CONTAINER],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=BASE,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            event = parse_access_line(line)
            if event:
                self.q.put(event)
        self.alive = False

    def drain(self):
        n = 0
        while True:
            try:
                self.events.append(self.q.get_nowait())
                n += 1
            except queue.Empty:
                return n

    def close(self):
        try:
            self.proc.terminate()
        except OSError:
            pass


# ----------------------------------------------------------- diagnostics ---
def parse_upstreams(text):
    """(host, port, https) for each reverse_proxy upstream in the Caddyfile."""
    ups = []
    for line in text.splitlines():
        m = re.match(r"\s*reverse_proxy\s+([^{#]+)", line)
        if not m:
            continue
        for tok in m.group(1).split():
            if tok[0] in "/*@{":
                continue
            https = tok.startswith("https://")
            t = re.sub(r"^https?://", "", tok).split("/")[0]
            if "{" in t or not t:
                continue
            host, __, port = t.partition(":")
            up = (host, port or ("443" if https else "80"), https)
            if up not in ups:
                ups.append(up)
    return ups


def site_ports(text):
    """Ports Caddy must listen on according to the Caddyfile."""
    ports = {80, 443}
    for a in site_addresses(text):
        m = re.search(r":(\d+)$", a)
        if m:
            ports.add(int(m.group(1)))
    return sorted(ports)


def find_networks(host):
    """Docker networks where the container (or alias) called `host` lives."""
    rc, ids = run(["docker", "ps", "-q"], timeout=15)
    if rc != 0 or not ids.split():
        return []
    fmt = "{{.Name}}|{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{range $v.Aliases}}{{.}},{{end}};{{end}}"
    rc, out = run(["docker", "inspect", "--format", fmt] + ids.split(), timeout=20)
    found = []
    for line in out.splitlines():
        name, __, nets = line.partition("|")
        for ent in nets.split(";"):
            net, __, al = ent.partition("=")
            if net and net not in ("bridge", "host", "none") and (
                    host == name.lstrip("/") or host in al.split(",")):
                found.append(net)
    return sorted(set(found))


def _norm_item(line):
    t = line.strip()
    return t[1:].strip().strip("\"'") if t.startswith("-") else None


def _add_list_items(lines, s, e, key, items):
    """Add items to the service's `key:` list (lines s..e). Returns the new e."""
    k = next((i for i in range(s + 1, e) if re.match(rf"^    {key}:\s*$", lines[i])), None)
    if k is None:
        new = [f"    {key}:"] + [f'      - "{i}"' if key == "ports" else f"      - {i}" for i in items]
        lines[e:e] = new
        return e + len(new)
    last = k
    for j in range(k + 1, e):
        if lines[j].startswith("      - "):
            last = j
        elif lines[j].strip() and not lines[j].startswith("      "):
            break
    have = {_norm_item(l) for l in lines[k + 1:last + 1]}
    new = [f'      - "{i}"' if key == "ports" else f"      - {i}" for i in items if i.strip('"') not in have]
    lines[last + 1:last + 1] = new
    return e + len(new)


def patch_compose(text, service, ports=(), extra_hosts=(), networks=()):
    """Add ports / extra_hosts / external networks to the compose service."""
    lines = text.split("\n")
    s = next((i for i, l in enumerate(lines)
              if re.match(rf"^  {re.escape(service)}:\s*(#.*)?$", l)), None)
    if s is None:
        raise ValueError(_("Service '{service}' not found in {file}").format(service=service, file=COMPOSE))
    e = s + 1
    while e < len(lines) and (not lines[e].strip() or lines[e].startswith("   ")):
        e += 1
    while e > s + 1 and not lines[e - 1].strip():
        e -= 1
    if ports:
        e = _add_list_items(lines, s, e, "ports", [f"{p}:{p}" for p in ports])
    if extra_hosts:
        e = _add_list_items(lines, s, e, "extra_hosts", [f'"{h}"' for h in extra_hosts])
    if networks:
        has_key = any(re.match(r"^    networks:\s*$", lines[i]) for i in range(s + 1, e))
        e = _add_list_items(lines, s, e, "networks", ([] if has_key else ["default"]) + list(networks))
        t = next((i for i, l in enumerate(lines) if re.match(r"^networks:\s*$", l)), None)
        if t is None:
            lines += ["", "networks:"]
            t = len(lines) - 1
        te = t + 1
        while te < len(lines) and (not lines[te].strip() or lines[te].startswith(" ")):
            te += 1
        while te > t + 1 and not lines[te - 1].strip():
            te -= 1
        for net in networks:
            if not any(re.match(rf"^  {re.escape(net)}:", l) for l in lines[t + 1:te]):
                lines[te:te] = [f"  {net}:", "    external: true"]
                te += 2
    return "\n".join(lines)


def compose_snippet(need):
    out = ["    # " + _("in service '{service}':").format(service=SERVICE)]
    if need["ports"]:
        out += ["    ports:"] + [f'      - "{p}:{p}"' for p in need["ports"]]
    if need["hosts"]:
        out += ["    extra_hosts:"] + [f'      - "{h}"' for h in need["hosts"]]
    if need["nets"]:
        out += ["    networks:", "      - internal"] + [f"      - {n}" for n in need["nets"]]
        out += ["", "# " + _("and at the end of the file:"), "networks:"]
        out += [f"  {n}:\n    external: true" for n in need["nets"]]
    return "\n".join(out)


# ------------------------------------------------------------ certificates ---
# Minimal DER reader: just enough X.509 to show validity, issuer and names
# without depending on openssl or third-party libraries.
def _der(data, pos):
    """Read one DER element header at `pos` -> (tag, content_start, content_end)."""
    tag, ln, pos = data[pos], data[pos + 1], pos + 2
    if ln & 0x80:
        n = ln & 0x7F
        ln, pos = int.from_bytes(data[pos:pos + n], "big"), pos + n
    return tag, pos, pos + ln


def _children(data, start, end):
    pos = start
    while pos < end:
        tag, s, e = _der(data, pos)
        yield tag, s, e
        pos = e


def _der_time(raw):
    t = raw.decode("ascii")
    fmt = "%y%m%d%H%M%SZ" if len(t) == 13 else "%Y%m%d%H%M%SZ"
    return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)


def _name_attrs(data, start, end):
    """X.501 Name -> {oid_hex: text}."""
    out = {}
    for _t1, s1, e1 in _children(data, start, end):
        for _t2, s2, e2 in _children(data, s1, e1):
            kids = list(_children(data, s2, e2))
            if len(kids) == 2 and kids[0][0] == 0x06:
                out[data[kids[0][1]:kids[0][2]].hex()] = data[kids[1][1]:kids[1][2]].decode("utf-8", "replace")
    return out


def parse_certificate(der):
    """DER X.509 -> {names, cn, issuer, not_before, not_after}."""
    __, s, e = _der(der, 0)
    tbs = list(_children(der, *list(_children(der, s, e))[0][1:]))
    i = 1 if tbs[0][0] == 0xA0 else 0          # optional [0] version
    issuer = _name_attrs(der, *tbs[i + 2][1:])
    subject = _name_attrs(der, *tbs[i + 4][1:])
    times = [der[a:b] for __, a, b in _children(der, *tbs[i + 3][1:])]
    names = []
    for tag, a, b in tbs[i + 5:]:
        if tag != 0xA3:                        # [3] extensions
            continue
        for ___, es, ee in _children(der, *next(_children(der, a, b))[1:]):
            ext = list(_children(der, es, ee))
            if der[ext[0][1]:ext[0][2]].hex() == "551d11":       # subjectAltName
                __, os_, oe = _der(der, ext[-1][1])              # inside the OCTET STRING
                names = [der[x:y].decode("ascii", "replace") for t, x, y in _children(der, os_, oe) if t == 0x82]
    cn = subject.get("550403", "")
    return {"names": names or ([cn] if cn else []), "cn": cn,
            "issuer": issuer.get("550403") or issuer.get("55040a") or "?",
            "not_before": _der_time(times[0]), "not_after": _der_time(times[1])}


def parse_pem_certificates(text):
    """Every certificate in a PEM bundle, parsed (unparseable ones are skipped)."""
    import base64
    out = []
    for body in re.findall(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", text, re.S):
        try:
            out.append(parse_certificate(base64.b64decode("".join(body.split()))))
        except (ValueError, IndexError, StopIteration):
            continue
    return out


def read_certificates():
    """Leaf certificates stored by Caddy in the container, or None if unreadable."""
    script = ('d="${XDG_DATA_HOME:-$HOME/.local/share}/caddy/certificates"; '
              'find "$d" -name "*.crt" -exec sh -c \'echo "###FILE $1"; cat "$1"\' _ {} \\; 2>/dev/null; true')
    rc, out = run(["docker", "exec", CONTAINER, "sh", "-c", script], timeout=30)
    if rc != 0:
        return None
    certs = []
    for chunk in out.split("###FILE ")[1:]:
        path, __, pem = chunk.partition("\n")
        parsed = parse_pem_certificates(pem)
        if parsed:                              # first block is the leaf; the rest is the chain
            certs.append(dict(parsed[0], file=path.strip(), internal="/certificates/local/" in path))
    return certs


def is_internal_name(host):
    """Names that get certificates from Caddy's internal CA instead of Let's Encrypt."""
    # same rule as Caddy: these never go to a public ACME CA (".lan", ".test"... do, and fail)
    return "." not in host or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))


def site_domains(text):
    """Public domain names Caddy must get certificates for, per the Caddyfile."""
    env, out = runtime_env(text), []
    for a in site_addresses(text):
        if a.startswith("http://"):
            continue
        a = re.sub(r"^https://", "", a)
        a = re.sub(r"\{\$([A-Za-z_]\w*)(:[^}]*)?\}", lambda m: env.get(m.group(1)) or (m.group(2) or ":")[1:], a)
        a = re.sub(r":\d+$", "", a).lower()
        if not a or "*" in a or re.fullmatch(r"[\d.]+|[0-9a-f:]+", a) or is_internal_name(a):
            continue
        if a not in out:
            out.append(a)
    return out


def cert_covers(names, domain):
    for n in names:
        n = n.lower()
        if n == domain or (n.startswith("*.") and domain.partition(".")[2] == n[2:]):
            return True
    return False


def acme_errors(since="24h"):
    """{domain: last ACME error text} from the container logs."""
    rc, logs = run(["docker", "logs", "--since", since, CONTAINER], timeout=30)
    errs = {}
    for line in logs.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if str(j.get("logger", "")).startswith(("tls.obtain", "tls.issuance", "tls.renew")) and j.get("level") in ("error", "warn"):
            ident = j.get("identifier") or j.get("identifiers")
            if isinstance(ident, list):
                ident = ident[0] if ident else None
            if ident and j.get("error"):
                errs[str(ident).lower()] = str(j["error"])
    return errs


def cert_report(text, now=None):
    """-> (rows, n_certs); rows = [(level, title, [details])], level in ok/warn/err."""
    certs = read_certificates()
    if certs is None:
        return [("warn", _("Could not read the certificate storage of the container."), [])], 0
    now = now or datetime.now(timezone.utc)
    rows = []
    for c in sorted(certs, key=lambda c: c["not_after"]):
        names = ", ".join(c["names"][:3]) + ("…" if len(c["names"]) > 3 else "")
        if c["internal"]:
            rows.append(("ok", _("{names} · internal CA (local, renewed automatically)").format(names=names), []))
            continue
        days = (c["not_after"] - now).total_seconds() / 86400
        date = c["not_after"].strftime("%Y-%m-%d")
        fmt = dict(names=names, issuer=c["issuer"], date=date, days=int(days))
        hint = [_("Caddy renews about 30 days before expiry. If it has not, check the ACME errors below or the logs [g].")]
        if days < 0:
            rows.append(("err", _("{names} · {issuer} · EXPIRED on {date}").format(**fmt), hint))
        elif days < 7:
            rows.append(("err", _("{names} · {issuer} · about to expire: {date} ({days} days left)").format(**fmt), hint))
        elif days < 20:
            rows.append(("warn", _("{names} · {issuer} · renewal pending: valid until {date} ({days} days left)").format(**fmt), hint))
        else:
            rows.append(("ok", _("{names} · {issuer} · valid until {date} ({days} days left)").format(**fmt), []))
    errs = None
    for d in site_domains(text):
        if any(cert_covers(c["names"], d) for c in certs):
            continue
        errs = acme_errors() if errs is None else errs
        if d in errs:
            rows.append(("err", _("Let's Encrypt/ACME is failing for {domain}").format(domain=d), [
                errs[d][:300],
                _("Typical causes: DNS not pointing here, ports 80/443 closed, or rate limits (wait an hour).")]))
        else:
            rows.append(("warn", _("No certificate yet for {domain}").format(domain=d), [
                _("Caddy requests it on the first HTTPS request. Check that the DNS points to this server and that ports 80 and 443 are reachable from the Internet.")]))
    if not certs and not rows:
        rows.append(("ok", _("No certificates stored yet"), []))
    return rows, len(certs)


MARK = {"ok": "✔", "warn": "⚠", "err": "✘"}


def format_items(items):
    """[(level, title, [details])] -> text lines for the output pane / terminal."""
    out = []
    for level, title, details in items:
        out.append(f"{MARK[level]} {title}")
        out.extend("    " + d for d in details)
    return out


def summary_line(items):
    problems = sum(1 for i in items if i[0] == "err")
    warns = sum(1 for i in items if i[0] == "warn")
    if problems or warns:
        return _("Result: {problems} problem(s), {warns} warning(s).").format(problems=problems, warns=warns)
    return "✔ " + _("All good.")


def diagnose():
    """Run the checks. Returns (items, fixes, needs); items = [(level, title, [details])]."""
    items, fixes = [], set()
    need = {"ports": [], "hosts": [], "nets": []}

    def add(level, title, *detail):
        items.append((level, title, [d for d in detail if d]))

    rc, out = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    if rc != 0:
        add("err", _("Docker is not responding"), out.splitlines()[-1] if out else "",
            _("Fix: start Docker (systemctl start docker) and check that your user can use it."))
        return items, fixes, need
    add("ok", _("Docker available (v{version})").format(version=out.strip()))

    with open(CADDYFILE, encoding="utf-8") as f:
        text = f.read()
    cenv = container_env()
    refs = referenced_vars(text)
    undefined = [n for n, d in refs if not d and not (cenv.get(n) or read_env(n))]
    if undefined:
        for n in undefined:
            add("err", _("Variable {name} in the Caddyfile is not defined (neither in the container nor in .env)").format(name=n),
                _("Fix: define {name}=... in .env with [n] and apply it with [s].").format(name=n))
    elif refs:
        add("ok", _("Caddyfile variables defined ({names})").format(names=", ".join(n for n, __ in refs)))

    ok, out = validate_text(text)
    if ok:
        add("ok", _("The Caddyfile is valid"))
    else:
        tail = out.splitlines()[-1] if out else ""
        hint = _("Fix: edit it with [e] (the TUI will not let you save something invalid).")
        if "File to import not found" in out:
            hint = _("Fix: 'import name' needs a snippet '(name) { ... }' defined at the top of the "
                     "Caddyfile, or a file with that name next to it.")
        add("err", _("The Caddyfile is NOT valid"), tail, hint)

    state, __ = container_state()
    if state != "running":
        add("err", _("Container {name} is not running (state: {state})").format(name=CONTAINER, state=state_label(state)),
            _("Fix: start it with [s]."))
        fixes.add("start")
        return items, fixes, need
    add("ok", _("Container {name} is running").format(name=CONTAINER))

    rc, live = run(["docker", "exec", CONTAINER, "cat", CONF_IN_CONTAINER], timeout=15)
    if rc == 0 and live.strip() != text.strip():
        add("err", _("The container uses a DIFFERENT Caddyfile than the one on disk"),
            _("Typical cause: an editor replaced the file and Docker still mounts the old one."),
            _("Fix: recreate the container. Until then your changes are not applied!"))
        fixes.add("recreate")
    elif rc == 0:
        add("ok", _("The container sees the same Caddyfile as the disk"))

    rc, js = run(["docker", "inspect", "--format", "{{json .HostConfig.PortBindings}}|{{json .HostConfig.ExtraHosts}}",
                  CONTAINER], timeout=15)
    pb, eh = {}, []
    try:
        a, __, b = js.partition("|")
        pb, eh = json.loads(a) or {}, json.loads(b) or []
    except ValueError:
        pass
    published = {int(k.split("/")[0]) for k, v in pb.items() if v}
    explicit = [p for p in site_ports(text) if p not in (80, 443)]
    missing = [p for p in explicit if p not in published]
    if missing:
        add("err", _("Caddyfile ports not published by the container: {ports}").format(ports=", ".join(map(str, missing))),
            _("Those sites will not be reachable from outside."),
            _("Fix: add them to 'ports' of service '{service}' in the docker compose.").format(service=SERVICE))
        need["ports"] = missing
        fixes.add("compose")
    elif explicit:
        add("ok", _("Site ports published ({ports})").format(ports=", ".join(map(str, explicit))))
    std = [p for p in (80, 443) if p not in published]
    if std:
        add("warn", _("Ports {ports} not published").format(ports=" / ".join(map(str, std))),
            _("They are needed for Let's Encrypt to issue certificates (HTTP-01 challenge) and for the HTTP→HTTPS redirect."),
            _("Ignore this if you use your own certificates, DNS-01 or only a local network."))

    for host, port, https in parse_upstreams(text):
        url = f"{'https' if https else 'http'}://{host}:{port}"
        cmd = ["docker", "exec", CONTAINER, "wget", "-S", "--spider", "-q", "-T", "4"]
        rc, out = run(cmd + (["--no-check-certificate"] if https else []) + [url], timeout=20)
        low = out.lower()
        if "http/" in low:
            add("ok", _("Upstream {host}:{port} responds").format(host=host, port=port))
        elif host == "host.docker.internal" and not any("host.docker.internal" in h for h in eh):
            add("err", _("host.docker.internal is not configured in the container"),
                _("Fix: add extra_hosts 'host.docker.internal:host-gateway'."))
            need["hosts"] = ["host.docker.internal:host-gateway"]
            fixes.add("compose")
        elif "bad address" in low or "not found" in low or "resolve" in low:
            nets = [n for n in find_networks(host)]
            msg = [_("Fix: connect Caddy to the network of '{host}'.").format(host=host)]
            if nets:
                msg = [_("'{host}' is on Docker network: {nets}").format(host=host, nets=", ".join(nets)),
                       _("Fix: add that network to '{service}' as external in the docker compose.").format(service=SERVICE)]
                need["nets"] = sorted(set(need["nets"]) | set(nets))
                fixes.add("compose")
            else:
                msg.append(_("I found no running container with that name: is it stopped?"))
            add("err", _("Caddy cannot resolve upstream '{host}' (different network)").format(host=host), *msg)
        elif "refused" in low:
            add("warn", _("{host}:{port} refuses the connection").format(host=host, port=port),
                _("The service is not listening: is it stopped, or on another port?"))
        elif upstream_answers(host, port, not https):
            if https:
                add("err", _("Upstream {host}:{port} speaks plain HTTP, but the Caddyfile reaches it with HTTPS").format(host=host, port=port),
                    _("Caddy answers 502 (\"server gave HTTP response to HTTPS client\")."),
                    _("Fix: remove 'https://' from that reverse_proxy."))
            else:
                add("err", _("Upstream {host}:{port} speaks HTTPS, but the Caddyfile reaches it in plain HTTP").format(host=host, port=port),
                    _("Caddy answers 502 (malformed HTTP response)."),
                    _("Fix: use 'reverse_proxy https://{host}:{port}' (with 'transport http {{ tls_insecure_skip_verify }}' if its certificate is self-signed).").format(host=host, port=port))
        else:
            add("warn", _("{host}:{port} does not respond (timeout)").format(host=host, port=port),
                _("Check that the service is running and the firewall."))

    for name, port in published_plain_http(text):
        add("warn", _("'{name}' is published on port {port} in plain HTTP, outside Caddy").format(name=name, port=port),
            _("Whoever reaches that port talks unencrypted, and https://...:{port} fails with \"SSL received a record that exceeded the maximum length\".").format(port=port),
            _("Fix: stop publishing that port in the docker compose of '{name}' (Caddy reaches it through the Docker network), or use http:// on purpose.").format(name=name))

    rc, logs = run(["docker", "logs", "--since", "10m", CONTAINER], timeout=20)
    errs = Counter()
    for line in logs.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("level") == "error" and not str(j.get("logger", "")).startswith("http.log.access"):
            errs[str(j.get("msg", ""))[:110]] += 1
    if errs:
        add("warn", _("{count} errors in the logs of the last 10 min").format(count=sum(errs.values())),
            *[f"{n}× {m}" for m, n in errs.most_common(3)])
    else:
        add("ok", _("No errors in the logs of the last 10 min"))

    rows, n_certs = cert_report(text)
    problems = [r for r in rows if r[0] != "ok"]
    for level, title, details in problems:
        add(level, title, *details)
    if n_certs and not problems:
        add("ok", _("HTTPS certificates valid ({n})").format(n=n_certs))

    unprotected = sites_without_blocklist(text) if get_blocked_ips(text) else []
    if unprotected:
        add("warn", _("The IP blocklist is not applied to: {sites}").format(sites=", ".join(unprotected)),
            _("Fix: open [i] and choose \"Re-apply the blocklist to all sites\"."))

    img = check_image_update(cached_registry_digest)
    if img["status"] in ("update", "pulled"):
        add("warn", _("A newer Caddy image is available ({ref})").format(ref=img["ref"]) if img["status"] == "update"
            else _("A newer Caddy image is already pulled but the container still uses the old one"),
            _("Fix: press [y] to update Caddy (your Caddyfile is validated with the new version first)."))
    elif img["status"] == "current":
        add("ok", _("Caddy image up to date (Caddy {version})").format(version=img["version"]))

    probes = probe_services(text, skip_ports=set(missing))
    bad = [p for p in probes if p[0] != "ok"]
    for level, title, details in bad:
        add(level, title, *details)
    if probes and not bad:
        add("ok", _("All {n} site(s) answer correctly (checked from this machine)").format(n=len(probes)))
    return items, fixes, need


def run_check(args, collect=None):
    """Headless diagnostics (for cron / systemd / monitoring). Exit code: 0 ok, 1 warnings, 2 problems.
    `collect` (a list) receives the JSON payload instead of printing it (used by --all --json)."""
    items, __, __ = diagnose()
    errs = sum(1 for i in items if i[0] == "err")
    warns = sum(1 for i in items if i[0] == "warn")
    code = 2 if errs else 1 if warns else 0
    if args.json:
        payload = {"status": ("ok", "warning", "critical")[code], "problems": errs, "warnings": warns,
                   "checks": [{"level": l, "title": t, "details": d} for l, t, d in items]}
        if collect is not None:
            collect.append(dict(payload, container=CONTAINER))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif not (args.quiet and code == 0):
        shown = [i for i in items if not args.quiet or i[0] != "ok"]
        print("\n".join(format_items(shown) + ["", summary_line(items)]))
    if getattr(args, "notify", False):
        notify_result(items, code)
    return code


# ------------------------------------------------------------------ alerts ---
ALERT_ENV = os.environ.get("CADDY_TUI_ALERT_ENV", "/etc/caddy-tui/alert.env")
SYSTEMD_DIR = "/etc/systemd/system"
UNIT_NAME = "caddy-tui-check"
CRON_FILE = "/etc/cron.d/caddy-tui-check"


def state_dir():
    return os.environ.get("CADDY_TUI_STATE_DIR") or "/var/lib/caddy-tui"


def state_path():
    """Alert state (what was already notified) of the active Caddy container."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", CONTAINER) or "default"
    return os.path.join(state_dir(), f"alert-state-{safe}.json")


def read_alert_state():
    for path in (state_path(), os.path.join(state_dir(), "alert-state.json")):       # the second one: before 1.8, shared
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return {}


def read_env_file(path):
    """KEY=VALUE lines (systemd EnvironmentFile style) -> dict; {} if unreadable."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def write_env_file(path, values):
    """Write KEY=VALUE lines with mode 600 (it holds the bot token)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Written by caddy-tui --setup-alerts\n")
        f.writelines(f"{k}={v}\n" for k, v in values.items() if v != "")
    os.chmod(path, 0o600)


def load_alert_env():
    """Fill missing environment variables from the alerts file (used by --notify)."""
    for k, v in read_env_file(ALERT_ENV).items():
        os.environ.setdefault(k, v)


def parse_interval(text):
    """'30m' / '2h' / '90' (minutes) -> minutes, between 5 minutes and 24 hours."""
    m = re.fullmatch(r"\s*(\d+)\s*([mh]?)(?:in|our|ours)?\s*", text.lower())
    if not m:
        raise ValueError(text)
    minutes = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    if not 5 <= minutes <= 24 * 60:
        raise ValueError(text)
    return minutes


def telegram_api(token, method, params=None, timeout=15):
    """Call the Telegram Bot API; returns `result` or raises RuntimeError (token never leaks)."""
    base = os.environ.get("CADDY_TUI_TELEGRAM_API") or "https://api.telegram.org"
    url = f"{base}/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = {"ok": False, "description": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(str(e).replace(token, "***"))
    if not body.get("ok"):
        raise RuntimeError(str(body.get("description", "error")))
    return body.get("result")


def telegram_send(token, chat_id, text):
    telegram_api(token, "sendMessage", {"chat_id": chat_id, "text": text[:3900]})


def telegram_find_chat(token):
    """(chat_id, title) of the latest chat that wrote to the bot, or None."""
    for u in reversed(telegram_api(token, "getUpdates") or []):
        msg = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
        chat = msg.get("chat")
        if chat:
            return chat["id"], chat.get("title") or chat.get("username") or chat.get("first_name") or str(chat["id"])
    return None


def alert_decision(code, items, state, now, on_warnings=False, repeat_hours=24):
    """Decide whether to notify. -> (kind, new_state); kind in (None, "problem", "recovered")."""
    failing = code >= 2 or (on_warnings and code == 1)
    shown = [i for i in items if i[0] != "ok"]
    digest = hashlib.sha256("|".join(f"{l}:{re.sub(r'[0-9]+', '#', t)}" for l, t, __ in shown).encode()).hexdigest()[:16]
    if failing:
        same = state.get("failing") and state.get("digest") == digest
        if same and now - state.get("sent", 0) < repeat_hours * 3600:
            return None, state
        return "problem", {"failing": True, "digest": digest, "sent": now}
    if state.get("failing"):
        return "recovered", {"failing": False, "digest": "", "sent": now}
    return None, state


def notify_result(items, code):
    """Send the Telegram alert (or recovery notice) for a --check result when due."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(_("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Run: sudo caddy-tui --setup-alerts"),
              file=sys.stderr)
        return
    state = read_alert_state()
    on_warn = os.environ.get("ALERT_ON_WARNINGS") == "1"
    try:
        repeat = float(os.environ.get("ALERT_REPEAT_HOURS") or 24)
    except ValueError:
        repeat = 24
    kind, new_state = alert_decision(code, items, state, time.time(), on_warn, repeat)
    if not kind:
        return
    host = socket.gethostname() + (f"/{CONTAINER}" if MULTI else "")
    if kind == "recovered":
        text = "✔ " + _("{host}: Caddy is OK again.").format(host=host)
    else:
        icon = "✘" if code >= 2 else "⚠"
        text = f"{icon} {host}: " + _("Caddy check failed.") + "\n\n" + "\n".join(
            format_items([i for i in items if i[0] != "ok"]) + ["", summary_line(items)])
    try:
        telegram_send(token, chat, text)
    except RuntimeError as e:
        print(_("Could not send the Telegram message: {error}").format(error=e), file=sys.stderr)
        return                                  # state not saved: it will retry on the next run
    try:
        os.makedirs(os.path.dirname(state_path()), exist_ok=True)
        with open(state_path(), "w", encoding="utf-8") as f:
            json.dump(new_state, f)
    except OSError as e:
        print(e, file=sys.stderr)


def render_service(command, auto_block=False, all_containers=False, stats=False):
    every = " --all" if all_containers else ""
    pre = f"ExecStartPre=-{command} --auto-block{every}\n" if auto_block else ""
    pre += f"ExecStartPre=-{command} --collect-stats --quiet{every}\n" if stats else ""
    return f"""[Unit]
Description=Check the Caddy container with caddy-tui
Documentation=man:caddy-tui(1)

[Service]
Type=oneshot
EnvironmentFile=-{ALERT_ENV}
StateDirectory=caddy-tui
{pre}ExecStart={command} --check --quiet --notify{every}
# Exit 1 means warnings only: not a failure of the unit.
SuccessExitStatus=1
"""


def render_timer(minutes):
    return f"""[Unit]
Description=Periodic Caddy check (caddy-tui)

[Timer]
OnBootSec=5min
OnUnitActiveSec={minutes}min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
"""


def cron_schedule(minutes):
    if minutes < 60:
        return f"*/{minutes} * * * *"
    hours = minutes // 60
    return "0 0 * * *" if hours >= 24 else f"0 */{hours} * * *"


def render_cron(command, minutes, auto_block=False, all_containers=False, stats=False):
    every = " --all" if all_containers else ""
    pre = f"{command} --auto-block{every} >/dev/null 2>&1; " if auto_block else ""
    pre += f"{command} --collect-stats --quiet{every} >/dev/null 2>&1; " if stats else ""
    return (f"# caddy-tui: periodic Caddy check with Telegram alerts\nSHELL=/bin/sh\n"
            f"{cron_schedule(minutes)} root {pre}{command} --check --quiet --notify{every} >/dev/null 2>&1\n")


def ask_line(prompt, default=""):
    try:
        value = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    except EOFError:
        value = ""
    return value or default


def ask_yes(prompt, default=False):
    try:
        value = input(prompt + (_(" [Y/n] ") if default else _(" [y/N] "))).strip().lower()
    except EOFError:
        value = ""
    return default if not value else value[0] in ("y", "s")


def _program_command():
    exe = os.path.abspath(sys.argv[0])
    return exe if os.access(exe, os.X_OK) else f"{sys.executable} {exe}"


def _systemd_available():
    return os.path.isdir("/run/systemd/system") and bool(shutil.which("systemctl"))


def setup_alerts(args):
    """Interactive setup: Telegram bot + periodic check (systemd timer, or cron if there is no systemd)."""
    if os.geteuid() != 0:
        sys.exit(_("Setting up alerts needs root. Run it with sudo."))
    print(_("== caddy-tui alerts setup =="))
    configure(args)
    print(_("Caddy container: {name}").format(name=CONTAINER))
    old = read_env_file(ALERT_ENV)

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or old.get("TELEGRAM_BOT_TOKEN", "")
    me = None
    for __ in range(3):
        if not token:
            token = getpass.getpass(_("Telegram bot token (create one with @BotFather): ")).strip()
        try:
            me = telegram_api(token, "getMe")
            break
        except RuntimeError as e:
            print(_("The bot token is not valid: {error}").format(error=e))
            token = ""
    if not me:
        sys.exit(_("No valid Telegram bot token."))
    bot = me.get("username", "bot")
    print(_("Bot: @{username}").format(username=bot))

    chat = os.environ.get("TELEGRAM_CHAT_ID") or old.get("TELEGRAM_CHAT_ID", "")
    if not chat:
        chat = ask_line(_("Telegram chat id (empty = detect it)"))
    if not chat:
        input(_("Send any message to @{username} on Telegram now, then press Enter.").format(username=bot))
        try:
            found = telegram_find_chat(token)
        except RuntimeError as e:
            sys.exit(_("Could not read the bot updates: {error}").format(error=e))
        if not found:
            sys.exit(_("The bot has not received any message yet. Write to it and run the setup again."))
        chat = str(found[0])
        print(_("Found chat: {title} ({id})").format(title=found[1], id=found[0]))
        if not ask_yes(_("Send the alerts to this chat?"), True):
            sys.exit(_("Cancelled."))

    try:
        minutes = parse_interval(args.interval or ask_line(_("Check interval (e.g. 30m, 1h, 6h)"), "1h"))
    except ValueError:
        sys.exit(_("Invalid interval. Use between 5 minutes and 24 hours, e.g. 30m, 1h, 6h."))
    on_warn = args.notify_warnings or ask_yes(_("Also notify on warnings (not only on problems)?"), False)
    others = list_caddy_containers()
    all_containers = len(others) > 1 and ask_yes(
        _("There are {n} Caddy containers on this machine. Monitor all of them (not only {name})?").format(n=len(others), name=CONTAINER), True)
    keep_stats = ask_yes(_("Also keep traffic statistics (hourly totals per site and busiest addresses, kept 90 days)?"), True)
    auto_block, ban_time = ask_yes(_("Also ban scanners automatically, like fail2ban? (only public addresses; bans expire)"), False), "24h"
    if auto_block:
        ban_time = ask_line(_("Ban length (e.g. 24h, 7d; 0 = permanent)"), "24h")
        try:
            parse_span(ban_time)
        except ValueError:
            sys.exit(_("Invalid time span. Use e.g. 15m, 24h, 7d."))

    values = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat, "CADDY_CONTAINER": "" if all_containers else CONTAINER,
              "CADDY_TUI_LANG": LANG, "ALERT_ON_WARNINGS": "1" if on_warn else "0",
              "CADDY_CADDYFILE": args.caddyfile or "", "CADDY_DIR": args.dir or "",
              "CADDY_COMPOSE": args.compose or "", "CADDY_SERVICE": args.service or "",
              "CADDY_ALL": "1" if all_containers else "", "STATS": "1" if keep_stats else "", "AUTOBLOCK": "1" if auto_block else "", "AUTOBLOCK_BAN": ban_time if auto_block else "",
              "AUTOBLOCK_WINDOW": f"{minutes}m" if auto_block else ""}
    write_env_file(ALERT_ENV, values)
    print(_("Saved {path} (mode 600).").format(path=ALERT_ENV))

    command = _program_command()
    if _systemd_available():
        for name, text in ((UNIT_NAME + ".service", render_service(command, auto_block, all_containers, keep_stats)), (UNIT_NAME + ".timer", render_timer(minutes))):
            with open(os.path.join(SYSTEMD_DIR, name), "w", encoding="utf-8") as f:
                f.write(text)
        for cmd in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", UNIT_NAME + ".timer"]):
            rc, out = run(cmd, timeout=60)
            if rc != 0:
                sys.exit(_("systemctl failed: {error}").format(error=out))
        print(_("systemd timer enabled: every {minutes} min. See it with: systemctl list-timers {unit}.timer").format(
            minutes=minutes, unit=UNIT_NAME))
    else:
        with open(CRON_FILE, "w", encoding="utf-8") as f:
            f.write(render_cron(command, minutes, auto_block, all_containers, keep_stats))
        print(_("No systemd found: cron job written to {path} (every {minutes} min).").format(path=CRON_FILE, minutes=minutes))

    host = socket.gethostname()
    try:
        telegram_send(token, chat, "✔ " + _("{host}: caddy-tui alerts are set up. You will be notified here when the Caddy check finds problems.").format(host=host))
        print(_("Test message sent to Telegram."))
    except RuntimeError as e:
        print(_("Could not send the Telegram message: {error}").format(error=e))
    items, __, __ = diagnose()
    print(_("First check:") + " " + summary_line(items))
    return 0


def remove_alerts(args):
    """Undo --setup-alerts: stop the timer, delete the units / cron job, optionally the saved settings."""
    if os.geteuid() != 0:
        sys.exit(_("Removing alerts needs root. Run it with sudo."))
    if _systemd_available():
        run(["systemctl", "disable", "--now", UNIT_NAME + ".timer"], timeout=60)
    removed = []
    for path in (os.path.join(SYSTEMD_DIR, UNIT_NAME + ".service"), os.path.join(SYSTEMD_DIR, UNIT_NAME + ".timer"), CRON_FILE):
        if os.path.exists(path):
            os.unlink(path)
            removed.append(path)
    if _systemd_available():
        run(["systemctl", "daemon-reload"], timeout=60)
    print(_("Removed: {files}").format(files=", ".join(removed)) if removed else _("There was nothing to remove."))
    if os.path.exists(ALERT_ENV) and ask_yes(_("Also delete the saved Telegram settings ({path})?").format(path=ALERT_ENV), False):
        os.unlink(ALERT_ENV)
        print(_("Deleted {path}.").format(path=ALERT_ENV))
    return 0


# ------------------------------------------------------------ service probes ---
def site_endpoints(text):
    """[(scheme, host, port)] of every named site in the Caddyfile (no wildcards, IPs or bare ports)."""
    env, out = runtime_env(text), []
    for a in site_addresses(text):
        scheme = "http" if a.startswith("http://") else "https"
        a = re.sub(r"^https?://", "", a)
        a = re.sub(r"\{\$([A-Za-z_]\w*)(:[^}]*)?\}", lambda m: env.get(m.group(1)) or (m.group(2) or ":")[1:], a)
        m = re.fullmatch(r"([^:/]*)(?::(\d+))?(?:/.*)?", a)
        if not m:
            continue
        host, port = m.group(1).lower(), int(m.group(2) or (80 if scheme == "http" else 443))
        if not host or "*" in host or re.fullmatch(r"[\d.]+", host):
            continue
        ep = (scheme, host, port)
        if ep not in out:
            out.append(ep)
    return out


def probe_http(scheme, host, port, connect_to, timeout=6, cafile=None, verify=True):
    """GET / on connect_to:port with SNI/Host=host. -> {status, ms, error, cert_error}."""
    t0 = time.monotonic()
    result = {"status": None, "ms": 0, "error": None, "cert_error": False}
    try:
        sock = socket.create_connection((connect_to, port), timeout=timeout)
        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                if cafile:
                    ctx.load_verify_locations(cafile)
                if not verify:
                    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname=host)
            sock.settimeout(timeout)
            sock.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: caddy-tui-probe\r\n"
                         f"Accept: */*\r\nConnection: close\r\n\r\n".encode())
            data = b""
            while b"\r\n" not in data and len(data) < 4096:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        m = re.match(rb"HTTP/\d(?:\.\d)? (\d{3})", data)
        if m:
            result["status"] = int(m.group(1))
        else:
            result["error"] = _("not an HTTP response")
    except ssl.SSLCertVerificationError as e:
        result.update(error=e.verify_message or str(e), cert_error=True)
    except (socket.timeout, TimeoutError):
        result["error"] = _("timeout")
    except ssl.SSLError as e:
        result["error"] = f"TLS: {e.reason or e}"
    except OSError as e:
        result["error"] = e.strerror or str(e)
    result["ms"] = int((time.monotonic() - t0) * 1000)
    return result


def resolve_host(host):
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    ips = []
    for info in sorted(infos, key=lambda i: i[0] != socket.AF_INET):      # IPv4 first
        if info[4][0] not in ips:
            ips.append(info[4][0])
    return ips


def local_probe_row(ep, result):
    """(level, title, details) for the check against this machine's Caddy."""
    scheme, host, port = ep
    url = f"{scheme}://{host}" + ("" if port == (80 if scheme == "http" else 443) else f":{port}")
    if result["cert_error"]:
        if is_internal_name(host):
            return "warn", url, [_("Certificate not trusted by this machine (normal with Caddy's internal CA): {reason}").format(
                reason=result["error"])]
        return "err", url, [_("TLS certificate problem: {reason}").format(reason=result["error"])]
    if result.get("plain_http"):
        return "err", url, [_("Port {port} answers plain HTTP, but this site is meant to be served over HTTPS "
                              "(browsers fail with \"SSL received a record that exceeded the maximum length\").").format(port=port),
                            _("Fix: something other than Caddy is listening on that port: check the published ports "
                              "('docker ps') and whether the address in the Caddyfile should be http://.")]
    if result["error"]:
        return "err", url, [_("No answer from Caddy: {error}").format(error=result["error"])]
    if result["status"] >= 500:
        return "err", url, [_("The service answers HTTP {status} ({ms} ms): the upstream may be down.").format(
            status=result["status"], ms=result["ms"])]
    cert = _(", certificate valid") if scheme == "https" and not result.get("cert_warning") else ""
    details = [_("HTTP {status} in {ms} ms{cert}").format(status=result["status"], ms=result["ms"], cert=cert)]
    if result.get("cert_warning"):
        details.append(_("Certificate not trusted by this machine (normal with Caddy's internal CA): {reason}").format(
            reason=result["cert_warning"]))
        return "warn", url, details
    return "ok", url, details


def upstream_answers(host, port, https):
    """True if host:port answers HTTP(S) from inside the Caddy container (the scheme Caddy would not use)."""
    cmd = ["docker", "exec", CONTAINER, "wget", "-S", "--spider", "-q", "-T", "4"]
    rc, out = run(cmd + (["--no-check-certificate"] if https else []) + [f"{'https' if https else 'http'}://{host}:{port}"],
                  timeout=20)
    return "http/" in out.lower()


def published_plain_http(text):
    """[(container, host port)]: upstream containers also published straight to the host in plain HTTP.
    Caddy already serves them over HTTPS, so that port is an unencrypted back door (and https://host:port fails)."""
    found = []
    for host, port, https in parse_upstreams(text):
        if https or host == "host.docker.internal":
            continue
        rc, js = run(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", host], timeout=15)
        if rc != 0:
            continue
        try:
            ports = json.loads(js) or {}
        except ValueError:
            continue
        for b in ports.get(f"{port}/tcp") or []:
            if b.get("HostIp") in ("127.0.0.1", "::1") or not str(b.get("HostPort", "")).isdigit():
                continue
            hp = int(b["HostPort"])
            if (host, hp) in found:
                continue
            if probe_http("http", host, hp, "127.0.0.1")["status"] is None:
                continue
            if probe_http("https", host, hp, "127.0.0.1", verify=False)["status"] is not None:
                continue                                            # it does TLS itself
            found.append((host, hp))
    return found


def probe_services(text, skip_ports=(), with_public=False, workers=8):
    """Check every named site. -> [(level, title, [details])].
    Local check: straight to 127.0.0.1 with the site's name (certificate verified).
    Public check: through DNS, like a client would (can fail from inside the LAN: hairpin NAT)."""
    eps = [e for e in site_endpoints(text) if e[2] not in skip_ports]

    def work(ep):
        scheme, host, port = ep
        local = probe_http(scheme, host, port, "127.0.0.1")
        if local["cert_error"] and is_internal_name(host):
            # internal CA: the certificate cannot be trusted here, but we can still see if the service answers
            unverified = probe_http(scheme, host, port, "127.0.0.1", verify=False)
            if unverified["status"] is not None:
                local = dict(unverified, cert_warning=local["error"])
        if scheme == "https" and not local["cert_error"] and str(local["error"] or "").startswith("TLS:"):
            local["plain_http"] = probe_http("http", host, port, "127.0.0.1")["status"] is not None
        pub = ips = None
        if with_public:
            ips = resolve_host(host)
            if ips:
                pub = probe_http(scheme, host, port, ips[0])
        return ep, local, ips, pub

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        done = list(pool.map(work, eps))
    rows = []
    for ep, local, ips, pub in done:
        level, title, details = local_probe_row(ep, local)
        if with_public:
            if not ips:
                details.append(_("DNS: {domain} does not resolve from this machine").format(domain=ep[1]))
                level = "warn" if level == "ok" else level
            else:
                details.append(_("DNS: {domain} → {ips}").format(domain=ep[1], ips=", ".join(ips[:3])))
                if pub["status"] is not None:
                    details.append(_("Public route ({ip}): HTTP {status} in {ms} ms").format(
                        ip=ips[0], status=pub["status"], ms=pub["ms"]))
                else:
                    details.append(_("Public route ({ip}): {error}. It may be hairpin NAT: test from outside your network.").format(
                        ip=ips[0], error=pub["error"]))
                    level = "warn" if level == "ok" else level
        rows.append((level, title, details))
    return rows


# ------------------------------------------------------------ image updates ---
ACCEPT_MANIFESTS = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])


def parse_image(ref):
    """'caddy:2-alpine' -> (registry host, repository, tag); None for digest-pinned references."""
    if "@" in ref:
        return None
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repo = parts[0], "/".join(parts[1:])
    else:
        registry, repo = "registry-1.docker.io", "/".join(parts)
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
    if registry == "registry-1.docker.io" and "/" not in repo:
        repo = "library/" + repo
    return registry, repo, tag


def registry_digest(ref, timeout=10):
    """Digest the registry currently serves for `ref` (anonymous bearer-token flow). Raises RuntimeError."""
    parsed = parse_image(ref)
    if not parsed:
        raise RuntimeError(_("the image is pinned by digest"))
    registry, repo, tag = parsed
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"

    def head(token=None):
        headers = {"Accept": ACCEPT_MANIFESTS, "User-Agent": f"caddy-tui/{__version__}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=headers), timeout=timeout)

    try:
        try:
            resp = head()
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise RuntimeError(f"HTTP {e.code}")
            challenge = e.headers.get("WWW-Authenticate", "")
            realm = re.search(r'realm="([^"]+)"', challenge)
            if not realm:
                raise RuntimeError("HTTP 401")
            query = {k: m.group(1) for k in ("service", "scope") for m in [re.search(rf'{k}="([^"]+)"', challenge)] if m}
            query.setdefault("scope", f"repository:{repo}:pull")
            with urllib.request.urlopen(f"{realm.group(1)}?{urllib.parse.urlencode(query)}", timeout=timeout) as r:
                body = json.load(r)
            resp = head(body.get("token") or body.get("access_token"))
        with resp:
            digest = resp.headers.get("Docker-Content-Digest")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(str(e))
    if not digest:
        raise RuntimeError(_("the registry did not return a digest"))
    return digest


def cache_file():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, APP, "image-check.json")


def cached_registry_digest(ref, ttl=6 * 3600, now=None):
    """registry_digest with a small on-disk cache, so periodic checks do not hit the registry every time."""
    now = time.time() if now is None else now
    try:
        with open(cache_file(), encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("ref") == ref and now - cached.get("ts", 0) < ttl:
            return cached["digest"]
    except (OSError, ValueError, KeyError):
        pass
    digest = registry_digest(ref)
    try:
        os.makedirs(os.path.dirname(cache_file()), exist_ok=True)
        with open(cache_file(), "w", encoding="utf-8") as f:
            json.dump({"ref": ref, "digest": digest, "ts": now}, f)
    except OSError:
        pass
    return digest


def caddy_version():
    rc, out = run(["docker", "exec", CONTAINER, "caddy", "version"], timeout=15)
    return out.split()[0] if rc == 0 and out.split() else "?"


def check_image_update(digest_fn=None):
    """-> {status, ref, version, error, image_id}; status: current / update / pulled / unknown / local."""
    digest_fn = digest_fn or registry_digest
    ref = IMAGE
    info = {"status": "unknown", "ref": ref, "version": caddy_version(), "error": None, "image_id": ""}
    rc, cid = run(["docker", "inspect", "-f", "{{.Image}}", CONTAINER], timeout=15)
    cid = cid.strip() if rc == 0 else ""
    info["image_id"] = cid
    rc, tagid = run(["docker", "image", "inspect", "-f", "{{.Id}}", ref], timeout=15)
    if cid and rc == 0 and tagid.strip() != cid:
        info["status"] = "pulled"                       # a newer image is already local, the container still uses the old one
        return info
    rc, js = run(["docker", "image", "inspect", "-f", "{{json .RepoDigests}}", cid or ref], timeout=15)
    try:
        local = [d.split("@", 1)[1] for d in (json.loads(js) or []) if "@" in d] if rc == 0 else []
    except ValueError:
        local = []
    if not local:
        info["status"] = "local"                        # built locally: nothing to compare with
        return info
    try:
        remote = digest_fn(ref)
    except RuntimeError as e:
        info["error"] = str(e)
        return info
    info["status"] = "current" if remote in local else "update"
    return info


# ----------------------------------------------------- install from scratch ---
INSTALL_CONTAINER = "caddy"


def render_install_compose(http_port=80, https_port=443, container=INSTALL_CONTAINER):
    return f"""# Created by caddy-tui --install
services:
  caddy:
    image: caddy:2-alpine
    container_name: {container}
    restart: unless-stopped
    ports:
      - "{http_port}:80"
      - "{https_port}:443"
      - "{https_port}:443/udp"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config

volumes:
  caddy_data:
  caddy_config:
"""


def render_install_caddyfile(domain="", upstream="", email=""):
    parts = []
    if email:
        parts.append(f"{{\n\temail {email}\n}}\n")
    if domain and upstream:
        parts.append(build_site_block(dict(host=domain, upstream=upstream, https_upstream=False, skip_verify=False,
                                           gzip=True, headers=True, log=True, user="", password="")))
    else:
        parts.append(':80 {\n\trespond "Caddy is running. Add your sites with caddy-tui."\n\tlog\n}\n')
    return "\n".join(parts)


def docker_run_command(directory, http_port=80, https_port=443, container=INSTALL_CONTAINER):
    """Plain `docker run` equivalent of the compose file (used when docker compose v2 is not available)."""
    return ["docker", "run", "-d", "--name", container, "--restart", "unless-stopped",
            "-p", f"{http_port}:80", "-p", f"{https_port}:443", "-p", f"{https_port}:443/udp",
            "--add-host", "host.docker.internal:host-gateway",
            "-v", f"{os.path.join(directory, 'Caddyfile')}:/etc/caddy/Caddyfile:ro",
            "-v", "caddy_data:/data", "-v", "caddy_config:/config", "caddy:2-alpine"]


def port_in_use(port):
    """True only if something is listening there (a permission error does not count as 'in use')."""
    import errno
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
        return False
    except OSError as e:
        return e.errno == errno.EADDRINUSE
    finally:
        sock.close()


def port_owner(port):
    """Best-effort name of the process listening on `port` (needs ss and, to see others' processes, root)."""
    rc, out = run(["ss", "-ltnpH", f"sport = :{port}"], timeout=10)
    m = re.search(r'users:\(\("([^"]+)"', out) if rc == 0 else None
    return m.group(1) if m else ""


def native_caddy_running():
    rc, out = run(["systemctl", "is-active", "caddy"], timeout=10)
    if rc == 0 and out.strip() == "active":
        return True
    rc, __ = run(["pgrep", "-x", "caddy"], timeout=10)
    return rc == 0


def distro_ids():
    ids = []
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            info = dict(l.rstrip("\n").split("=", 1) for l in f if "=" in l)
        ids = [info.get("ID", "").strip('"')] + info.get("ID_LIKE", "").strip('"').split()
    except OSError:
        pass
    return [i for i in ids if i]


def apt_candidate(package):
    rc, out = run(["apt-cache", "policy", package], timeout=30)
    m = re.search(r"Candidate: (\S+)", out) if rc == 0 else None
    return m.group(1) if m and m.group(1) != "(none)" else ""


def docker_install_plan(ids=None, candidate=None, have_apt=None):
    """-> list of packages to install with apt (docker.io + a docker compose v2 package if the distro has one),
    or None if this is not an apt-based distribution."""
    ids = distro_ids() if ids is None else ids
    have_apt = bool(shutil.which("apt-get")) if have_apt is None else have_apt
    if not have_apt or not any(i in ("debian", "ubuntu", "raspbian", "linuxmint", "armbian", "pop") for i in ids):
        return None
    candidate = candidate or apt_candidate
    packages = ["docker.io"]
    for pkg in ("docker-compose-v2", "docker-compose-plugin", "docker-compose"):
        version = candidate(pkg)
        major = re.match(r"(?:\d+:)?(\d+)", version or "")
        if major and (pkg != "docker-compose" or int(major.group(1)) >= 2):   # the old v1 'docker-compose' does not count
            packages.append(pkg)
            break
    return packages


def compose_available():
    rc, __ = run(["docker", "compose", "version"], timeout=20)
    return rc == 0


def ensure_docker():
    """Make sure the docker CLI exists, offering to install it (apt-based systems, as root). -> bool"""
    if shutil.which("docker"):
        return True
    print(_("Docker is not installed."))
    plan = docker_install_plan()
    if plan is None:
        print(_("Install it following https://docs.docker.com/engine/install/ and run caddy-tui again."))
        return False
    if os.geteuid() != 0:
        print(_("Installing Docker needs root. Run: sudo caddy-tui --install"))
        return False
    if not ask_yes(_("Install Docker now? (apt packages: {packages})").format(packages=" ".join(plan)), True):
        return False
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    print(_("Installing Docker (this can take a few minutes)..."))
    for cmd in (["apt-get", "update"], ["apt-get", "install", "-y"] + plan):
        rc, out = run(cmd, timeout=1800)
        if rc != 0:
            print(_("The installation failed:") + "\n" + "\n".join(out.splitlines()[-8:]))
            return False
    if _systemd_available():
        run(["systemctl", "enable", "--now", "docker"], timeout=120)
    rc, out = run(["docker", "info"], timeout=60)
    if rc != 0:
        print(_("Docker was installed but is not responding: {error}").format(error=out.splitlines()[-1] if out else ""))
        return False
    user = os.environ.get("SUDO_USER")
    if user and user != "root" and ask_yes(_("Let user {user} run docker without sudo (adds them to the docker group)?").format(user=user), False):
        run(["usermod", "-aG", "docker", user], timeout=30)
        print(_("Done. {user} must log out and back in for it to take effect.").format(user=user))
    if not compose_available():
        print(_("Note: docker compose v2 is not available in this distribution. Caddy will run as a plain container: "
                "some automatic fixes will show you the change to make instead of applying it."))
    print("✔ " + _("Docker installed."))
    return True


def docker_status():
    """-> ('ok' | 'denied' | 'stopped', raw output): can this user talk to the Docker daemon?"""
    rc, out = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    if rc == 0:
        return "ok", out
    return ("denied" if "permission denied" in out.lower() else "stopped"), out


def ensure_docker_running(interactive=True, stream=None):
    """The docker CLI exists; make sure the daemon answers, offering to start it when it is stopped. -> bool"""
    stream = stream or sys.stdout
    status, __ = docker_status()
    if status == "ok":
        return True
    if status == "denied":
        print(_("You are not allowed to use Docker. Run caddy-tui with sudo, or add your user to the docker group."), file=stream)
        return False
    print(_("Docker is installed but its service is not running."), file=stream)
    if interactive and os.geteuid() == 0 and _systemd_available() and ask_yes(_("Start Docker now?"), True):
        rc, out = run(["systemctl", "enable", "--now", "docker"], timeout=120)
        if rc == 0 and docker_status()[0] == "ok":
            print("✔ " + _("Docker is running."))
            return True
        print(_("Could not start Docker: {error}").format(error=out.splitlines()[-1] if out else ""), file=stream)
        return False
    print(_("Start it with: sudo systemctl start docker"), file=stream)
    return False


def validate_standalone(directory, image="caddy:2-alpine", env=None):
    envs = [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
    rc, out = run(["docker", "run", "--rm", *envs, "-v", f"{directory}:/etc/caddy:ro", "-w", "/etc/caddy", image,
                   "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], timeout=180)
    return rc == 0, pretty_logs(out)


def install_flow(args):
    """Guided first-time setup of Caddy in Docker. -> name of the new container, or None if cancelled."""
    global BASE
    print(_("== caddy-tui: set up Caddy in Docker =="))
    if native_caddy_running():
        print("⚠ " + _("A Caddy that is not in Docker is running on this machine; it probably owns ports 80/443."))
        if not ask_yes(_("Continue anyway?"), False):
            return None
    existing = list_caddy_containers()
    if existing and not ask_yes(_("A Caddy container already exists ({name}). Create another one?").format(name=existing[0][0]), False):
        return None
    default_dir = "/opt/caddy" if os.geteuid() == 0 else os.path.join(os.path.expanduser("~"), "caddy")
    directory = os.path.abspath(args.dir or ask_line(_("Folder for the configuration"), default_dir))
    if any(os.path.exists(os.path.join(directory, f)) for f in ("Caddyfile", "docker-compose.yml")):
        print(_("{path} already contains a Caddyfile or docker-compose.yml. Choose another folder.").format(path=directory))
        return None

    domain = ask_line(_("Domain of your first site (empty = decide later)")).strip().lower()
    upstream = email = ""
    if domain:
        if not HOST_RE.match(domain) or domain.startswith(("http://", "https://")):
            print(_("Invalid domain"))
            return None
        upstream = ask_line(_("Where does that site's application listen? (host:port, e.g. 192.168.1.50:8080)")).strip()
        if not upstream or any(ch.isspace() for ch in upstream):
            print(_("Enter the upstream, e.g. 192.168.1.50:8080"))
            return None
        if not is_internal_name(domain):
            ips = resolve_host(domain)
            print(_("DNS: {domain} → {ips}").format(domain=domain, ips=", ".join(ips[:3])) if ips
                  else "⚠ " + _("DNS: {domain} does not resolve from this machine").format(domain=domain))
            email = ask_line(_("Email for Let's Encrypt notices (optional)")).strip()
            if email and "@" not in email:
                email = ""

    http_port, https_port = 80, 443
    busy = [(p, port_owner(p)) for p in (80, 443) if port_in_use(p)]
    if busy:
        print("⚠ " + _("Ports in use: {ports}").format(ports=", ".join(f"{p}" + (f" ({o})" if o else "") for p, o in busy)))
        print(_("Let's Encrypt needs 80 and 443. You can use other ports for now (e.g. behind a router that forwards 80/443 to them)."))
        if not ask_yes(_("Use alternative ports (8080 / 8443)?"), True):
            return None
        http_port, https_port = 8080, 8443
        taken = [p for p in (http_port, https_port) if port_in_use(p)]
        if taken:
            print(_("Ports in use: {ports}").format(ports=", ".join(map(str, taken))))
            return None

    use_compose = compose_available()
    files = ["Caddyfile"] + (["docker-compose.yml"] if use_compose else [])
    print()
    print(_("Folder: {path}").format(path=directory))
    print(_("Files: {files}").format(files=", ".join(files)))
    print(_("Site: {site}").format(site=f"{domain} → {upstream}" if domain else _("none yet (a placeholder on port 80)")))
    print(_("Ports: {http} → 80, {https} → 443").format(http=http_port, https=https_port))
    if not ask_yes(_("Create it and start Caddy?"), True):
        return None

    caddyfile = render_install_caddyfile(domain, upstream, email)
    with tempfile.TemporaryDirectory() as tmp:                      # validate before touching the destination
        with open(os.path.join(tmp, "Caddyfile"), "w", encoding="utf-8") as f:
            f.write(caddyfile)
        print(_("Validating the configuration (pulls the Caddy image the first time)..."))
        ok, out = validate_standalone(tmp)
    if not ok:
        print("✘ " + _("The generated configuration is NOT valid") + "\n" + out)
        return None
    name = INSTALL_CONTAINER
    if inspect_container(name):
        name = "caddy-tui-caddy"
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "Caddyfile"), "w", encoding="utf-8") as f:
        f.write(caddyfile)
    if use_compose:
        with open(os.path.join(directory, "docker-compose.yml"), "w", encoding="utf-8") as f:
            f.write(render_install_compose(http_port, https_port, name))
        cmd = ["docker", "compose", "-f", os.path.join(directory, "docker-compose.yml"), "up", "-d"]
    else:
        cmd = docker_run_command(directory, http_port, https_port, name)
    print(_("Starting Caddy..."))
    BASE = directory
    rc, out = run(cmd, cwd=directory, timeout=600)
    if rc != 0:
        print("✘ " + _("Could not start Caddy:") + "\n" + "\n".join(out.splitlines()[-10:]))
        return None
    for __ in range(15):
        state, __ = container_state_of(name)
        if state == "running":
            break
        time.sleep(1)
    else:
        rc, logs = run(["docker", "logs", "--tail", "15", name], timeout=20)
        print("✘ " + _("Caddy did not stay running:") + "\n" + logs)
        return None
    print("✔ " + _("Caddy is running in container '{name}' (configuration in {path}).").format(name=name, path=directory))
    if domain and not is_internal_name(domain):
        print(_("It will request the certificate for {domain} on the first HTTPS request: the DNS must point to this server and ports 80/443 must reach it.").format(domain=domain))
    print(_("Opening caddy-tui..."))
    time.sleep(1.5)
    return name


def container_state_of(name):
    rc, out = run(["docker", "inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}", name], timeout=10)
    if rc != 0:
        return "missing", ""
    state, __, started = out.partition("|")
    return state, started


# ------------------------------------------------------- export / import ---
EXPORT_FORMAT = 1
EXPORT_MEMBERS = ("manifest.json", "Caddyfile", "docker-compose.yml", ".env", "bans.json", "alert.env", "caddy_data.tar.gz")
EXPORT_MAX_BYTES = 300 * 1024 * 1024


def run_bytes(cmd, data=None, timeout=300):
    """Like run() but binary: -> (rc, stdout bytes, stderr text)."""
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout, cwd=BASE)
        return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 1, b"", str(e)


def default_export_name():
    return f"caddy-tui-export-{socket.gethostname()}-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"


def export_certs():
    """The container's Caddy data directory (certificates, ACME account) as tar.gz bytes, or None."""
    rc, data, err = run_bytes(["docker", "run", "--rm", "--volumes-from", CONTAINER, IMAGE, "sh", "-c",
                               'tar czf - -C "${XDG_DATA_HOME:-/data}" .'], timeout=300)
    return data if rc == 0 and data else None


def build_export(path, with_certs=False, with_alerts=False):
    """Write the export bundle (tar.gz, mode 600). -> manifest dict."""
    import hashlib
    files = {}
    sources = [("Caddyfile", CADDYFILE)]
    if COMPOSE and os.path.isfile(COMPOSE):
        sources.append(("docker-compose.yml", COMPOSE))
    sources += [(".env", ENVFILE), ("bans.json", bans_path())]
    if with_alerts:
        sources.append(("alert.env", ALERT_ENV))
    for name, src in sources:
        try:
            with open(src, "rb") as f:
                files[name] = f.read()
        except OSError:
            continue
    if with_certs:
        certs = export_certs()
        if certs:
            files["caddy_data.tar.gz"] = certs
    manifest = {"format": EXPORT_FORMAT, "tool": __version__, "created": datetime.now().isoformat(timespec="seconds"),
                "host": socket.gethostname(), "container": CONTAINER, "image": IMAGE, "service": SERVICE,
                "compose": bool(COMPOSE), "files": {n: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for n, b in files.items()}}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in [("manifest.json", json.dumps(manifest, indent=1).encode())] + list(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(data))
    os.chmod(path, 0o600)
    return manifest


def read_export(path):
    """Read and verify a bundle. -> (manifest, {name: bytes}). Raises ValueError with a readable reason."""
    import hashlib
    members, total = {}, 0
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name not in EXPORT_MEMBERS or not m.isfile():
                    raise ValueError(_("Unexpected entry in the file: {name}").format(name=m.name))
                total += m.size
                if total > EXPORT_MAX_BYTES:
                    raise ValueError(_("The file is too big."))
                members[m.name] = tar.extractfile(m).read()
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ValueError(_("Not a caddy-tui export: {error}").format(error=e))
    try:
        manifest = json.loads(members.pop("manifest.json"))
        assert manifest["format"] == EXPORT_FORMAT
    except (KeyError, ValueError, AssertionError):
        raise ValueError(_("Not a caddy-tui export (missing or unsupported manifest)."))
    for name, info in manifest.get("files", {}).items():
        data = members.get(name)
        if data is None or hashlib.sha256(data).hexdigest() != info.get("sha256"):
            raise ValueError(_("{name} is missing or corrupted in the export.").format(name=name))
    if "Caddyfile" not in members:
        raise ValueError(_("The export has no Caddyfile."))
    return manifest, members


def import_flow(args, bundle):
    """Restore an export into a new Caddy in Docker. -> container name, or None."""
    global BASE
    try:
        manifest, files = read_export(bundle)
    except ValueError as e:
        print("✘ " + str(e))
        return None
    print(_("== caddy-tui: restore an export =="))
    print(_("Export from {host} ({date}), caddy-tui {version}; image {image}.").format(
        host=manifest.get("host", "?"), date=manifest.get("created", "?"), version=manifest.get("tool", "?"), image=manifest.get("image", "?")))
    print(_("Contains: {files}").format(files=", ".join(sorted(files))))
    yes = getattr(args, "yes", False)
    if native_caddy_running() and not yes and not ask_yes(_("A Caddy that is not in Docker is running on this machine. Continue anyway?"), False):
        return None
    default_dir = "/opt/caddy" if os.geteuid() == 0 else os.path.join(os.path.expanduser("~"), "caddy")
    directory = os.path.abspath(args.dir or (default_dir if yes else ask_line(_("Folder for the configuration"), default_dir)))
    if any(os.path.exists(os.path.join(directory, f)) for f in ("Caddyfile", "docker-compose.yml")):
        print(_("{path} already contains a Caddyfile or docker-compose.yml. Choose another folder.").format(path=directory))
        return None
    busy = [p for p in (80, 443) if port_in_use(p)]
    if busy and not yes and not ask_yes(_("Ports in use: {ports}").format(ports=", ".join(map(str, busy))) + " " + _("Continue anyway?"), False):
        return None
    if not yes and not ask_yes(_("Restore it into {path} and start Caddy?").format(path=directory), True):
        return None

    image = manifest.get("image") or "caddy:2-alpine"
    env_text = files.get(".env", b"").decode("utf-8", "replace")
    env = {k.strip(): v.strip().strip("\"'") for k, v in (l.split("=", 1) for l in env_text.splitlines()
                                                         if "=" in l and not l.lstrip().startswith("#"))}
    caddyfile = files["Caddyfile"].decode("utf-8")
    needed = dict(referenced_vars(caddyfile))
    val_env = {n: env[n] for n in needed if n in env}
    val_env.update({n: "example.com" for n, has_default in needed.items() if n not in val_env and not has_default})
    with tempfile.TemporaryDirectory() as tmp:                      # validate before touching the destination
        with open(os.path.join(tmp, "Caddyfile"), "w", encoding="utf-8") as f:
            f.write(caddyfile)
        print(_("Validating the configuration (pulls the Caddy image the first time)..."))
        ok, out = validate_standalone(tmp, image, val_env)
    if not ok:
        print("✘ " + _("The configuration in the export is NOT valid") + "\n" + out)
        return None

    os.makedirs(directory, exist_ok=True)
    compose_text = files.get("docker-compose.yml")
    use_compose = compose_available()
    if compose_text is None and use_compose:
        compose_text = render_install_compose().encode()
    for name, mode in (("Caddyfile", 0o644), (".env", 0o600), ("docker-compose.yml", 0o644)):
        data = files.get(name) if name != "docker-compose.yml" else compose_text
        if data is not None and (name != "docker-compose.yml" or use_compose):
            with open(os.path.join(directory, name), "wb") as f:
                f.write(data)
            os.chmod(os.path.join(directory, name), mode)
    BASE = directory
    name = manifest.get("container") or INSTALL_CONTAINER
    if inspect_container(name):
        print("⚠ " + _("A container called '{name}' already exists; remove it or restore on another machine.").format(name=name))
        return None
    if not use_compose:
        print("⚠ " + _("docker compose v2 is not available: Caddy will run as a plain container (without the certificates, if any)."))
        rc, out = run(docker_run_command(directory, 80, 443, name), cwd=directory, timeout=600)
    else:
        cf = os.path.join(directory, "docker-compose.yml")
        rc, out = run(["docker", "compose", "-f", cf, "create"], cwd=directory, timeout=600)
        if rc == 0 and "caddy_data.tar.gz" in files:
            rc2, cid = run(["docker", "compose", "-f", cf, "ps", "-aq", manifest.get("service") or "caddy"], cwd=directory, timeout=60)
            rc3, __, err = run_bytes(["docker", "run", "--rm", "-i", "--volumes-from", cid.split()[0] if cid.split() else "", image, "sh", "-c",
                                       'tar xzf - -C "${XDG_DATA_HOME:-/data}"'], data=files["caddy_data.tar.gz"], timeout=300)
            print(("✔ " + _("Certificates and ACME account restored.")) if rc2 == 0 and rc3 == 0
                  else "⚠ " + _("Could not restore the certificates: {error}").format(error=err))
        if rc == 0:
            rc, out = run(["docker", "compose", "-f", cf, "up", "-d"], cwd=directory, timeout=600)
    if rc != 0:
        print("✘ " + _("Could not start Caddy:") + "\n" + "\n".join(out.splitlines()[-10:]))
        return None
    for __ in range(15):
        state, __ = container_state_of(name)
        if state == "running":
            break
        time.sleep(1)
    else:
        rc, logs = run(["docker", "logs", "--tail", "15", name], timeout=20)
        print("✘ " + _("Caddy did not stay running:") + "\n" + logs)
        return None
    print("✔ " + _("Caddy is running in container '{name}' (configuration in {path}).").format(name=name, path=directory))
    if "alert.env" in files or "bans.json" in files:
        print(_("The export also had alerts / bans settings. Run: sudo caddy-tui --setup-alerts to set the periodic check up again."))
    return name


# -------------------------------------------------------------- statistics ---
STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS hourly (container TEXT, hour INTEGER, host TEXT, requests INTEGER, bytes INTEGER,
  s2 INTEGER, s3 INTEGER, s4 INTEGER, s5 INTEGER, dur_sum REAL, dur_max REAL, PRIMARY KEY (container, hour, host));
CREATE TABLE IF NOT EXISTS daily_ips (container TEXT, day INTEGER, ip TEXT, requests INTEGER, errors INTEGER,
  PRIMARY KEY (container, day, ip));
CREATE TABLE IF NOT EXISTS meta (container TEXT, key TEXT, value REAL, PRIMARY KEY (container, key));
"""
STATS_PERIODS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400}


def stats_path():
    return os.path.join(os.path.dirname(bans_path()), "stats.sqlite3")


def stats_db(path=None):
    """Open (creating it, mode 600: it holds client addresses) the statistics database."""
    path = path or stats_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    db = sqlite3.connect(path, timeout=30)
    db.executescript(STATS_SCHEMA)
    if new:
        os.chmod(path, 0o600)
    return db


def aggregate_events(events, since_ts=0.0):
    """Access events -> ({(hour, host): [req, bytes, 2xx, 3xx, 4xx, 5xx, dur_sum, dur_max]}, {(day, ip): [req, errors]}, last_ts, n)."""
    hourly, ips, last, n = {}, {}, since_ts, 0
    for ts, host, __, __, ip, status, size, dur in events:
        if ts <= since_ts:
            continue
        n += 1
        last = max(last, ts)
        h = hourly.setdefault((int(ts // 3600) * 3600, host), [0, 0, 0, 0, 0, 0, 0.0, 0.0])
        h[0] += 1
        h[1] += size
        if 200 <= status < 600:
            h[status // 100] += 1
        h[6] += dur
        h[7] = max(h[7], dur)
        i = ips.setdefault((int(ts // 86400) * 86400, ip), [0, 0])
        i[0] += 1
        i[1] += 1 if status >= 400 else 0
    return hourly, ips, last, n


def store_aggregates(db, container, hourly, ips, last_ts):
    with db:
        for (hour, host), v in hourly.items():
            db.execute("""INSERT INTO hourly VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(container, hour, host) DO UPDATE SET
                          requests=requests+excluded.requests, bytes=bytes+excluded.bytes, s2=s2+excluded.s2, s3=s3+excluded.s3,
                          s4=s4+excluded.s4, s5=s5+excluded.s5, dur_sum=dur_sum+excluded.dur_sum, dur_max=MAX(dur_max, excluded.dur_max)""",
                       (container, hour, host, *v))
        for (day, ip), v in ips.items():
            db.execute("""INSERT INTO daily_ips VALUES (?,?,?,?,?) ON CONFLICT(container, day, ip) DO UPDATE SET
                          requests=requests+excluded.requests, errors=errors+excluded.errors""", (container, day, ip, *v))
        if last_ts:
            db.execute("INSERT INTO meta VALUES (?, 'cursor', ?) ON CONFLICT(container, key) DO UPDATE SET value=excluded.value",
                       (container, last_ts))


def prune_stats(db, now=None, hourly_days=90, ip_days=30):
    now = time.time() if now is None else now
    with db:
        db.execute("DELETE FROM hourly WHERE hour < ?", (now - hourly_days * 86400,))
        db.execute("DELETE FROM daily_ips WHERE day < ?", (now - ip_days * 86400,))


def iter_access_events(since=None):
    """Stream the access-log events of the container (`since` = unix time, or None for everything the container still has)."""
    cmd = ["docker", "logs"] + (["--since", str(int(since))] if since else []) + [CONTAINER]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=BASE)
    try:
        for line in proc.stdout:
            event = parse_access_line(line)
            if event:
                yield event
    finally:
        proc.stdout.close()
        proc.wait()


def collect_stats(db=None, events=None, now=None):
    """Fold the access log since the last run into the database. -> number of requests counted."""
    own = db is None
    db = db or stats_db()
    try:
        row = db.execute("SELECT value FROM meta WHERE container=? AND key='cursor'", (CONTAINER,)).fetchone()
        cursor = row[0] if row else 0.0
        keep_from = (time.time() if now is None else now) - 90 * 86400
        if events is None:
            events = iter_access_events(cursor or None)
        hourly, ips, last, n = aggregate_events((e for e in events if e[0] >= keep_from), cursor)
        store_aggregates(db, CONTAINER, hourly, ips, last)
        prune_stats(db, now)
        return n
    finally:
        if own:
            db.close()


def stats_report(db, container, start, end):
    """Numbers for [start, end): totals, per hour, per site, top addresses and the span of data available."""
    q = lambda sql, *a: db.execute(sql, a).fetchall()
    by_hour = q("""SELECT hour, SUM(requests), SUM(bytes), SUM(s2), SUM(s3), SUM(s4), SUM(s5), SUM(dur_sum), MAX(dur_max)
                   FROM hourly WHERE container=? AND hour>=? AND hour<? GROUP BY hour ORDER BY hour""", container, start, end)
    by_site = q("""SELECT host, SUM(requests), SUM(bytes), SUM(s4), SUM(s5), SUM(dur_sum) FROM hourly
                   WHERE container=? AND hour>=? AND hour<? GROUP BY host ORDER BY 2 DESC""", container, start, end)
    top_ips = q("""SELECT ip, SUM(requests), SUM(errors) FROM daily_ips WHERE container=? AND day>=? AND day<?
                   GROUP BY ip ORDER BY 2 DESC LIMIT 10""", container, int(start // 86400) * 86400, end)
    span = q("SELECT MIN(hour), MAX(hour) FROM hourly WHERE container=?", container)[0]
    t = [sum(r[i] or 0 for r in by_hour) for i in range(1, 8)]
    return {"requests": t[0], "bytes": t[1], "s2": t[2], "s3": t[3], "s4": t[4], "s5": t[5], "dur_sum": t[6],
            "dur_max": max([r[8] or 0 for r in by_hour], default=0), "by_hour": by_hour, "by_site": by_site,
            "top_ips": top_ips, "first": span[0], "last": span[1]}


def _pct(part, total):
    return f"{100 * part / total:.1f}%" if total else "0%"


def _bar(value, peak, width=24):
    return "█" * (max(1, round(width * value / peak)) if value and peak else 0)


def render_stats(rep, label, hourly_rows):
    """Text of the report. `hourly_rows` = True to list hours (24 h), False to group by local day."""
    n = rep["requests"]
    if not n:
        return _("No data for this period yet.")
    avg = rep["dur_sum"] / n * 1000
    peak = max(rep["by_hour"], key=lambda r: r[1])
    out = [label, "",
           _("Requests: {n} · Traffic sent: {size} · Avg latency: {avg:.0f} ms (max {mx:.0f} ms)").format(
               n=f"{n:,}", size=human_bytes(rep["bytes"]), avg=avg, mx=rep["dur_max"] * 1000),
           _("Status: 2xx {a} ({ap}) · 3xx {b} · 4xx {c} ({cp}) · 5xx {d} ({dp})").format(
               a=f"{rep['s2']:,}", ap=_pct(rep["s2"], n), b=f"{rep['s3']:,}", c=f"{rep['s4']:,}", cp=_pct(rep["s4"], n),
               d=f"{rep['s5']:,}", dp=_pct(rep["s5"], n)),
           _("Busiest hour: {when} · {n} requests").format(when=datetime.fromtimestamp(peak[0]).strftime("%Y-%m-%d %H:00"), n=f"{peak[1]:,}"),
           ""]
    buckets = {}
    for hour, req, __, __, __, s4, s5, __, __ in rep["by_hour"]:
        d = datetime.fromtimestamp(hour)
        key = d.strftime("%Y-%m-%d %H:00") if hourly_rows else d.strftime("%Y-%m-%d")
        b = buckets.setdefault(key, [0, 0, 0])
        b[0] += req
        b[1] += s4
        b[2] += s5
    top = max(b[0] for b in buckets.values())
    out.append(_("Requests per hour") if hourly_rows else _("Requests per day"))
    for key, (req, s4, s5) in buckets.items():
        out.append(f"  {key}  {_bar(req, top):<24} {req:>8,}   4xx {s4:,} · 5xx {s5:,}")
    out += ["", _("Sites")]
    for host, req, size, s4, s5, dsum in rep["by_site"][:15]:
        out.append(f"  {host:<34} {req:>9,} ({_pct(req, n):>6})  avg {dsum / req * 1000:.0f} ms  4xx {_pct(s4, req)}  5xx {_pct(s5, req)}  {human_bytes(size)}")
    if rep["top_ips"]:
        out += ["", _("Busiest client addresses (counted by UTC day)")]
        out += [_("  {ip:<40} {n:>8,} requests · {e:,} errors").format(ip=ip, n=r, e=e) for ip, r, e in rep["top_ips"]]
    out += ["", _("Data available: {a} → {b}").format(a=datetime.fromtimestamp(rep["first"]).strftime("%Y-%m-%d %H:00"),
                                                     b=datetime.fromtimestamp(rep["last"]).strftime("%Y-%m-%d %H:00"))]
    return "\n".join(out)


def period_report(period, now=None):
    """-> (report dict, text) for "24h" / "7d" / "30d" / "90d" and the active container, after collecting what is new."""
    now = time.time() if now is None else now
    db = stats_db()
    try:
        collect_stats(db, now=now)
        end = int(now // 3600) * 3600 + 3600
        rep = stats_report(db, CONTAINER, end - STATS_PERIODS[period], end)
    finally:
        db.close()
    label = _("Traffic statistics · last {period} · {name}").format(period=period, name=CONTAINER)
    return rep, render_stats(rep, label, period == "24h")


def run_stats(args):
    """--stats [PERIOD]: print the report (or JSON)."""
    period = args.stats if args.stats in STATS_PERIODS else "7d"
    rep, text = period_report(period)
    if args.json:
        slim = {k: v for k, v in rep.items() if k not in ("by_hour", "by_site", "top_ips")}
        slim.update(container=CONTAINER, period=period,
                    sites=[{"host": h, "requests": r, "bytes": b, "s4": s4, "s5": s5} for h, r, b, s4, s5, __ in rep["by_site"]],
                    top_ips=[{"ip": ip, "requests": r, "errors": e} for ip, r, e in rep["top_ips"]],
                    hours=[{"hour": h, "requests": r} for h, r, *__ in rep["by_hour"]])
        print(json.dumps(slim, ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


def run_collect_stats(args):
    n = collect_stats()
    if not getattr(args, "quiet", False):
        print(_("Statistics updated: {n} new request(s) counted for {name}.").format(n=n, name=CONTAINER))
    return 0


# --------------------------------------------------------------------- UI ----
class App:
    def __init__(self, scr, args=None):
        self.scr = scr
        self.args = args
        self.caddies, self.caddies_ts = [], 0
        self.out = [_("Welcome. Press a key from the menu.")]
        self.title = _("Output")
        self.diff = False
        self.scroll = 0
        self.state = ("?", "")
        self.last_refresh = 0
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        scr.timeout(1000)

    # -- output
    def show(self, title, text):
        self.title = title
        self.out = text.splitlines() or [_("(no output)")]
        self.scroll = 0
        self.diff = False

    def show_diff(self, title, lines):
        self.show(title, "\n".join(lines))
        self.diff = True

    def refresh_state(self, force=False):
        if force or time.time() - self.last_refresh > 3:
            self.state = container_state()
            self.last_refresh = time.time()
        if force or time.time() - getattr(self, "caddies_ts", 0) > 20:
            self.caddies, self.caddies_ts = list_caddy_containers(), time.time()

    # -- drawing
    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 12 or w < 50:
            scr.addstr(0, 0, _("Terminal too small (min. 50x12)"))
            scr.refresh()
            return
        state, started = self.state
        color = {"running": 1, "exited": 2, "missing": 2}.get(state, 3)
        scr.addstr(0, 0, " Caddy TUI ".ljust(w), curses.A_REVERSE)
        scr.addstr(1, 1, _("Container: "))
        scr.addstr(f"{CONTAINER} ")
        scr.addstr(f"● {state_label(state)}", curses.color_pair(color) | curses.A_BOLD)
        if state == "running":
            scr.addstr(_("  (up {time})").format(time=uptime(started)))
        names = [row[0] for row in getattr(self, "caddies", [])]
        if len(names) > 1:
            pos = names.index(CONTAINER) + 1 if CONTAINER in names else 1
            scr.addstr("  " + _("{i}/{n} Caddy containers · [S] switch").format(i=pos, n=len(names)), curses.A_DIM)
        scr.addstr(2, 1, f"Caddyfile: {CADDYFILE}"[: w - 2])

        # menu in two columns
        top = 4
        colw = max(30, min(w // 2, 34))
        cols = 3 if w >= 3 * colw + 2 else 2
        rows = -(-len(MENU) // cols)
        for i, (k, label) in enumerate(MENU):
            y, x = top + i % rows, 1 + (i // rows) * colw
            if y < h - 3 and x + colw <= w + 2:
                scr.addstr(y, x, f"[{k}]", curses.color_pair(4) | curses.A_BOLD)
                scr.addstr(f" {_(label)}"[: colw - 5])

        # output panel
        oy = top + rows + 1
        scr.addstr(oy, 0, f" {self.title} ".ljust(w), curses.A_UNDERLINE)
        avail = h - oy - 2
        max_scroll = max(0, len(self.out) - avail)
        self.scroll = max(0, min(self.scroll, max_scroll))
        for i, line in enumerate(self.out[self.scroll : self.scroll + avail]):
            attr = 0
            if getattr(self, "diff", False):
                if line.startswith(("+++", "---")):
                    attr = curses.A_BOLD
                elif line.startswith("+"):
                    attr = curses.color_pair(1)
                elif line.startswith("-"):
                    attr = curses.color_pair(2)
                elif line.startswith("@@"):
                    attr = curses.color_pair(4)
            elif line.startswith("✘") or " ERROR " in line:
                attr = curses.color_pair(2)
            elif line.startswith("⚠") or " WARN " in line:
                attr = curses.color_pair(3)
            elif line.startswith("✔") or "Valid configuration" in line:
                attr = curses.color_pair(1)
            scr.addstr(oy + 1 + i, 1, line[: w - 2].replace("\t", "    "), attr)
        hint = _("↑/↓ PgUp/PgDn scroll output")
        if max_scroll:
            hint += f"  [{self.scroll + 1}-{min(self.scroll + avail, len(self.out))}/{len(self.out)}]"
        scr.addstr(h - 1, 0, hint[: w - 1], curses.A_DIM)
        scr.refresh()

    # -- dialogs
    def ask(self, question, default=False):
        h, w = self.scr.getmaxyx()
        suffix = _(" [Y/n] ") if default else _(" [y/N] ")
        self.scr.addstr(h - 1, 0, (question + suffix).ljust(w - 1)[: w - 1], curses.A_REVERSE)
        self.scr.refresh()
        self.scr.timeout(-1)
        try:
            while True:
                k = self.scr.getch()
                if k in (ord("s"), ord("S"), ord("y"), ord("Y")):
                    return True
                if k in (ord("n"), ord("N"), 27):
                    return False
                if k in (10, 13, curses.KEY_ENTER):
                    return default
        finally:
            self.scr.timeout(1000)

    def pick(self, title, items):
        """Simple list picker; returns the index or None."""
        sel = 0
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                self.scr.erase()
                self.scr.addstr(0, 0, f" {title} ".ljust(w), curses.A_REVERSE)
                for i, it in enumerate(items[: h - 3]):
                    self.scr.addstr(i + 1, 2, it[: w - 4], curses.A_REVERSE if i == sel else 0)
                self.scr.addstr(h - 1, 0, _("↑/↓ choose · Enter accept · Esc cancel")[: w - 1], curses.A_DIM)
                k = self.scr.getch()
                if k == curses.KEY_UP:
                    sel = max(0, sel - 1)
                elif k == curses.KEY_DOWN:
                    sel = min(len(items) - 1, sel + 1)
                elif k in (10, 13, curses.KEY_ENTER):
                    return sel
                elif k == 27:
                    return None
        finally:
            self.scr.timeout(1000)

    def external_editor(self, path):
        editor = os.environ.get("EDITOR") or shutil.which("nano") or "vi"
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.call(f'{editor} "{path}"', shell=True)
        finally:
            curses.reset_prog_mode()
            self.scr.refresh()
            curses.curs_set(0)

    # -- actions
    def busy(self, msg):
        self.show(_("Working..."), msg)
        self.draw()

    def act_validate(self):
        self.busy(_("Validating Caddyfile..."))
        with open(CADDYFILE, encoding="utf-8") as f:
            ok, out = validate_text(f.read())
        self.show(_("Validation: ✔ OK") if ok else _("Validation: ✘ ERRORS"), out)
        return ok

    def act_reload(self):
        state, __ = container_state()
        if state != "running":
            self.show(_("Reload"), _("The container is not running. Use [s] to start it."))
            return
        if not self.act_validate():
            return
        self.busy(_("Reloading configuration..."))
        rc, out = run(["docker", "exec", CONTAINER, "caddy", "reload",
                       "--config", CONF_IN_CONTAINER, "--adapter", "caddyfile"], timeout=60)
        out = pretty_logs(out)
        if rc == 0:
            out = "✔ " + _("Configuration reloaded without downtime.") + "\n" + out
        self.show(_("Reload: OK") if rc == 0 else _("Reload: ERROR"), out)

    def act_compose(self, title, *args, confirm=None):
        if confirm and not self.ask(confirm):
            return
        self.busy(title + "...")
        rc, out = run(compose(*args), timeout=180)
        self.show(f"{title}: " + ("OK" if rc == 0 else "ERROR"), out or _("(done)"))
        self.refresh_state(True)

    def act_start(self):
        self.act_compose(_("Start / apply"), "up", "-d", SERVICE)

    def act_logs(self):
        self.busy(_("Reading logs..."))
        rc, out = run(["docker", "logs", "--tail", "200", CONTAINER], timeout=20)
        self.show(_("Logs of {name} (last 200 lines)").format(name=CONTAINER), pretty_logs(out))
        self.scroll = 10**6  # to the end

    def act_view(self):
        with open(CADDYFILE, encoding="utf-8") as f:
            self.show("Caddyfile", f.read())

    def act_format(self):
        with open(CADDYFILE, encoding="utf-8") as f:
            text = f.read()
        self.busy(_("Formatting..."))
        rc, out = run(["docker", "run", "--rm", "-i", IMAGE, "caddy", "fmt", "-"], stdin=text, timeout=60)
        if rc != 0:
            self.show(_("Format: ERROR"), out)
        elif out.strip() == text.strip():
            self.show(_("Format"), "✔ " + _("The Caddyfile is already correctly formatted."))
        else:
            backup_caddyfile()
            write_in_place(out + "\n")
            self.show(_("Format"), "✔ " + _("Caddyfile reformatted (backup in .caddy-backups/)."))

    def act_edit(self):
        with open(CADDYFILE, encoding="utf-8") as f:
            original = f.read()
        fd, tmp = tempfile.mkstemp(prefix="Caddyfile.", suffix=".edit")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(original)
            while True:
                self.external_editor(tmp)
                with open(tmp, encoding="utf-8") as f:
                    new = f.read()
                if new == original:
                    self.show(_("Edit"), _("No changes."))
                    return
                self.busy(_("Validating changes..."))
                ok, out = validate_text(new)
                if ok:
                    break
                self.show("✘ " + _("The configuration is NOT valid"), out)
                self.draw()
                if not self.ask(_("Edit again? (No = discard changes)"), True):
                    self.show(_("Edit"), _("Changes discarded. The Caddyfile was not modified.") + "\n\n" + out)
                    return
            bk = backup_caddyfile()
            write_in_place(new)
            self.show(_("Edit"), "✔ " + _("Caddyfile saved and validated.\nPrevious copy: {path}").format(path=os.path.relpath(bk, BASE)))
            self.draw()
            state, __ = container_state()
            if state == "running" and self.ask(_("Reload Caddy now?"), True):
                self.act_reload()
        finally:
            os.path.exists(tmp) and os.unlink(tmp)

    def act_backup(self):
        os.makedirs(BACKUPS, exist_ok=True)
        dest = os.path.join(BACKUPS, "full-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".tar.gz")
        files = [("Caddyfile", CADDYFILE), (".env", ENVFILE)]
        names = [n for n, path in files if os.path.exists(path)]
        with tarfile.open(dest, "w:gz") as tar:
            for n, path in files:
                if os.path.exists(path):
                    tar.add(path, arcname=n)
        os.chmod(dest, 0o600)  # .env holds secrets
        self.show(_("Backup"), "✔ " + _("Backup created: {path}\nContains: {files}").format(path=os.path.relpath(dest, BASE), files=", ".join(names)))

    def _restore(self, entry, text, env_text=None):
        """Show what restoring `text` would change, ask, back up the current state and restore."""
        with open(CADDYFILE, encoding="utf-8") as f:
            current = f.read()
        self.busy(_("Validating backup..."))
        ok, out = validate_text(text)
        if not ok:
            self.show(_("The backup's Caddyfile is not valid; not restoring"), out)
            return
        self.show_diff(_("Changes if you restore {name}").format(name=entry["name"]),
                       unified_diff(current, text, _("current"), entry["name"]) or [_("(identical to the current Caddyfile)")])
        self.draw()
        what = _("Caddyfile and .env") if env_text is not None else "Caddyfile"
        if not self.ask(_("Restore {what}? The current state will be backed up first.").format(what=what)):
            return
        if env_text is not None:
            self.act_backup()
        else:
            backup_caddyfile()
        write_in_place(text)
        if env_text is not None:
            with open(ENVFILE, "w", encoding="utf-8") as f:
                f.write(env_text)
        msg = "✔ " + _("Restored {what} from {file}.").format(what=what, file=entry["name"])
        if env_text is not None:
            msg += "\n" + _("Use [s] to apply the .env changes.")
        self.show(_("Restore"), msg)
        self.draw()
        state, __ = container_state()
        if state == "running" and self.ask(_("Reload Caddy now?"), True):
            self.act_reload()

    def act_restore(self):
        entries = read_backups()
        if not entries:
            self.show(_("Restore"), _("No backups yet. Create one with [k] (copies are also made when editing)."))
            return
        with open(CADDYFILE, encoding="utf-8") as f:
            current = f.read()
        labels = []
        for e in entries:
            text, __ = backup_text(e)
            added, removed = diff_stats(current, text)
            change = f"+{added} -{removed}" if added or removed else _("identical to current")
            labels.append(f"{datetime.fromtimestamp(e['mtime']):%Y-%m-%d %H:%M}  {ago(e['mtime'])}  {change}"
                          + ("  (Caddyfile + .env)" if e["kind"] == "full" else ""))
        i = self.pick(_("Choose the backup to restore"), labels)
        if i is None:
            return
        text, env_text = backup_text(entries[i])
        self._restore(entries[i], text, env_text)

    def act_undo(self):
        """Go back to the latest earlier version that differs from the current Caddyfile."""
        with open(CADDYFILE, encoding="utf-8") as f:
            current = f.read()
        for e in read_backups():
            text, __ = backup_text(e)
            if text != current:
                self._restore(e, text)           # Caddyfile only; pressing [u] again redoes it
                return
        self.show(_("Undo"), _("There is no earlier version different from the current Caddyfile."))

    # -- modal form (wizards)
    def form(self, title, fields, validator):
        """Full-screen form. Returns {key: value} or None."""
        vals = {f["key"]: f["value"] for f in fields}
        nb, sel, err = len(fields), 0, ""
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                self.scr.erase()
                self.scr.addstr(0, 0, f" {title} ".ljust(w - 1), curses.A_REVERSE)
                vx = 34
                for i, f in enumerate(fields):
                    v, focused = vals[f["key"]], sel == i
                    if f["kind"] == "bool":
                        shown = "[x] " + _("yes") if v else "[ ] " + _("no")
                    elif f["kind"] == "secret":
                        shown = "*" * len(v)
                    else:
                        shown = v
                    if focused and f["kind"] != "bool":
                        shown += "▏"
                    self.scr.addstr(2 + i, 2, f["label"][: vx - 4])
                    self.scr.addstr(2 + i, vx, shown[: max(1, w - vx - 2)],
                                    curses.A_REVERSE if focused else curses.A_UNDERLINE)
                by = 3 + nb
                for j, label in enumerate((_("[ Accept ]"), _("[ Cancel ]"))):
                    self.scr.addstr(by, 2 + j * 16, label,
                                    curses.A_REVERSE if sel == nb + j else curses.A_BOLD)
                if sel < nb:
                    self.scr.addstr(by + 2, 2, fields[sel].get("hint", "")[: w - 4], curses.A_DIM)
                if err:
                    self.scr.addstr(by + 4, 2, ("✘ " + err)[: w - 4], curses.color_pair(2) | curses.A_BOLD)
                self.scr.addstr(h - 1, 0,
                                _("Tab/↑↓ move · Space toggle · Ctrl+U clear field · Esc cancel")[: w - 1],
                                curses.A_DIM)
                try:
                    k = self.scr.get_wch()
                except curses.error:
                    continue
                err = ""
                f = fields[sel] if sel < nb else None
                if k == "\x1b":
                    return None
                if k in (curses.KEY_DOWN, "\t") or (k in ("\n", "\r") and sel < nb):
                    sel = min(sel + 1, nb + 1)
                elif k in (curses.KEY_UP, curses.KEY_BTAB):
                    sel = max(sel - 1, 0)
                elif sel >= nb and k == curses.KEY_RIGHT:
                    sel = nb + 1
                elif sel >= nb and k == curses.KEY_LEFT:
                    sel = nb
                elif sel >= nb and k in ("\n", "\r", curses.KEY_ENTER, " "):
                    if sel == nb + 1:
                        return None
                    err = validator(vals) or ""
                    if not err:
                        return vals
                elif f and f["kind"] == "bool" and k in (" ", curses.KEY_LEFT, curses.KEY_RIGHT):
                    vals[f["key"]] = not vals[f["key"]]
                elif f and f["kind"] != "bool":
                    if k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                        vals[f["key"]] = vals[f["key"]][:-1]
                    elif k == "\x15":
                        vals[f["key"]] = ""
                    elif isinstance(k, str) and k.isprintable():
                        vals[f["key"]] += k
        finally:
            self.scr.timeout(1000)

    # -- service form (add / edit)
    def _service_form(self, title, initial, existing, pw_hash=None, editing=False):
        iv = dict(host="", upstream="", https_upstream=False, skip_verify=False, gzip=True, headers=True,
                  log=True, user="", password="")
        iv.update(initial or {})
        fields = [
            dict(key="host", label=_("Service domain"), kind="text", value=iv["host"],
                 hint=_("E.g. app.mydomain.com (its DNS must point to this server)")),
            dict(key="upstream", label=_("Upstream (host:port)"), kind="text", value=iv["upstream"],
                 hint=_("E.g. 192.168.1.50:8080 · or container-name:3000 if it is on Caddy's network")),
            dict(key="https_upstream", label=_("Upstream uses HTTPS"), kind="bool", value=iv["https_upstream"]),
            dict(key="skip_verify", label=_("  …ignore its certificate"), kind="bool", value=iv["skip_verify"],
                 hint=_("Only if the upstream uses a self-signed certificate (lowers security)")),
            dict(key="gzip", label=_("Gzip compression"), kind="bool", value=iv["gzip"]),
            dict(key="headers", label=_("Security headers"), kind="bool", value=iv["headers"]),
            dict(key="log", label=_("Access log (monitor)"), kind="bool", value=iv["log"],
                 hint=_("Needed to see this service in the traffic monitor")),
            dict(key="user", label=_("User (basic auth)"), kind="text", value=iv["user"],
                 hint=_("Optional: leave user and password empty for no login")),
            dict(key="password", label=_("Password"), kind="secret", value="",
                 hint=_("Leave empty to keep the current password") if pw_hash else _("Stored hashed (bcrypt) in the Caddyfile")),
        ]

        def check(v):
            host, up = v["host"].strip(), v["upstream"].strip()
            if not HOST_RE.match(host):
                return _("Invalid domain")
            if host.lower() in existing:
                return _("A block for that domain already exists in the Caddyfile")
            if not up or any(c.isspace() for c in up):
                return _("Enter the upstream, e.g. 192.168.1.50:8080")
            has_user, has_pw = bool(v["user"].strip()), bool(v["password"])
            if has_user != has_pw and not (editing and has_user and pw_hash):
                return _("Enter user and password, or leave both empty")
            if any(c.isspace() for c in v["user"].strip()):
                return _("The user cannot contain spaces")
            return None

        return self.form(title, fields, check)

    # -- site forms (add / edit) for each kind of site
    def _host_check(self, v, existing):
        host = v["host"].strip()
        if not HOST_RE.match(host):
            return _("Invalid domain")
        if host.lower() in existing:
            return _("A block for that domain already exists in the Caddyfile")
        return None

    def _redirect_form(self, title, initial, existing):
        iv = dict(host="", target="", keep_path=True, permanent=True, log=True)
        iv.update(initial or {})
        fields = [
            dict(key="host", label=_("Domain to redirect from"), kind="text", value=iv["host"],
                 hint=_("E.g. www.mydomain.com (its DNS must point to this server)")),
            dict(key="target", label=_("Redirect to (URL)"), kind="text", value=iv["target"],
                 hint=_("E.g. https://mydomain.com")),
            dict(key="keep_path", label=_("Keep the path and query"), kind="bool", value=iv["keep_path"],
                 hint=_("www.x.com/a?b=1 goes to https://x.com/a?b=1")),
            dict(key="permanent", label=_("Permanent redirect (301)"), kind="bool", value=iv["permanent"],
                 hint=_("Off = temporary (302)")),
            dict(key="log", label=_("Access log (monitor)"), kind="bool", value=iv["log"]),
        ]

        def check(v):
            err = self._host_check(v, existing)
            if err:
                return err
            if not re.fullmatch(r"https?://\S+", v["target"].strip()):
                return _("Enter a full URL starting with http:// or https://")
            return None
        return self.form(title, fields, check)

    def _maintenance_form(self, title, initial, existing):
        iv = dict(host="", message=_("We are doing some maintenance. Please come back soon."), status="503", log=True)
        iv.update({k: (str(v) if k == "status" else v) for k, v in (initial or {}).items()})
        fields = [
            dict(key="host", label=_("Service domain"), kind="text", value=iv["host"],
                 hint=_("E.g. app.mydomain.com (its DNS must point to this server)")),
            dict(key="message", label=_("Message"), kind="text", value=iv["message"],
                 hint=_("What visitors see (one line)")),
            dict(key="status", label=_("HTTP status"), kind="text", value=iv["status"],
                 hint=_("503 tells search engines and monitors that it is temporary")),
            dict(key="log", label=_("Access log (monitor)"), kind="bool", value=iv["log"]),
        ]

        def check(v):
            err = self._host_check(v, existing)
            if err:
                return err
            if not v["message"].strip():
                return _("Enter a message")
            if not (v["status"].strip().isdigit() and 100 <= int(v["status"]) <= 599):
                return _("The status must be a number between 100 and 599")
            return None
        v = self.form(title, fields, check)
        return dict(v, status=int(v["status"]), message=v["message"].strip()) if v else None

    def _routes_form(self, title, initial, existing):
        iv = dict(host="", upstream="", rules=[], gzip=True, log=True)
        iv.update(initial or {})
        rules = list(iv["rules"]) + [("", "", False)] * 3
        fields = [
            dict(key="host", label=_("Service domain"), kind="text", value=iv["host"],
                 hint=_("E.g. app.mydomain.com (its DNS must point to this server)")),
            dict(key="upstream", label=_("Default upstream (host:port)"), kind="text", value=iv["upstream"],
                 hint=_("Receives everything that matches no path below")),
        ]
        for n in range(3):
            path, up, strip = rules[n]
            fields += [
                dict(key=f"path{n}", label=_("Path {n} (e.g. /api)").format(n=n + 1), kind="text", value=path,
                     hint=_("Requests under this path go to the upstream on the next line")),
                dict(key=f"up{n}", label=_("  …upstream (host:port)"), kind="text", value=up),
                dict(key=f"strip{n}", label=_("  …remove the path prefix"), kind="bool", value=strip,
                     hint=_("On: /api/users reaches the upstream as /users")),
            ]
        fields += [dict(key="gzip", label=_("Gzip compression"), kind="bool", value=iv["gzip"]),
                   dict(key="log", label=_("Access log (monitor)"), kind="bool", value=iv["log"])]

        def check(v):
            err = self._host_check(v, existing)
            if err:
                return err
            if not v["upstream"].strip() or any(ch.isspace() for ch in v["upstream"].strip()):
                return _("Enter the upstream, e.g. 192.168.1.50:8080")
            seen, used = set(), 0
            for n in range(3):
                path, up = v[f"path{n}"].strip(), v[f"up{n}"].strip()
                if not path and not up:
                    continue
                if not path.startswith("/") or any(ch.isspace() for ch in path):
                    return _("A path must start with / (e.g. /api)")
                if not up or any(ch.isspace() for ch in up):
                    return _("Enter the upstream, e.g. 192.168.1.50:8080")
                if route_pattern(path) in seen:
                    return _("The same path appears twice")
                seen.add(route_pattern(path))
                used += 1
            return None if used else _("Add at least one path (otherwise use a plain reverse proxy)")
        v = self.form(title, fields, check)
        if not v:
            return None
        rules = [(v[f"path{n}"].strip().rstrip("*").rstrip("/") or "/", v[f"up{n}"].strip(), v[f"strip{n}"])
                 for n in range(3) if v[f"path{n}"].strip()]
        return dict(host=v["host"], upstream=v["upstream"], rules=rules, gzip=v["gzip"], log=v["log"])

    def site_form(self, kind, title, initial, existing, pw_hash=None, editing=False):
        if kind == "redirect":
            return self._redirect_form(title, initial, existing)
        if kind == "maintenance":
            return self._maintenance_form(title, initial, existing)
        if kind == "routes":
            return self._routes_form(title, initial, existing)
        return self._service_form(title, initial, existing, pw_hash, editing)

    def _commit_caddyfile(self, new, template, title=None, **kw):
        """Validate `new`, back up and save it; offer a reload."""
        self.busy(_("Validating changes..."))
        new = sync_blocklist(new)
        ok, out = validate_text(new)
        if not ok:
            self.show("✘ " + _("The configuration is NOT valid"), out)
            return False
        bk = backup_caddyfile()
        write_in_place(new)
        self.show(title or _("Manage services"), "✔ " + template.format(path=os.path.relpath(bk, BASE), **kw))
        self.draw()
        state, __ = container_state()
        if state == "running" and self.ask(_("Reload Caddy now?"), True):
            self.act_reload()
        return True

    def act_manage(self):
        with open(CADDYFILE, encoding="utf-8") as f:
            text = f.read()
        blocks = find_site_blocks(text)
        if not blocks:
            self.show(_("Manage services"), _("There are no site blocks in the Caddyfile yet. Use [a] to add one."))
            return
        lines = text.split("\n")
        i = self.pick(_("Choose a service"), [block_label(lines[b["start"]:b["end"] + 1]) for b in blocks])
        if i is None:
            return
        blk = blocks[i]
        blines = lines[blk["start"]:blk["end"] + 1]
        host = " ".join(blk["addr"])
        parsed = parse_any_block(blines)
        options = ([("form", _("Edit with the form"))] if parsed else []) + [
            ("editor", _("Edit in the editor")), ("remove", _("Remove")), ("cancel", _("Cancel"))]
        j = self.pick(_("What do you want to do with {host}?").format(host=host), [label for __, label in options])
        if j is None or options[j][0] == "cancel":
            return
        action = options[j][0]

        if action == "remove":
            self.show(_("Block that will be removed"), "\n".join(blines))
            self.draw()
            if not self.ask(_("Remove service {host}? The block will be deleted from the Caddyfile.").format(host=host)):
                self.show(_("Manage services"), _("Cancelled. Nothing was changed."))
                return
            self._commit_caddyfile(remove_block(text, blk), _("Service {host} removed (previous copy: {path})."), host=host)
            return

        if action == "form":
            kind, values, pw_hash = parsed
            existing = [a.lower() for b in blocks if b is not blk for a in b["addr"]]
            v = self.site_form(kind, _("Edit service"), values, existing, pw_hash, editing=True)
            if v is None:
                self.show(_("Manage services"), _("Cancelled. Nothing was changed."))
                return
            user = v["user"].strip() if kind == "proxy" else ""
            new_hash = None
            if user:
                self.busy(_("Generating configuration..."))
                new_hash = hash_password(v["password"]) if v["password"] else pw_hash
                if not new_hash:
                    self.show(_("Manage services"), _("Could not hash the password (is Docker available?)."))
                    return
            block = build_block_for(kind, v, new_hash, verb="edited").rstrip("\n").split("\n")
            new = replace_block(text, blk, block, with_marker=True)
            self.show(_("Block preview"), "\n".join(block))
            self.draw()
            if not self.ask(_("Save to the Caddyfile?"), True):
                self.show(_("Manage services"), _("Cancelled. Nothing was changed."))
                return
            self._commit_caddyfile(new, _("Service {host} updated (previous copy: {path})."), host=v["host"].strip())
            return

        # edit just this block in the editor, validated in the context of the whole file
        fd, tmp = tempfile.mkstemp(prefix="service.", suffix=".caddy")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(blines) + "\n")
            while True:
                self.external_editor(tmp)
                with open(tmp, encoding="utf-8") as f:
                    edited = f.read().rstrip("\n")
                if not edited.strip() or edited.split("\n") == blines:
                    self.show(_("Manage services"), _("No changes."))
                    return
                candidate = replace_block(text, blk, edited.split("\n"))
                self.busy(_("Validating changes..."))
                ok, out = validate_text(candidate)
                if ok:
                    break
                self.show("✘ " + _("The configuration is NOT valid"), out)
                self.draw()
                if not self.ask(_("Edit again? (No = discard changes)"), True):
                    self.show(_("Manage services"), _("Cancelled. Nothing was changed."))
                    return
            self._commit_caddyfile(candidate, _("Service {host} updated (previous copy: {path})."), host=host)
        finally:
            os.path.exists(tmp) and os.unlink(tmp)

    def act_add(self):
        with open(CADDYFILE, encoding="utf-8") as f:
            current = f.read()
        existing = [a.lower() for a in site_addresses(current)]
        kinds = [("proxy", _("Reverse proxy to an application")), ("routes", _("Reverse proxy with paths (/api → another app)")),
                 ("redirect", _("Redirect to another address")), ("maintenance", _("Maintenance page (fixed message)"))]
        i = self.pick(_("What do you want to add?"), [label for __, label in kinds])
        if i is None:
            return
        kind = kinds[i][0]
        v = self.site_form(kind, _("Add service to Caddy"), None, existing)
        if v is None:
            self.show(_("Add service"), _("Cancelled. Nothing was changed."))
            return
        self.busy(_("Generating configuration..."))
        pw = hash_password(v["password"]) if kind == "proxy" and v["user"].strip() else None
        if kind == "proxy" and v["user"].strip() and not pw:
            self.show(_("Add service: ERROR"), _("Could not hash the password (is Docker available?)."))
            return
        block = build_block_for(kind, v, pw)
        new = sync_blocklist(current.rstrip("\n") + "\n\n" + block)
        ok, out = validate_text(new)
        if not ok:
            self.show("✘ " + _("The generated configuration is NOT valid"), out + "\n\n" + block)
            return
        self.show(_("Block preview (will be appended to the end of the Caddyfile)"), block)
        self.draw()
        if not self.ask(_("Save to the Caddyfile?"), True):
            self.show(_("Add service"), _("Cancelled. Nothing was changed."))
            return
        bk = backup_caddyfile()
        write_in_place(new)
        self.show(_("Add service"), "✔ " + _(
            "Service {host} added (previous copy: {path}).\n"
            "Remember: the domain's DNS must point to this server and, if the upstream is another\n"
            "container, it must be on the same Docker network as Caddy.").format(
                host=v["host"].strip(), path=os.path.relpath(bk, BASE)))
        self.draw()
        state, __ = container_state()
        if state == "running" and self.ask(_("Reload Caddy now?"), True):
            self.act_reload()

    # -- traffic monitor
    def act_traffic(self):
        state, __ = container_state()
        if state != "running":
            self.show(_("Traffic monitor"), _("The Caddy container is not running. Use [s] to start it."))
            return
        windows = [(60, "1 min"), (300, "5 min"), (900, "15 min")]
        wi, paused_at = 1, None
        feed = TrafficFeed()
        self.scr.timeout(500)
        try:
            while True:
                if paused_at is None:
                    feed.drain()
                self.draw_traffic(feed, windows[wi], paused_at)
                k = self.scr.getch()
                if k in (ord("q"), ord("Q"), 27):
                    return
                if k == ord("p"):
                    paused_at = None if paused_at else time.time()
                elif k == ord("w"):
                    wi = (wi + 1) % len(windows)
                elif k == ord("c"):
                    feed.events.clear()
                elif k == ord("b"):
                    self.block_from_monitor(feed, windows[wi], paused_at)
        finally:
            feed.close()
            self.scr.timeout(1000)
            self.show(_("Traffic monitor"), _("Monitor closed."))

    def draw_traffic(self, feed, window, paused_at):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 14 or w < 60:
            scr.addstr(0, 0, _("Terminal too small (min. 60x14)"))
            scr.refresh()
            return
        span, label = window
        now = paused_at or time.time()
        ev = [e for e in feed.events if now - span <= e[0] <= now]
        n = len(ev)
        title = _(" Traffic monitor · {name} · window {label}").format(name=CONTAINER, label=label) + (_("  [PAUSED]") if paused_at else "")
        scr.addstr(0, 0, title.ljust(w - 1), curses.A_REVERSE)
        codes = Counter(e[5] // 100 for e in ev)
        avg_ms = (sum(e[7] for e in ev) / n * 1000) if n else 0
        scr.addstr(2, 2, _("Requests: {n}  ({rate:.2f}/s)    Traffic sent: {size}    Avg latency: {ms:.0f} ms").format(
            n=n, rate=n / span, size=human_bytes(sum(e[6] for e in ev)), ms=avg_ms)[: w - 3])
        x = 2
        for cls, col in ((2, 1), (3, 4), (4, 3), (5, 2)):
            txt = f"{cls}xx {codes.get(cls, 0)}"
            scr.addstr(3, x, txt, curses.color_pair(col) | curses.A_BOLD)
            x += len(txt) + 4

        # sparkline: requests per slice of the window
        width = w - 6
        buckets = [0] * width
        start = now - span
        for e in ev:
            buckets[min(width - 1, int((e[0] - start) / span * width))] += 1
        mx = max(buckets) or 1
        line = "".join(" " if c == 0 else SPARK[min(7, int((c - 1) / max(mx - 1, 1) * 7.99))] for c in buckets)
        scr.addstr(5, 2, line, curses.color_pair(1))
        scr.addstr(6, 2, f"-{label}".ljust(width - 6) + _("now"), curses.A_DIM)

        # rankings
        half = w // 2
        for col, (name, idx) in enumerate(((_("Top domains"), 1), (_("Top client IPs"), 4))):
            scr.addstr(8, 2 + col * half, name, curses.A_BOLD | curses.A_UNDERLINE)
            for r, (val, cnt) in enumerate(Counter(e[idx] for e in ev).most_common(4)):
                scr.addstr(9 + r, 2 + col * half, f"{cnt:>6}  {val}"[: half - 4])

        # latest requests
        ly = 14
        scr.addstr(ly - 1, 0, _(" Latest requests ").ljust(w - 1), curses.A_UNDERLINE)
        avail = h - ly - 1
        if not n and not feed.events:
            msg = [_("No requests recorded yet."),
                   _("Accesses are only recorded if the site has the `log` directive in the Caddyfile"),
                   _("(services created with the wizard [a] include it by default).")]
            for i, m in enumerate(msg[:avail]):
                scr.addstr(ly + i, 2, m[: w - 3], curses.A_DIM)
        for i, e in enumerate(ev[-avail:] if avail > 0 else []):
            ts, host, meth, uri, ip, st, size, dur = e
            col = {2: 1, 3: 4, 4: 3, 5: 2}.get(st // 100, 0)
            scr.addstr(ly + i, 1, time.strftime("%H:%M:%S", time.localtime(ts)))
            scr.addstr(ly + i, 10, f"{st}", curses.color_pair(col) | curses.A_BOLD)
            scr.addstr(ly + i, 14, f"{meth:6} {host}{uri}  ← {ip}  {dur * 1000:.0f}ms {human_bytes(size)}"[: w - 15])
        foot = _("[p] pause · [w] window · [c] clear · [b] block IP · [q] back")
        if not feed.alive:
            foot = "⚠ " + _("Log stream closed (container stopped?)") + " · " + foot
        scr.addstr(h - 1, 0, foot[: w - 1], curses.A_DIM)
        scr.refresh()

    # -- diagnostics and repair
    def act_diagnose(self):
        self.busy(_("Running checks (may take a few seconds)..."))
        items, fixes, need = diagnose()
        L = format_items(items) + ["", summary_line(items)]
        self.show(_("Diagnostics"), "\n".join(L))
        if not fixes:
            return
        self.draw()
        recreate = "recreate" in fixes
        if "start" in fixes and self.ask(_("Start the Caddy container?"), True):
            self.act_start()
            return
        if "compose" in fixes and not COMPOSE:
            self.show(_("Required docker compose changes"),
                      _("The container was not created with docker compose (or I cannot find its file; use --compose).\n"
                        "Apply these changes by hand and recreate the container:") + "\n\n" + compose_snippet(need))
            return
        if "compose" in fixes and self.ask(_("Update {file} with the required changes?").format(file=os.path.basename(COMPOSE)), True):
            try:
                path = COMPOSE
                with open(path, encoding="utf-8") as f:
                    old = f.read()
                new = patch_compose(old, SERVICE, need["ports"], need["hosts"], need["nets"])
                cand = os.path.join(os.path.dirname(path), f".compose.candidate.{os.getpid()}.yml")
                with open(cand, "w", encoding="utf-8") as f:
                    f.write(new)
                try:
                    rc, out = run(["docker", "compose", "-f", cand, "config", "-q"], timeout=30)
                finally:
                    os.unlink(cand)
                if rc != 0:
                    raise ValueError(_("The resulting compose file is not valid:") + "\n" + out)
                os.makedirs(BACKUPS, exist_ok=True)
                bk = os.path.join(BACKUPS, os.path.basename(COMPOSE) + "." + datetime.now().strftime("%Y%m%d-%H%M%S"))
                shutil.copy2(path, bk)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
                self.show(_("Compose updated"), "✔ " + _("{file} updated (backup: {path}).").format(file=COMPOSE, path=os.path.relpath(bk, BASE)))
                recreate = True
            except (OSError, ValueError) as e:
                self.show(_("Could not update the compose file automatically"),
                          f"{e}\n\n" + _("Add this by hand in {file}:").format(file=COMPOSE) + "\n\n" + compose_snippet(need))
                return
            self.draw()
        if recreate and self.ask(_("Recreate the Caddy container to apply the changes?"), True):
            self.act_compose(_("Recreate"), "up", "-d", "--force-recreate", "--no-deps", SERVICE)
            self.draw()
            if self.ask(_("Run the diagnostics again?"), True):
                time.sleep(3)
                self.act_diagnose()

    def act_env(self):
        if not os.path.exists(ENVFILE):
            example = os.path.join(BASE, ".env.example")
            if not self.ask(_("No .env exists. Create it from .env.example?") if os.path.exists(example)
                            else _("No .env exists. Create an empty one?")):
                return
            if os.path.exists(example):
                shutil.copy(example, ENVFILE)
            else:
                open(ENVFILE, "a").close()
        self.external_editor(ENVFILE)
        self.show(".env", _("If you changed variables the container uses, use [s] to recreate it with them."))

    def act_certs(self):
        state, __ = container_state()
        if state != "running":
            self.show(_("HTTPS certificates"), _("The container is not running. Use [s] to start it."))
            return
        self.busy(_("Reading certificates..."))
        with open(CADDYFILE, encoding="utf-8") as f:
            rows, __ = cert_report(f.read())
        L = format_items(rows) + ["", summary_line(rows)]
        self.show(_("HTTPS certificates"), "\n".join(L))

    def act_probe(self):
        state, __ = container_state()
        if state != "running":
            self.show(_("Probe services"), _("The container is not running. Use [s] to start it."))
            return
        self.busy(_("Probing the sites (this can take a few seconds)..."))
        with open(CADDYFILE, encoding="utf-8") as f:
            text = f.read()
        rows = probe_services(text, with_public=True)
        if not rows:
            self.show(_("Probe services"), _("There are no named sites to probe in the Caddyfile."))
            return
        self.show(_("Probe services"), "\n".join(format_items(rows) + ["", summary_line(rows)]))

    # -- IP blocklist
    def block_ip(self, raw, ban_seconds=0, reason=""):
        """Validate, confirm and add `raw` (IP or CIDR) to the blocklist (optionally for a limited time)."""
        try:
            ip = normalize_network(raw)
        except ValueError:
            self.show(_("Blocked IPs"), _("Not a valid IP or range (loopback and ranges wider than /8 are refused)."))
            return
        with open(CADDYFILE, encoding="utf-8") as f:
            text = f.read()
        current = get_blocked_ips(text)
        if ip in current:
            self.show(_("Blocked IPs"), _("{ip} is already blocked.").format(ip=ip))
            return
        if ipaddress.ip_network(ip, strict=False).is_private:
            self.draw()
            if not self.ask(_("{ip} is a private (LAN) address. Blocking it may lock you out. Continue?").format(ip=ip)):
                return
        self.draw()
        if not self.ask(_("Block {ip}? Its connections will be dropped on every site.").format(ip=ip), True):
            self.show(_("Blocked IPs"), _("Cancelled. Nothing was changed."))
            return
        if self._commit_caddyfile(set_blocked_ips(text, current + [ip]), _("{ip} blocked (previous copy: {path})."),
                                  title=_("Blocked IPs"), ip=ip) and (ban_seconds or reason):
            register_ban(ip, reason, ban_seconds)

    def act_blocklist(self):
        with open(CADDYFILE, encoding="utf-8") as f:
            text = f.read()
        ips = get_blocked_ips(text)
        bans = load_bans()
        lifted = expired_bans(bans, ips)
        if lifted:                                   # timed bans that ran out: lift them now
            ips = [x for x in ips if x not in lifted]
            if self._commit_caddyfile(set_blocked_ips(text, ips), _("Ban lifted ({n} expired) (previous copy: {path})."),
                                      title=_("Blocked IPs"), n=len(lifted)):
                for ip in lifted:
                    forget_ban(ip)
            with open(CADDYFILE, encoding="utf-8") as f:
                text = f.read()
            ips, bans = get_blocked_ips(text), load_bans()
        missing = sites_without_blocklist(text) if ips else []

        def describe(ip):
            b = bans.get(ip)
            if not b:
                return ""
            extra = ", ".join(x for x in (b.get("reason"), _("lifts {when}").format(when=time_left(b["until"])) if b.get("until") else "") if x)
            return f"  ({extra})" if extra else ""
        options = [("block", _("Block an IP or range…")), ("review", _("Review suspicious addresses…"))]
        options += [("unblock", _("Unblock {ip}").format(ip=ip) + describe(ip), ip) for ip in ips]
        if missing:
            options.append(("sync", _("Re-apply the blocklist to all sites")))
        options.append(("cancel", _("Cancel")))
        j = self.pick(_("Blocked IPs: {n}").format(n=len(ips)), [o[1] for o in options])
        if j is None or options[j][0] == "cancel":
            return
        kind = options[j][0]
        if kind == "block":
            fields = [dict(key="ip", label=_("IP or range (CIDR)"), kind="text", value="",
                           hint=_("E.g. 203.0.113.5 or 198.51.100.0/24"))]

            def check(v):
                try:
                    normalize_network(v["ip"])
                except ValueError:
                    return _("Not a valid IP or range (loopback and ranges wider than /8 are refused).")
                return None
            v = self.form(_("Block an IP or range"), fields, check)
            if v is not None:
                self.block_ip(v["ip"])
        elif kind == "review":
            self.review_suspicious()
        elif kind == "unblock":
            ip = options[j][2]
            if self._commit_caddyfile(set_blocked_ips(text, [x for x in ips if x != ip]),
                                      _("{ip} unblocked (previous copy: {path})."), title=_("Blocked IPs"), ip=ip):
                forget_ban(ip)
        else:
            self._commit_caddyfile(set_blocked_ips(text, ips), _("Blocklist re-applied to all sites (previous copy: {path})."),
                                   title=_("Blocked IPs"))

    def review_suspicious(self, events=None):
        """Look at the recent access log for scanners / brute-forcers and offer to ban one."""
        self.busy(_("Reading the access log..."))
        events = read_access_events("30m") if events is None else events
        with open(CADDYFILE, encoding="utf-8") as f:
            blocked = set(get_blocked_ips(f.read()))
        found = find_scanners(events, allow=allow_list(), blocked=blocked)
        if not found:
            self.show(_("Blocked IPs"), _("No suspicious addresses in the last 30 minutes."))
            return
        i = self.pick(_("Suspicious addresses (last 30 min)"),
                      [f"{o['ip'].ljust(39)} {o['requests']:>5} req · {reason_text(o['reasons'])}" for o in found])
        if i is None:
            return
        spans = [(_("For 24 hours"), 86400), (_("For 7 days"), 7 * 86400), (_("Permanently"), 0)]
        j = self.pick(_("Block {ip}").format(ip=found[i]["ip"]), [label for label, __ in spans])
        if j is None:
            return
        self.block_ip(found[i]["ip"], spans[j][1], reason_text(found[i]["reasons"]))

    def block_from_monitor(self, feed, window, paused_at):
        now = paused_at or time.time()
        ev = [e for e in feed.events if now - window[0] <= e[0] <= now]
        with open(CADDYFILE, encoding="utf-8") as f:
            blocked = set(get_blocked_ips(f.read()))
        counts, bad = Counter(e[4] for e in ev), Counter(e[4] for e in ev if e[5] >= 400)
        suspicious = {o["ip"]: o for o in find_scanners(ev, allow=allow_list(), blocked=blocked, min_requests=10)}
        top = [(ip, n) for ip, n in counts.most_common(15) if ip not in blocked and ip != "-"]
        top.sort(key=lambda t: t[0] not in suspicious)                 # suspicious ones first (stable: busiest first)
        if not top:
            return
        labels = []
        for ip, n in top:
            label = _("{ip}   {n} requests · {bad} with 4xx/5xx").format(ip=ip.ljust(39), n=n, bad=bad[ip])
            if ip in suspicious:
                label = "⚠ " + label + " · " + reason_text(suspicious[ip]["reasons"])
            labels.append(label)
        i = self.pick(_("Block which address?"), labels)
        self.scr.timeout(500)
        if i is not None:
            ip = top[i][0]
            self.block_ip(ip, 0, reason_text(suspicious[ip]["reasons"]) if ip in suspicious else "")
            self.scr.timeout(500)

    def act_image(self):
        state, __ = container_state()
        if state == "missing":
            self.show(_("Caddy image"), _("The container does not exist."))
            return
        self.busy(_("Checking for a newer Caddy image..."))
        info = check_image_update()
        lines = [_("Image: {ref}").format(ref=info["ref"]), _("Running version: {version}").format(version=info["version"]), ""]
        msg = {
            "current": ("✔", _("Up to date: the container runs the latest {ref}.").format(ref=info["ref"])),
            "update": ("⚠", _("A newer {ref} is available.").format(ref=info["ref"])),
            "pulled": ("⚠", _("A newer image is already pulled but the container still runs the old one. Recreate it to use it.")),
            "local": ("✔", _("This image was not pulled from a registry; there is nothing to compare it with.")),
            "unknown": ("⚠", _("Could not check for updates: {error}").format(error=info["error"])),
        }[info["status"]]
        lines.append(f"{msg[0]} {msg[1]}")
        self.show(_("Caddy image"), "\n".join(lines))
        if info["status"] not in ("update", "pulled"):
            return
        self.draw()
        question = (_("Pull the new image and recreate Caddy?") if info["status"] == "update"
                    else _("Recreate Caddy with the new image?"))
        if self.ask(question, True):
            self.update_image(info, pull=info["status"] == "update")

    def update_image(self, info, pull=True):
        """Pull, check the Caddyfile against the new version, then recreate the container."""
        old_version = info["version"]
        if pull:
            self.busy(_("Pulling the image..."))
            rc, out = run(compose("pull", SERVICE) if COMPOSE else ["docker", "pull", info["ref"]], timeout=900)
            if rc != 0:
                self.show(_("Caddy image") + ": ERROR", out)
                return
        self.busy(_("Validating your Caddyfile with the new image..."))
        with open(CADDYFILE, encoding="utf-8") as f:
            ok, out = validate_text(f.read())
        if not ok:
            self.show("✘ " + _("The new image rejects your Caddyfile; the container was left untouched."), out)
            return
        if not COMPOSE:
            self.show(_("Caddy image"), "✔ " + _("Image pulled. The container was not created with docker compose: recreate it yourself."))
            return
        self.busy(_("Recreate") + "...")
        rc, out = run(compose("up", "-d", "--force-recreate", "--no-deps", SERVICE), timeout=300)
        self.refresh_state(True)
        if rc != 0:
            self.show(_("Recreate") + ": ERROR", out)
            return
        time.sleep(2)
        old_id = info["image_id"].replace("sha256:", "")[:12]
        self.show(_("Caddy image"), "✔ " + _("Update finished: Caddy {old} → {new}.").format(old=old_version, new=caddy_version())
                  + ("\n" + _("Previous image: {id}. To roll back: docker tag {id} {ref} and recreate the container.").format(
                      id=old_id, ref=info["ref"]) if old_id else ""))

    def act_export(self):
        fields = [
            dict(key="path", label=_("Save to"), kind="text", value=os.path.join(os.getcwd(), default_export_name()),
                 hint=_("The file holds your .env: it is created readable only by you")),
            dict(key="certs", label=_("Include certificates"), kind="bool", value=True,
                 hint=_("Avoids asking Let's Encrypt again on the new server. Contains private keys!")),
            dict(key="alerts", label=_("Include the alerts settings"), kind="bool", value=False,
                 hint=_("Telegram token and chat (alert.env)")),
        ]

        def check(v):
            path = v["path"].strip()
            if not path or os.path.isdir(path) or not os.path.isdir(os.path.dirname(os.path.abspath(path))):
                return _("Enter a file path inside an existing folder")
            if os.path.exists(path):
                return _("That file already exists")
            return None
        v = self.form(_("Export the configuration"), fields, check)
        if v is None:
            self.show(_("Export"), _("Cancelled. Nothing was changed."))
            return
        self.busy(_("Exporting..."))
        path = os.path.abspath(v["path"].strip())
        manifest = build_export(path, v["certs"], v["alerts"])
        self.show(_("Export"), "✔ " + _("Exported to {path} ({size} KB).\nContains: {files}\nOn the new machine: caddy-tui --import {name}").format(
            path=path, size=max(1, os.path.getsize(path) // 1024), files=", ".join(sorted(manifest["files"])), name=os.path.basename(path)))

    def act_switch(self):
        rows = list_caddy_containers()
        if len(rows) < 2:
            self.show(_("Switch Caddy"), _("Only one Caddy container was found."))
            return
        labels = [("● " if n == CONTAINER else "  ") + f"{n}  ({img}, {state_label(st)})" for n, img, st in rows]
        i = self.pick(_("Choose the Caddy to manage"), labels)
        if i is None or rows[i][0] == CONTAINER:
            return
        ok, msg = configure_other(self.args or argparse.Namespace(), rows[i][0])
        if not ok:
            self.show(_("Switch Caddy"), "✘ " + msg)
            return
        update_config(container=CONTAINER)
        self.refresh_state(True)
        self.show(_("Switch Caddy"), "✔ " + _("Now managing {name}.").format(name=CONTAINER) + "\n"
                  + _("Caddyfile: {path}").format(path=CADDYFILE))

    def act_stats(self):
        state, __ = container_state()
        if state == "missing":
            self.show(_("Traffic statistics"), _("The container does not exist."))
            return
        periods = [("24h", _("Last 24 hours (per hour)")), ("7d", _("Last 7 days")), ("30d", _("Last 30 days")), ("90d", _("Last 90 days"))]
        i = self.pick(_("Traffic statistics"), [label for __, label in periods])
        if i is None:
            return
        self.busy(_("Reading the access log and updating the statistics..."))
        __, text = period_report(periods[i][0])
        self.show(_("Traffic statistics"), text)

    def act_language(self):
        langs = available_languages()
        new = langs[(langs.index(LANG) + 1) % len(langs)] if LANG in langs else "en"
        set_language(new)
        save_lang(new)
        self.show(_("Language"), _("Interface language: {lang}").format(lang=LANG_NAMES.get(new, new)))

    # -- main loop
    def loop(self):
        actions = {
            "a": self.act_add, "m": self.act_manage, "t": self.act_traffic, "i": self.act_blocklist, "d": self.act_diagnose, "h": self.act_certs, "p": self.act_probe, "w": self.act_stats, "y": self.act_image, "e": self.act_edit, "v": self.act_validate, "l": self.act_reload,
            "s": self.act_start, "g": self.act_logs, "f": self.act_format,
            "c": self.act_view, "b": self.act_restore, "u": self.act_undo, "k": self.act_backup, "o": self.act_export, "n": self.act_env,
            "x": lambda: self.act_compose(_("Stop"), "stop", SERVICE, confirm=_("Stop Caddy? The site will stop being served over HTTPS.")),
            "r": lambda: self.act_compose(_("Restart"), "restart", SERVICE, confirm=_("Restart Caddy? There will be a brief outage.")),
        }
        while True:
            self.refresh_state()
            self.draw()
            k = self.scr.getch()
            if k == -1:
                continue
            if k == curses.KEY_RESIZE:
                continue
            if k == curses.KEY_UP:
                self.scroll -= 1
            elif k == curses.KEY_DOWN:
                self.scroll += 1
            elif k == curses.KEY_PPAGE:
                self.scroll -= 10
            elif k == curses.KEY_NPAGE:
                self.scroll += 10
            elif k in (ord("q"), ord("Q")):
                return
            elif k == ord("L"):
                self.act_language()
            elif k == ord("S"):
                self.act_switch()
            elif 0 <= k < 256 and chr(k).lower() in actions:
                try:
                    actions[chr(k).lower()]()
                except OSError as e:
                    self.show(_("Error"), str(e))
                self.refresh_state(True)


def parse_args():
    env = os.environ.get
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang")
    set_language(pick_language(pre.parse_known_args()[0].lang))
    ap = argparse.ArgumentParser(description=_("TUI to manage a Caddy running in Docker."))
    ap.add_argument("--container", default=env("CADDY_CONTAINER"), help=_("name of the Caddy container"))
    ap.add_argument("--caddyfile", default=env("CADDY_CADDYFILE"), help=_("path of the Caddyfile on the host"))
    ap.add_argument("--dir", default=env("CADDY_DIR"), help=_("project folder (compose, .env, backups)"))
    ap.add_argument("--compose", default=env("CADDY_COMPOSE"), help=_("docker compose file"))
    ap.add_argument("--service", default=env("CADDY_SERVICE"), help=_("Caddy service in the compose file"))
    ap.add_argument("--image", default=env("CADDY_IMAGE"), help=_("image used to validate/format"))
    ap.add_argument("--lang", default=None, metavar="LANG", help=_(
        "interface language: auto or one of {langs} (default: saved preference, else system language)").format(
            langs=", ".join(available_languages())))
    ap.add_argument("--check", action="store_true", help=_(
        "run the diagnostics once, without the interface, and exit (0 ok, 1 warnings, 2 problems, 3 cannot run)"))
    ap.add_argument("--json", action="store_true", help=_("with --check: print the result as JSON"))
    ap.add_argument("-q", "--quiet", action="store_true", help=_(
        "with --check: print only warnings and problems (nothing if all is well)"))
    ap.add_argument("--notify", action="store_true", help=_(
        "with --check: send a Telegram message when there is a problem (and when it recovers)"))
    ap.add_argument("--setup-alerts", action="store_true", help=_(
        "interactive setup of the Telegram bot and the periodic check (needs root)"))
    ap.add_argument("--remove-alerts", action="store_true", help=_("remove the periodic check set up by --setup-alerts"))
    ap.add_argument("--interval", metavar="TIME", help=_("with --setup-alerts: check interval, e.g. 30m, 1h, 6h"))
    ap.add_argument("--notify-warnings", action="store_true", help=_("with --setup-alerts: notify on warnings too"))
    ap.add_argument("--install", action="store_true", help=_(
        "guided setup of a new Caddy in Docker (installs Docker first if needed)"))
    ap.add_argument("--auto-block", action="store_true", help=_(
        "ban scanners seen in the recent access log (and lift expired bans), then exit"))
    ap.add_argument("--ban-time", metavar="TIME", help=_("with --auto-block: ban length, e.g. 24h, 7d, 0 = permanent (default 24h)"))
    ap.add_argument("--window", metavar="TIME", help=_("with --auto-block: how far back to look in the log (default 15m)"))
    ap.add_argument("--dry-run", action="store_true", help=_("with --auto-block: show what would be banned, change nothing"))
    ap.add_argument("--export", nargs="?", const="", metavar="FILE", help=_(
        "write the configuration (Caddyfile, compose, .env...) to a file to move it to another server"))
    ap.add_argument("--with-certs", action="store_true", help=_("with --export: include the certificates and ACME account (private keys!)"))
    ap.add_argument("--with-alerts", action="store_true", help=_("with --export: include the alerts settings (Telegram token)"))
    ap.add_argument("--import", dest="import_file", metavar="FILE", help=_("restore an export made with --export into a new Caddy in Docker"))
    ap.add_argument("-y", "--yes", action="store_true", help=_("with --import: do not ask for confirmation"))
    ap.add_argument("--stats", nargs="?", const="7d", metavar="PERIOD", help=_(
        "print the traffic statistics of a period (24h, 7d, 30d, 90d; default 7d); --json for machines"))
    ap.add_argument("--collect-stats", action="store_true", help=_(
        "fold the access log since the last run into the statistics database, then exit (for the periodic timer)"))
    ap.add_argument("--all", action="store_true", help=_(
        "with --check / --auto-block: run for every Caddy container on this machine (exit code = the worst one)"))
    ap.add_argument("--list", action="store_true", help=_("list the Caddy containers found on this machine and exit"))
    ap.add_argument("--version", action="version", version=f"caddy-tui {__version__}")
    return ap.parse_args()


def main():
    if "--notify" in sys.argv or "--auto-block" in sys.argv or "--collect-stats" in sys.argv:
        load_alert_env()
    args = parse_args()
    if args.notify:
        args.check = True
    if args.remove_alerts:
        sys.exit(remove_alerts(args))
    if args.setup_alerts and os.geteuid() != 0:
        sys.exit(_("Setting up alerts needs root. Run it with sudo."))
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not (args.check or args.setup_alerts or args.auto_block)
    if args.import_file:
        interactive = interactive or args.yes
    if (args.install or (args.import_file and not args.yes)) and not interactive:
        sys.exit(_("This program needs an interactive terminal."))
    if not shutil.which("docker"):
        if interactive:
            if not ensure_docker():
                sys.exit(1)
        else:
            msg = _("Docker is not installed or not in PATH.")
            if args.check or args.auto_block:
                print(msg, file=sys.stderr)
                sys.exit(3)
            sys.exit(msg)
    if not ensure_docker_running(interactive and not args.setup_alerts, None if interactive else sys.stderr):
        sys.exit(3 if (args.check or args.auto_block) else 1)
    if args.list:
        sys.exit(print_caddies())
    if args.stats is not None or args.collect_stats:
        func = run_collect_stats if args.collect_stats else run_stats
        if args.all:
            sys.exit(run_for_all(args, func))
        try:
            configure(args)
        except SystemExit as e:
            print(e.code, file=sys.stderr)
            sys.exit(3)
        sys.exit(func(args))
    if args.all and (args.check or args.auto_block):
        collect = [] if (args.json and not args.auto_block) else None
        code = run_for_all(args, run_auto_block if args.auto_block else (lambda a: run_check(a, collect)), collect)
        if collect is not None:
            print(json.dumps({"status": ("ok", "warning", "critical", "critical")[min(code, 3)] if code < 3 else "unknown",
                              "containers": collect}, ensure_ascii=False, indent=2))
        sys.exit(code)
    if args.export is not None:
        try:
            configure(args)
        except SystemExit as e:
            print(e.code, file=sys.stderr)
            sys.exit(3)
        path = os.path.abspath(args.export or default_export_name())
        if os.path.exists(path):
            sys.exit(_("{path} already exists.").format(path=path))
        manifest = build_export(path, args.with_certs, args.with_alerts)
        print("✔ " + _("Exported to {path} ({size} KB).\nContains: {files}\nOn the new machine: caddy-tui --import {name}").format(
            path=path, size=max(1, os.path.getsize(path) // 1024), files=", ".join(sorted(manifest["files"])), name=os.path.basename(path)))
        sys.exit(0)
    if args.import_file:
        name = import_flow(args, args.import_file)
        if not name:
            sys.exit(1)
        if not interactive or not sys.stdout.isatty():
            sys.exit(0)
        args.container, args.dir = name, None
    if args.install:
        name = install_flow(args)
        if not name:
            sys.exit(0)
        args.container, args.dir = name, None
    if args.setup_alerts:
        sys.exit(setup_alerts(args))
    if args.check or args.auto_block:
        try:
            configure(args)
        except SystemExit as e:
            print(e.code, file=sys.stderr)
            sys.exit(3)
        sys.exit(run_auto_block(args) if args.auto_block else run_check(args))
    if not sys.stdout.isatty():
        sys.exit(_("This program needs an interactive terminal."))
    try:
        configure(args)
    except NoCaddyContainer as e:
        print(e.code)
        if not (interactive and ask_yes(_("Set up a new Caddy now with the guided installer?"), True)):
            sys.exit(1)
        name = install_flow(args)
        if not name:
            sys.exit(1)
        args.container, args.dir = name, None
        configure(args)
    update_config(container=CONTAINER)
    curses.wrapper(lambda scr: App(scr, args).loop())


if __name__ == "__main__":
    main()
