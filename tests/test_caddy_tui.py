import argparse
import ast
import http.server
import shutil
import socket
import ssl
import subprocess
import threading
import contextlib
import email.message
import io
import importlib.util
import json
import os
import re
import stat
import tarfile
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("caddy_tui", os.path.join(HERE, "..", "caddy-tui.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

CADDYFILE = """\
(seguridad) {
\tencode gzip
}

{$DOMAIN} {
\timport seguridad
\treverse_proxy fileshare:3000
}

{$DOMAIN}:8443 {
\treverse_proxy https://host.docker.internal:8444 {
\t\ttransport http {
\t\t\ttls_insecure_skip_verify
\t\t}
\t}
}

app.example.com:2283 {
\treverse_proxy immich_server:2283 /api/*
}
"""

COMPOSE = """\
services:
  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
    networks:
      - internal

networks:
  internal:

volumes:
  caddy_data:
"""


class Parsing(unittest.TestCase):
    def test_site_addresses_ignora_snippets_y_globales(self):
        self.assertEqual(c.site_addresses(CADDYFILE),
                         ["{$DOMAIN}", "{$DOMAIN}:8443", "app.example.com:2283"])

    def test_site_ports(self):
        self.assertEqual(c.site_ports(CADDYFILE), [80, 443, 2283, 8443])

    def test_parse_upstreams(self):
        self.assertEqual(c.parse_upstreams(CADDYFILE), [
            ("fileshare", "3000", False),
            ("host.docker.internal", "8444", True),
            ("immich_server", "2283", False),
        ])

    def test_referenced_vars(self):
        self.assertEqual(c.referenced_vars("{$A} {$B:x} {$A}"), [("A", False), ("B", True)])

    def test_human_bytes(self):
        self.assertEqual(c.human_bytes(512), "512 B")
        self.assertEqual(c.human_bytes(2048), "2.0 KB")


class PatchCompose(unittest.TestCase):
    ARGS = dict(ports=[8443, 2283], extra_hosts=["host.docker.internal:host-gateway"],
                networks=["immich_default"])

    def test_anade_puertos_hosts_y_redes(self):
        out = c.patch_compose(COMPOSE, "caddy", **self.ARGS)
        for frag in ('- "8443:8443"', '- "2283:2283"', 'extra_hosts:', '- "host.docker.internal:host-gateway"',
                     "- immich_default", "  immich_default:\n    external: true"):
            self.assertIn(frag, out)
        self.assertIn("volumes:\n  caddy_data:", out)

    def test_es_idempotente(self):
        once = c.patch_compose(COMPOSE, "caddy", **self.ARGS)
        self.assertEqual(once, c.patch_compose(once, "caddy", **self.ARGS))

    def test_servicio_inexistente(self):
        with self.assertRaises(ValueError):
            c.patch_compose(COMPOSE, "otro", ports=[1])


class SiteBlock(unittest.TestCase):
    BASE = dict(host="app.example.com", upstream="10.0.0.5:8080", https_upstream=False,
                skip_verify=False, gzip=True, headers=True, log=True, user="", password="")

    def test_bloque_basico(self):
        blk = c.build_site_block(self.BASE)
        self.assertIn("app.example.com {", blk)
        self.assertIn("reverse_proxy 10.0.0.5:8080", blk)
        self.assertIn("encode gzip", blk)
        self.assertIn("\tlog", blk)
        self.assertNotIn("basic_auth", blk)

    def test_https_sin_verificar_y_auth(self):
        v = dict(self.BASE, https_upstream=True, skip_verify=True, user="bob")
        blk = c.build_site_block(v, "$2a$14$hash")
        self.assertIn("reverse_proxy https://10.0.0.5:8080 {", blk)
        self.assertIn("tls_insecure_skip_verify", blk)
        self.assertIn("bob $2a$14$hash", blk)

    def test_host_valido(self):
        self.assertTrue(c.HOST_RE.match("app.example.com"))
        self.assertTrue(c.HOST_RE.match("{$DOMAIN}"))
        self.assertFalse(c.HOST_RE.match("host con espacios"))



def read_po(path):
    """Minimal .po parser -> {msgid: msgstr} (no plurals/contexts needed here)."""
    entries, cur, key = {}, {"msgid": "", "msgstr": ""}, None
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        m = re.match(r'^(msgid|msgstr) (".*")$', line)
        if m:
            if m.group(1) == "msgid" and key == "msgstr":
                entries[cur["msgid"]] = cur["msgstr"]
                cur = {"msgid": "", "msgstr": ""}
            key = m.group(1)
            cur[key] = json.loads(m.group(2))
        elif line.startswith('"') and key:
            cur[key] += json.loads(line)
    entries[cur["msgid"]] = cur["msgstr"]
    entries.pop("", None)
    return entries


def source_msgids():
    """Every literal passed to _() / N_() in the source."""
    tree = ast.parse(open(os.path.join(HERE, "..", "caddy-tui.py"), encoding="utf-8").read())
    return {n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("_", "N_")
            and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)}


LEAF_PEM = """-----BEGIN CERTIFICATE-----
MIIBtDCCAVmgAwIBAgIUKxTVL12EGfvI1dYlyELpyNzTEqswCgYIKoZIzj0EAwIw
KjEVMBMGA1UEAwwMZXhhbXBsZS50ZXN0MREwDwYDVQQKDAhUZXN0IE9yZzAgFw0y
NjEwMTcxOTU2NDNaGA8yMTI2MDkyMzE5NTY0M1owKjEVMBMGA1UEAwwMZXhhbXBs
ZS50ZXN0MREwDwYDVQQKDAhUZXN0IE9yZzBZMBMGByqGSM49AgEGCCqGSM49AwEH
A0IABAujQyjzTT2l8mP40w5p36jTHnwEvFIctS1XwnoCDpVK6uxGMnZzAPGyW84D
+G5DYRXQDKadQz8MSkBsivbphHqjWzBZMB0GA1UdDgQWBBS+t1jYJp/gp/kDuQeT
li9Ku4kMwTAPBgNVHRMBAf8EBTADAQH/MCcGA1UdEQQgMB6CDGV4YW1wbGUudGVz
dIIOKi5leGFtcGxlLnRlc3QwCgYIKoZIzj0EAwIDSQAwRgIhAKRM8kz1H42/ymhq
SeqhG4zKiB0fbHWSzJoHlrbwrsvvAiEA+x5Bn43Y9c69Mra0FCnqKEN5gfpeSs3Y
OHFdYk79B2o=
-----END CERTIFICATE-----
"""
ROOT_PEM = """-----BEGIN CERTIFICATE-----
MIIBXTCCAQSgAwIBAgIUTQqDZQAHFtkMYYhvFwAt+vmCvx0wCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJVGVzdCBSb290MCAXDTI2MTAxNzE5NTY0M1oYDzIxMjYwOTIz
MTk1NjQzWjAUMRIwEAYDVQQDDAlUZXN0IFJvb3QwWTATBgcqhkjOPQIBBggqhkjO
PQMBBwNCAAQLo0Mo8009pfJj+NMOad+o0x58BLxSHLUtV8J6Ag6VSursRjJ2cwDx
slvOA/huQ2EV0AymnUM/DEpAbIr26YR6ozIwMDAdBgNVHQ4EFgQUvrdY2Caf4Kf5
A7kHk5YvSruJDMEwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNHADBEAiBb
oGxJFdNyr7yrDGQLcZiiMGtWy63JfqUQ6P5nsJ8fngIgM7RqoQ5XC01I2zc+c8W0
EltKuHE+xAVsb/R/mRdqOCY=
-----END CERTIFICATE-----
"""


class Certificates(unittest.TestCase):
    def test_parsea_hoja_de_un_bundle(self):
        certs = c.parse_pem_certificates(LEAF_PEM + ROOT_PEM)
        self.assertEqual(len(certs), 2)
        leaf = certs[0]
        self.assertEqual(leaf["names"], ["example.test", "*.example.test"])
        self.assertEqual(leaf["cn"], "example.test")
        self.assertEqual(leaf["issuer"], "example.test")
        self.assertEqual(leaf["not_after"].year, 2126)
        self.assertEqual(certs[1]["names"], ["Test Root"])   # sin SAN -> CN

    def test_basura_se_ignora(self):
        self.assertEqual(c.parse_pem_certificates("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"), [])
        self.assertEqual(c.parse_pem_certificates("nada"), [])

    def test_nombres_internos_como_caddy(self):
        for host in ("localhost", "app.localhost", "nas.local", "x.internal", "r.home.arpa", "single"):
            self.assertTrue(c.is_internal_name(host), host)
        for host in ("app.example.com", "nas.lan", "x.test"):          # these go to ACME (and fail)
            self.assertFalse(c.is_internal_name(host), host)

    def test_cert_covers(self):
        self.assertTrue(c.cert_covers(["a.example.com"], "a.example.com"))
        self.assertTrue(c.cert_covers(["*.example.com"], "a.example.com"))
        self.assertFalse(c.cert_covers(["*.example.com"], "a.b.example.com"))
        self.assertFalse(c.cert_covers(["a.example.com"], "b.example.com"))

    def test_site_domains(self):
        text = ("{$DOMAIN}:8443 {\n}\nhttp://plain.example.com {\n}\nlocalhost {\n}\nnas.local {\n}\n"
                "192.168.1.5 {\n}\n*.wild.example.com {\n}\napp.example.com:2283 {\n}\n")
        with mock.patch.object(c, "runtime_env", return_value={"DOMAIN": "Main.Example.com"}):
            self.assertEqual(c.site_domains(text), ["main.example.com", "app.example.com"])


class CertReport(unittest.TestCase):
    NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def cert(self, days, names=("a.example.com",), internal=False):
        return {"names": list(names), "cn": names[0], "issuer": "R11", "internal": internal, "file": "x",
                "not_after": self.NOW + timedelta(days=days), "not_before": self.NOW - timedelta(days=60)}

    def report(self, certs, text="", errs=None, domains=()):
        with mock.patch.object(c, "read_certificates", return_value=certs), \
             mock.patch.object(c, "site_domains", return_value=list(domains)), \
             mock.patch.object(c, "acme_errors", return_value=errs or {}):
            return c.cert_report(text, now=self.NOW)

    def levels(self, certs, **kw):
        return [r[0] for r in self.report(certs, **kw)[0]]

    def test_umbrales(self):
        self.assertEqual(self.levels([self.cert(60)]), ["ok"])
        self.assertEqual(self.levels([self.cert(19)]), ["warn"])
        self.assertEqual(self.levels([self.cert(6)]), ["err"])
        self.assertEqual(self.levels([self.cert(-1)]), ["err"])

    def test_ca_interna_nunca_alerta(self):
        self.assertEqual(self.levels([self.cert(0.1, internal=True)]), ["ok"])

    def test_dominio_sin_certificado(self):
        rows, n = self.report([], domains=["new.example.com"])
        self.assertEqual((rows[0][0], n), ("warn", 0))
        self.assertIn("new.example.com", rows[0][1])

    def test_dominio_cubierto_por_wildcard(self):
        rows, __ = self.report([self.cert(60, names=("*.example.com",))], domains=["new.example.com"])
        self.assertEqual([r[0] for r in rows], ["ok"])

    def test_error_acme_es_error_con_detalle(self):
        rows, __ = self.report([], domains=["new.example.com"], errs={"new.example.com": "dns problem"})
        self.assertEqual(rows[0][0], "err")
        self.assertIn("dns problem", rows[0][2][0])

    def test_almacen_ilegible(self):
        rows, n = self.report(None)
        self.assertEqual((rows[0][0], n), ("warn", 0))

    def test_sin_certificados(self):
        rows, __ = self.report([])
        self.assertEqual(rows[0][0], "ok")


class HeadlessCheck(unittest.TestCase):
    ITEMS_OK = [("ok", "all fine", [])]
    ITEMS_WARN = [("ok", "fine", []), ("warn", "careful", ["detail"])]
    ITEMS_ERR = [("warn", "careful", []), ("err", "broken", ["why", "how"])]

    def run_check(self, items, **opts):
        args = argparse.Namespace(json=opts.get("json", False), quiet=opts.get("quiet", False))
        out = io.StringIO()
        with mock.patch.object(c, "diagnose", return_value=(items, set(), {})), contextlib.redirect_stdout(out):
            code = c.run_check(args)
        return code, out.getvalue()

    def test_codigos_de_salida(self):
        self.assertEqual(self.run_check(self.ITEMS_OK)[0], 0)
        self.assertEqual(self.run_check(self.ITEMS_WARN)[0], 1)
        self.assertEqual(self.run_check(self.ITEMS_ERR)[0], 2)

    def test_quiet_no_imprime_nada_si_todo_va_bien(self):
        self.assertEqual(self.run_check(self.ITEMS_OK, quiet=True), (0, ""))

    def test_quiet_solo_muestra_problemas(self):
        __, out = self.run_check(self.ITEMS_WARN, quiet=True)
        self.assertIn("careful", out)
        self.assertNotIn("fine", out)

    def test_texto_completo(self):
        __, out = self.run_check(self.ITEMS_ERR)
        self.assertIn("✘ broken\n    why\n    how", out)
        self.assertIn("1 problem(s), 1 warning(s)", out)

    def test_json(self):
        code, out = self.run_check(self.ITEMS_ERR, json=True)
        data = json.loads(out)
        self.assertEqual((code, data["status"], data["problems"], data["warnings"]), (2, "critical", 1, 1))
        self.assertEqual(data["checks"][1], {"level": "err", "title": "broken", "details": ["why", "how"]})
        self.assertEqual(json.loads(self.run_check(self.ITEMS_OK, json=True)[1])["status"], "ok")

    def test_format_items(self):
        self.assertEqual(c.format_items(self.ITEMS_WARN), ["✔ fine", "⚠ careful", "    detail"])


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class AlertHelpers(unittest.TestCase):
    def test_parse_interval(self):
        self.assertEqual(c.parse_interval("30m"), 30)
        self.assertEqual(c.parse_interval("1h"), 60)
        self.assertEqual(c.parse_interval(" 2 hours "), 120)
        self.assertEqual(c.parse_interval("90"), 90)
        self.assertEqual(c.parse_interval("45min"), 45)
        for bad in ("4m", "25h", "abc", "", "1d", "-5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                c.parse_interval(bad)

    def test_cron_schedule(self):
        self.assertEqual(c.cron_schedule(30), "*/30 * * * *")
        self.assertEqual(c.cron_schedule(60), "0 */1 * * *")
        self.assertEqual(c.cron_schedule(360), "0 */6 * * *")
        self.assertEqual(c.cron_schedule(1440), "0 0 * * *")

    def test_unidades(self):
        svc = c.render_service("/usr/bin/caddy-tui")
        self.assertIn("ExecStart=/usr/bin/caddy-tui --check --quiet --notify", svc)
        self.assertIn("SuccessExitStatus=1", svc)
        self.assertIn("OnUnitActiveSec=45min", c.render_timer(45))
        self.assertIn("root /usr/bin/caddy-tui --check --quiet --notify", c.render_cron("/usr/bin/caddy-tui", 30))

    def test_env_file_roundtrip_y_permisos(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "alert.env")
            c.write_env_file(path, {"TELEGRAM_BOT_TOKEN": "1:abc", "EMPTY": "", "CADDY_CONTAINER": "my-caddy"})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(c.read_env_file(path), {"TELEGRAM_BOT_TOKEN": "1:abc", "CADDY_CONTAINER": "my-caddy"})
        self.assertEqual(c.read_env_file("/no/such/file"), {})

    def test_telegram_api_no_filtra_el_token(self):
        err = c.urllib.error.URLError("failed https://api.telegram.org/bot123:SECRET/getMe")
        with mock.patch.object(c.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                c.telegram_api("123:SECRET", "getMe")
        self.assertNotIn("SECRET", str(cm.exception))

    def test_telegram_api_error_de_la_api(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.StringIO('{"ok": false, "description": "Unauthorized"}')
        with mock.patch.object(c.urllib.request, "urlopen", return_value=resp):
            with self.assertRaisesRegex(RuntimeError, "Unauthorized"):
                c.telegram_api("1:x", "getMe")


class AlertDecision(unittest.TestCase):
    ERR = [("err", "Port 8100 missing", [])]
    ERR2 = [("err", "Another problem", [])]
    OK = [("ok", "fine", [])]
    WARN = [("warn", "careful", [])]
    H = 3600

    def decide(self, code, items, state, now=0, **kw):
        return c.alert_decision(code, items, state, now, **kw)

    def test_problema_nuevo_avisa(self):
        kind, st = self.decide(2, self.ERR, {}, now=100)
        self.assertEqual(kind, "problem")
        self.assertTrue(st["failing"])

    def test_mismo_problema_no_repite_hasta_pasar_24h(self):
        __, st = self.decide(2, self.ERR, {}, now=0)
        self.assertEqual(self.decide(2, self.ERR, st, now=23 * self.H)[0], None)
        self.assertEqual(self.decide(2, self.ERR, st, now=25 * self.H)[0], "problem")

    def test_problema_distinto_avisa_enseguida(self):
        __, st = self.decide(2, self.ERR, {}, now=0)
        self.assertEqual(self.decide(2, self.ERR2, st, now=60)[0], "problem")

    def test_numeros_cambiantes_no_cuentan_como_problema_nuevo(self):
        a = [("warn", "5 errors in the logs", [])]
        b = [("warn", "9 errors in the logs", [])]
        __, st = self.decide(2, a, {}, now=0)
        self.assertIsNone(self.decide(2, b, st, now=60)[0])

    def test_recuperacion(self):
        __, st = self.decide(2, self.ERR, {}, now=0)
        kind, st2 = self.decide(0, self.OK, st, now=60)
        self.assertEqual(kind, "recovered")
        self.assertIsNone(self.decide(0, self.OK, st2, now=120)[0])

    def test_avisos_solo_si_se_pide(self):
        self.assertIsNone(self.decide(1, self.WARN, {})[0])
        self.assertEqual(self.decide(1, self.WARN, {}, on_warnings=True)[0], "problem")

    def test_todo_bien_sin_historial_no_avisa(self):
        self.assertEqual(self.decide(0, self.OK, {}), (None, {}))


class NotifyResult(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "42", "CADDY_TUI_STATE_DIR": self.tmp.name}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.sent = []

    def notify(self, items, code, fail=False):
        def fake_send(token, chat, text):
            if fail:
                raise RuntimeError("boom")
            self.sent.append(text)
        with mock.patch.object(c, "telegram_send", side_effect=fake_send), \
             contextlib.redirect_stderr(io.StringIO()):
            c.notify_result(items, code)

    ERR = [("err", "Port 8100 missing", ["detail"])]

    def test_envia_una_vez_y_avisa_al_recuperarse(self):
        self.notify(self.ERR, 2)
        self.notify(self.ERR, 2)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Port 8100 missing", self.sent[0])
        self.notify([("ok", "fine", [])], 0)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("OK again", self.sent[1])

    def test_si_falla_el_envio_reintenta_en_la_siguiente(self):
        self.notify(self.ERR, 2, fail=True)
        self.notify(self.ERR, 2)
        self.assertEqual(len(self.sent), 1)

    def test_sin_configurar_no_envia(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.notify(self.ERR, 2)
        self.assertEqual(self.sent, [])


class SetupAndRemove(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.env_path, self.sysd = os.path.join(d, "etc", "alert.env"), os.path.join(d, "systemd")
        os.makedirs(self.sysd)
        self.calls, self.sent = [], []
        patches = [
            mock.patch.object(c, "ALERT_ENV", self.env_path), mock.patch.object(c, "SYSTEMD_DIR", self.sysd),
            mock.patch.object(c, "CRON_FILE", os.path.join(d, "cron-caddy")),
            mock.patch.object(c, "_systemd_available", return_value=True),
            mock.patch.object(c, "configure", side_effect=lambda a: setattr(c, "CONTAINER", "my-caddy")),
            mock.patch.object(c, "run", side_effect=lambda cmd, **k: (self.calls.append(cmd), (0, ""))[1]),
            mock.patch.object(c, "diagnose", return_value=([("ok", "fine", [])], set(), {})),
            mock.patch.object(c, "telegram_api", side_effect=lambda t, m, p=None, timeout=15:
                              {"username": "mybot"} if m == "getMe" else [{"message": {"chat": {"id": 99, "title": "Me"}}}]),
            mock.patch.object(c, "telegram_send", side_effect=lambda t, ch, txt: self.sent.append((ch, txt))),
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(c.getpass, "getpass", return_value="123:tok"),
            mock.patch.dict(os.environ, {}, clear=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.args = argparse.Namespace(interval="30m", notify_warnings=True, caddyfile=None, dir=None, compose=None, service=None)

    def run_setup(self, inputs):
        with mock.patch("builtins.input", side_effect=inputs), contextlib.redirect_stdout(io.StringIO()) as out:
            code = c.setup_alerts(self.args)
        return code, out.getvalue()

    def test_configura_servicio_timer_y_mensaje(self):
        # chat id vacío -> se detecta; Enter; acepta el chat encontrado
        code, out = self.run_setup(["", "", "y", "y", "n"])
        self.assertEqual(code, 0)
        env = c.read_env_file(self.env_path)
        self.assertEqual((env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], env["CADDY_CONTAINER"], env["ALERT_ON_WARNINGS"]),
                         ("123:tok", "99", "my-caddy", "1"))
        self.assertEqual(stat.S_IMODE(os.stat(self.env_path).st_mode), 0o600)
        self.assertIn("OnUnitActiveSec=30min", slurp(os.path.join(self.sysd, "caddy-tui-check.timer")))
        self.assertIn("--check --quiet --notify", slurp(os.path.join(self.sysd, "caddy-tui-check.service")))
        self.assertIn(["systemctl", "enable", "--now", "caddy-tui-check.timer"], self.calls)
        self.assertEqual(self.sent[0][0], "99")
        self.assertIn("@mybot", out)

    def test_con_varios_caddy_ofrece_vigilarlos_todos(self):
        rows = [("my-caddy", "caddy:2", "running"), ("other", "caddy:2", "running")]
        with mock.patch.object(c, "list_caddy_containers", return_value=rows):
            self.run_setup(["", "", "y", "y", "y", "n"])            # chat found ; this chat ; monitor all ; stats ; no auto-block
        env = c.read_env_file(self.env_path)
        self.assertEqual(env["CADDY_ALL"], "1")
        self.assertNotIn("CADDY_CONTAINER", env)                    # not pinned to one container
        self.assertIn("--check --quiet --notify --all", slurp(os.path.join(self.sysd, "caddy-tui-check.service")))

    def test_con_varios_caddy_se_puede_dejar_solo_uno(self):
        rows = [("my-caddy", "caddy:2", "running"), ("other", "caddy:2", "running")]
        with mock.patch.object(c, "list_caddy_containers", return_value=rows):
            self.run_setup(["", "", "y", "n", "y", "n"])
        env = c.read_env_file(self.env_path)
        self.assertEqual((env.get("CADDY_ALL"), env["CADDY_CONTAINER"]), (None, "my-caddy"))
        self.assertNotIn("--all", slurp(os.path.join(self.sysd, "caddy-tui-check.service")))

    def test_con_autobloqueo(self):
        code, __ = self.run_setup(["", "", "y", "y", "y", "7d"])
        env = c.read_env_file(self.env_path)
        self.assertEqual((env["AUTOBLOCK"], env["AUTOBLOCK_BAN"], env["AUTOBLOCK_WINDOW"]), ("1", "7d", "30m"))
        self.assertIn("ExecStartPre=-", slurp(os.path.join(self.sysd, "caddy-tui-check.service")))

    def test_sin_autobloqueo_no_hay_variables(self):
        self.run_setup(["", "", "y", "y", "n"])
        self.assertNotIn("AUTOBLOCK", c.read_env_file(self.env_path))
        unit = slurp(os.path.join(self.sysd, "caddy-tui-check.service"))
        self.assertNotIn("--auto-block", unit)
        self.assertIn("--collect-stats --quiet", unit)                      # statistics were accepted
        self.assertEqual(c.read_env_file(self.env_path)["STATS"], "1")

    def test_sin_estadisticas(self):
        self.run_setup(["", "", "y", "n", "n"])
        self.assertNotIn("STATS", c.read_env_file(self.env_path))
        self.assertNotIn("--collect-stats", slurp(os.path.join(self.sysd, "caddy-tui-check.service")))

    def test_sin_systemd_usa_cron(self):
        with mock.patch.object(c, "_systemd_available", return_value=False):
            self.run_setup(["", "", "y", "y", "n"])
        self.assertIn("*/30 * * * *", slurp(c.CRON_FILE))
        self.assertEqual(os.listdir(self.sysd), [])

    def test_requiere_root(self):
        with mock.patch.object(os, "geteuid", return_value=1000), self.assertRaises(SystemExit):
            c.setup_alerts(self.args)

    def test_intervalo_invalido_aborta_sin_escribir(self):
        self.args.interval = "2m"
        with self.assertRaises(SystemExit):
            self.run_setup(["42"])
        self.assertFalse(os.path.exists(self.env_path))

    def test_quitar(self):
        self.run_setup(["", "", "y", "y", "n"])
        with mock.patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
            c.remove_alerts(self.args)
        self.assertEqual(os.listdir(self.sysd), [])
        self.assertFalse(os.path.exists(self.env_path))
        self.assertIn(["systemctl", "disable", "--now", "caddy-tui-check.timer"], self.calls)


MANAGE_CADDYFILE = """\
{
\temail admin@example.com
}

(seguridad) {
\tencode gzip
}

# my own comment { with a brace
{$DOMAIN} {
\timport seguridad
\treverse_proxy fileshare:3000
}

# Service added with caddy-tui (2026-01-01 10:00)
app.example.com {
\treverse_proxy 10.0.0.5:8080
\tencode gzip
\theader {
\t\tStrict-Transport-Security "max-age=31536000; includeSubDomains"
\t\tX-Content-Type-Options "nosniff"
\t\tX-Frame-Options "DENY"
\t}
\tlog
}

a.example.com, b.example.com {
\trespond "hi"
}
"""


class ManageBlocks(unittest.TestCase):
    def blocks(self, text=MANAGE_CADDYFILE):
        return c.find_site_blocks(text)

    def test_encuentra_sitios_y_omite_global_y_snippets(self):
        self.assertEqual([b["addr"] for b in self.blocks()],
                         [["{$DOMAIN}"], ["app.example.com"], ["a.example.com", "b.example.com"]])

    def test_marcador_propio(self):
        blocks = self.blocks()
        self.assertIsNone(blocks[0]["marker"])           # comentario ajeno: no es nuestro
        self.assertEqual(MANAGE_CADDYFILE.split("\n")[blocks[1]["marker"]][:9], "# Service")

    def test_llaves_en_comentarios_no_descuadran(self):
        self.assertEqual(len(self.blocks()), 3)

    def test_site_addresses_sigue_igual(self):
        self.assertEqual(c.site_addresses(MANAGE_CADDYFILE),
                         ["{$DOMAIN}", "app.example.com", "a.example.com", "b.example.com"])

    def test_bloque_del_asistente_es_simple_y_ida_y_vuelta(self):
        lines = MANAGE_CADDYFILE.split("\n")
        b = self.blocks()[1]
        v, pw = c.parse_simple_block(lines[b["start"]:b["end"] + 1])
        self.assertEqual((v["host"], v["upstream"], v["gzip"], v["headers"], v["log"], pw),
                         ("app.example.com", "10.0.0.5:8080", True, True, True, None))
        self.assertEqual([x.strip() for x in c.build_site_block(v, pw).rstrip("\n").split("\n")[1:]],
                         [x.strip() for x in lines[b["start"]:b["end"] + 1]])

    def test_con_https_auth_y_sin_verificar(self):
        v = dict(host="x.example.com", upstream="10.0.0.9:8443", https_upstream=True, skip_verify=True,
                 gzip=False, headers=False, log=True, user="bob", password="")
        blk = c.build_site_block(v, "$2a$14$abc").split("\n")
        parsed, pw = c.parse_simple_block(blk)
        self.assertEqual((parsed["https_upstream"], parsed["skip_verify"], parsed["user"], pw, parsed["upstream"]),
                         (True, True, "bob", "$2a$14$abc", "10.0.0.9:8443"))

    def test_bloques_a_mano_no_son_simples(self):
        lines = MANAGE_CADDYFILE.split("\n")
        for i in (0, 2):                                  # usa import / varias direcciones
            b = self.blocks()[i + 0 if i == 0 else 2]
            self.assertIsNone(c.parse_simple_block(lines[b["start"]:b["end"] + 1]))

    def test_quitar_bloque_con_su_marcador(self):
        new = c.remove_block(MANAGE_CADDYFILE, self.blocks()[1])
        self.assertNotIn("app.example.com", new)
        self.assertNotIn("# Service added", new)
        self.assertNotIn("\n\n\n", new)
        self.assertEqual([b["addr"][0] for b in c.find_site_blocks(new)], ["{$DOMAIN}", "a.example.com"])
        self.assertTrue(new.endswith("}\n"))

    def test_quitar_no_borra_comentarios_ajenos(self):
        self.assertIn("# my own comment", c.remove_block(MANAGE_CADDYFILE, self.blocks()[0]))

    def test_reemplazar_bloque(self):
        b = self.blocks()[2]
        new = c.replace_block(MANAGE_CADDYFILE, b, ['c.example.com {', '\trespond "x"', '}'])
        self.assertIn("c.example.com {", new)
        self.assertNotIn("a.example.com", new)

    def test_etiqueta(self):
        lines = MANAGE_CADDYFILE.split("\n")
        b = self.blocks()
        self.assertIn("app.example.com  →  10.0.0.5:8080", c.block_label(lines[b[1]["start"]:b[1]["end"] + 1]))
        self.assertIn("(custom block)", c.block_label(lines[b[2]["start"]:b[2]["end"] + 1]))


class ManageFlow(unittest.TestCase):
    """act_manage end to end with the UI primitives replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.caddyfile = os.path.join(d, "Caddyfile")
        with open(self.caddyfile, "w", encoding="utf-8") as f:
            f.write(MANAGE_CADDYFILE)
        for name, val in (("CADDYFILE", self.caddyfile), ("BASE", d), ("BACKUPS", os.path.join(d, "bk"))):
            p = mock.patch.object(c, name, val)
            p.start()
            self.addCleanup(p.stop)
        for name, kw in (("validate_text", dict(return_value=(True, ""))),
                         ("container_state", dict(return_value=("exited", "")))):
            p = mock.patch.object(c, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.app = c.App.__new__(c.App)
        self.shown = []
        self.app.show = lambda title, text: self.shown.append((title, text))
        self.app.busy = lambda msg: None
        self.app.draw = lambda: None
        self.app.ask = lambda q, d=False: True

    def current(self):
        with open(self.caddyfile, encoding="utf-8") as f:
            return f.read()

    def test_quitar(self):
        seq = iter([1, 2])                                # app.example.com ; form, editor, remove -> 2
        self.app.pick = lambda t, i: next(seq)
        self.app.act_manage()
        self.assertNotIn("app.example.com", self.current())
        self.assertTrue(os.listdir(os.path.join(self.tmp.name, "bk")))

    def test_editar_con_formulario(self):
        seq = iter([1, 0])                                # app.example.com ; "Edit with the form"
        self.app.pick = lambda t, i: next(seq)
        captured = {}

        def fake_form(title, initial, existing, pw_hash=None, editing=False):
            captured.update(initial=initial, existing=existing)
            return dict(initial, upstream="10.0.0.99:9000", gzip=False)
        self.app._service_form = fake_form
        self.app.act_manage()
        new = self.current()
        self.assertIn("reverse_proxy 10.0.0.99:9000", new)
        self.assertNotIn("10.0.0.5", new)
        self.assertIn("# Service edited with caddy-tui", new)
        self.assertNotIn("# Service added", new)
        self.assertIn("{$DOMAIN} {", new)                 # el resto intacto
        self.assertNotIn("app.example.com", captured["existing"])
        self.assertIn("a.example.com", captured["existing"])

    def test_cancelar_no_toca_nada(self):
        seq = iter([1, 3])                                # ... "Cancel" (form, editor, remove, cancel)
        self.app.pick = lambda t, i: next(seq)
        self.app.act_manage()
        self.assertEqual(self.current(), MANAGE_CADDYFILE)

    def test_bloque_a_mano_no_ofrece_formulario(self):
        offered = []
        seq = iter([2, None])
        def pick(title, items):
            offered.append(items)
            return next(seq)
        self.app.pick = pick
        self.app.act_manage()
        self.assertEqual(len(offered[1]), 3)              # editor, remove, cancel


class HistoryHelpers(unittest.TestCase):
    def test_diff_y_estadisticas(self):
        old, new = "a\nb\nc\n", "a\nB\nc\nd\n"
        self.assertEqual(c.diff_stats(old, new), (2, 1))
        lines = c.unified_diff(old, new, "old", "new")
        self.assertEqual(lines[:2], ["--- old", "+++ new"])
        self.assertIn("-b", lines)
        self.assertIn("+B", lines)
        self.assertEqual(c.unified_diff(old, old, "x", "y"), [])

    def test_ago(self):
        for secs, expected in ((5, "just now"), (120, "2 min ago"), (7300, "2 h ago"), (200000, "2 d ago")):
            self.assertEqual(c.ago(1000, now=1000 + secs), expected)

    def test_read_backups_ordena_y_solo_los_del_caddyfile(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(c, "BACKUPS", d):
            for name in ("Caddyfile.20260101-100000", "Caddyfile.20260102-100000", "full-20260101-110000.tar.gz",
                         "docker-compose.https.yml.20260101-100000", "notes.txt"):
                open(os.path.join(d, name), "w").close()
            names = [e["name"] for e in c.read_backups()]
            self.assertEqual(names, ["full-20260101-110000.tar.gz", "Caddyfile.20260102-100000", "Caddyfile.20260101-100000"])
        with mock.patch.object(c, "BACKUPS", "/no/such/dir"):
            self.assertEqual(c.read_backups(), [])

    def test_backup_text_de_los_dos_tipos(self):
        import tarfile
        with tempfile.TemporaryDirectory() as d:
            plain = os.path.join(d, "Caddyfile.1")
            with open(plain, "w") as f:
                f.write("plain")
            full = os.path.join(d, "full-1.tar.gz")
            with tarfile.open(full, "w:gz") as tar:
                for n, data in (("Caddyfile", b"cf"), (".env", b"X=1")):
                    src = os.path.join(d, "src-" + n)
                    with open(src, "wb") as f:
                        f.write(data)
                    tar.add(src, arcname=n)
            self.assertEqual(c.backup_text({"kind": "caddyfile", "path": plain}), ("plain", None))
            self.assertEqual(c.backup_text({"kind": "full", "path": full}), ("cf", "X=1"))


class UndoFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.caddyfile, self.bk = os.path.join(d, "Caddyfile"), os.path.join(d, "bk")
        os.makedirs(self.bk)
        for name, val in (("CADDYFILE", self.caddyfile), ("BASE", d), ("BACKUPS", self.bk),
                          ("ENVFILE", os.path.join(d, ".env"))):
            p = mock.patch.object(c, name, val)
            p.start()
            self.addCleanup(p.stop)
        for name, kw in (("validate_text", dict(return_value=(True, ""))),
                         ("container_state", dict(return_value=("exited", "")))):
            p = mock.patch.object(c, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.write(self.caddyfile, "v3\n")
        self.app = c.App.__new__(c.App)
        self.shown, self.asked = [], []
        self.app.show = lambda t, x: (self.shown.append((t, x)), setattr(self.app, "diff", False))
        self.app.show_diff = lambda t, lines: (self.shown.append((t, "\n".join(lines))), setattr(self.app, "diff", True))
        self.app.busy = lambda m: None
        self.app.draw = lambda: None
        self.answer = True
        self.app.ask = lambda q, d=False: (self.asked.append(q), self.answer)[1]

    @staticmethod
    def write(path, text, mtime=None):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if mtime:
            os.utime(path, (mtime, mtime))

    def current(self):
        with open(self.caddyfile, encoding="utf-8") as f:
            return f.read()

    def test_deshacer_vuelve_a_la_version_anterior_distinta(self):
        self.write(os.path.join(self.bk, "Caddyfile.20260101-100000"), "v1\n", 1000)
        self.write(os.path.join(self.bk, "Caddyfile.20260102-100000"), "v2\n", 2000)
        self.write(os.path.join(self.bk, "Caddyfile.20260103-100000"), "v3\n", 3000)    # igual a la actual: se salta
        self.app.act_undo()
        self.assertEqual(self.current(), "v2\n")
        self.assertIn("-v3", self.shown[0][1])              # el diff se mostró antes de preguntar
        self.assertIn("+v2", self.shown[0][1])

    def test_deshacer_dos_veces_rehace(self):
        self.write(os.path.join(self.bk, "Caddyfile.20260101-100000"), "v2\n", 1000)
        self.app.act_undo()
        self.assertEqual(self.current(), "v2\n")
        self.app.act_undo()                                 # la copia del estado anterior permite rehacer
        self.assertEqual(self.current(), "v3\n")

    def test_si_dices_que_no_no_cambia_nada(self):
        self.write(os.path.join(self.bk, "Caddyfile.20260101-100000"), "v2\n", 1000)
        self.answer = False
        self.app.act_undo()
        self.assertEqual(self.current(), "v3\n")
        self.assertEqual(os.listdir(self.bk), ["Caddyfile.20260101-100000"])

    def test_sin_versiones_distintas(self):
        self.write(os.path.join(self.bk, "Caddyfile.20260101-100000"), "v3\n", 1000)
        self.app.act_undo()
        self.assertIn("no earlier version", self.shown[-1][1])
        self.assertEqual(self.current(), "v3\n")

    def test_version_invalida_no_se_restaura(self):
        self.write(os.path.join(self.bk, "Caddyfile.20260101-100000"), "bad\n", 1000)
        with mock.patch.object(c, "validate_text", return_value=(False, "syntax error")):
            self.app.act_undo()
        self.assertEqual(self.current(), "v3\n")
        self.assertIn("syntax error", self.shown[-1][1])

    def test_restaurar_backup_completo_tambien_restaura_el_env(self):
        import tarfile
        full = os.path.join(self.bk, "full-20260101-100000.tar.gz")
        with tarfile.open(full, "w:gz") as tar:
            for n, data in (("Caddyfile", b"old\n"), (".env", b"DOMAIN=old.example.com\n")):
                src = os.path.join(self.tmp.name, "src-" + n)
                with open(src, "wb") as f:
                    f.write(data)
                tar.add(src, arcname=n)
        self.write(c.ENVFILE, "DOMAIN=new.example.com\n")
        seq = iter([0])
        self.app.pick = lambda t, items: next(seq)
        self.app.act_restore()
        self.assertEqual(self.current(), "old\n")
        self.assertEqual(slurp(c.ENVFILE), "DOMAIN=old.example.com\n")
        self.assertTrue(any(n.startswith("full-") and n != "full-20260101-100000.tar.gz" for n in os.listdir(self.bk)))


class ProbeHelpers(unittest.TestCase):
    def test_site_endpoints(self):
        text = ("{$DOMAIN}:8443 {\n}\nhttp://plain.example.com {\n}\napp.example.com {\n}\nnas.lan:2283 {\n}\n"
                "192.168.1.5 {\n}\n*.wild.example.com {\n}\n:8080 {\n}\napp.example.com {\n}\n")
        with mock.patch.object(c, "runtime_env", return_value={"DOMAIN": "Main.Example.com"}):
            self.assertEqual(c.site_endpoints(text), [
                ("https", "main.example.com", 8443), ("http", "plain.example.com", 80),
                ("https", "app.example.com", 443), ("https", "nas.lan", 2283)])

    def test_clasificacion(self):
        ok = {"status": 200, "ms": 12, "error": None, "cert_error": False}
        ep = ("https", "a.example.com", 443)
        self.assertEqual(c.local_probe_row(ep, ok)[0:2], ("ok", "https://a.example.com"))
        self.assertEqual(c.local_probe_row(("https", "a.example.com", 8443), ok)[1], "https://a.example.com:8443")
        for status, level in ((200, "ok"), (302, "ok"), (401, "ok"), (404, "ok"), (502, "err"), (503, "err")):
            self.assertEqual(c.local_probe_row(ep, dict(ok, status=status))[0], level, status)
        self.assertEqual(c.local_probe_row(ep, dict(ok, status=None, error="timeout"))[0], "err")
        row = c.local_probe_row(ep, dict(ok, status=None, error="certificate has expired", cert_error=True))
        self.assertEqual(row[0], "err")
        self.assertIn("expired", row[2][0])
        internal = c.local_probe_row(("https", "nas.local", 443), dict(ok, status=None, error="self-signed", cert_error=True))
        self.assertEqual(internal[0], "warn")               # internal CA: not trusted by this machine, not an outage

    def test_probe_services_con_sondeos_simulados(self):
        text = "a.example.com {\n}\nb.example.com:8443 {\n}\n"
        results = {"a.example.com": {"status": 200, "ms": 5, "error": None, "cert_error": False},
                   "b.example.com": {"status": 502, "ms": 7, "error": None, "cert_error": False}}
        with mock.patch.object(c, "runtime_env", return_value={}), \
             mock.patch.object(c, "probe_http", side_effect=lambda sch, host, port, to, **k: results[host]):
            rows = c.probe_services(text)
            self.assertEqual([r[0] for r in rows], ["ok", "err"])
            self.assertEqual([r[1] for r in rows], ["https://a.example.com", "https://b.example.com:8443"])
            self.assertEqual([r[1] for r in c.probe_services(text, skip_ports={8443})], ["https://a.example.com"])

    def test_ca_interna_sigue_viendo_si_el_servicio_responde(self):
        cert_err = {"status": None, "ms": 1, "error": "self-signed certificate", "cert_error": True}
        def fake(scheme, host, port, to, **k):
            if k.get("verify", True):
                return cert_err
            return {"status": 502 if host == "down.local" else 200, "ms": 3, "error": None, "cert_error": False}
        with mock.patch.object(c, "runtime_env", return_value={}), mock.patch.object(c, "probe_http", side_effect=fake):
            rows = c.probe_services("up.local {\n}\ndown.local {\n}\n")
        self.assertEqual([r[0] for r in rows], ["warn", "err"])        # up: only the certificate warning; down: an outage
        self.assertIn("not trusted", rows[0][2][-1])
        self.assertIn("HTTP 200", rows[0][2][0])

    def test_ruta_publica_fallida_es_aviso_no_error(self):
        text = "a.example.com {\n}\n"
        good = {"status": 200, "ms": 5, "error": None, "cert_error": False}
        bad = {"status": None, "ms": 6000, "error": "timeout", "cert_error": False}
        calls = iter([good, bad])
        with mock.patch.object(c, "runtime_env", return_value={}), \
             mock.patch.object(c, "resolve_host", return_value=["203.0.113.7"]), \
             mock.patch.object(c, "probe_http", side_effect=lambda *a, **k: next(calls)):
            level, __, details = c.probe_services(text, with_public=True)[0]
        self.assertEqual(level, "warn")
        self.assertTrue(any("hairpin" in d for d in details))

    def test_sin_dns_es_aviso(self):
        good = {"status": 200, "ms": 5, "error": None, "cert_error": False}
        with mock.patch.object(c, "runtime_env", return_value={}), mock.patch.object(c, "resolve_host", return_value=[]), \
             mock.patch.object(c, "probe_http", return_value=good):
            self.assertEqual(c.probe_services("a.example.com {\n}\n", with_public=True)[0][0], "warn")


class ProbeHttpServer(unittest.TestCase):
    """probe_http against real local servers (plain HTTP, and HTTPS with a throw-away certificate)."""

    @staticmethod
    def serve(status, certfile=None, keyfile=None):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        if certfile:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile, keyfile)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def test_http_200_y_502(self):
        for status in (200, 502):
            srv = self.serve(status)
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            r = c.probe_http("http", "site.example.com", srv.server_address[1], "127.0.0.1", timeout=3)
            self.assertEqual((r["status"], r["error"]), (status, None))

    def test_puerto_cerrado(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = c.probe_http("http", "x.example.com", port, "127.0.0.1", timeout=2)
        self.assertIsNone(r["status"])
        self.assertTrue(r["error"])

    @unittest.skipUnless(shutil.which("openssl"), "openssl not available")
    def test_https_verifica_el_certificado(self):
        with tempfile.TemporaryDirectory() as d:
            crt, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost", "-days", "2"],
                           check=True, capture_output=True)
            srv = self.serve(200, crt, key)
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            port = srv.server_address[1]
            ok = c.probe_http("https", "localhost", port, "127.0.0.1", timeout=3, cafile=crt)
            self.assertEqual((ok["status"], ok["cert_error"]), (200, False))
            untrusted = c.probe_http("https", "localhost", port, "127.0.0.1", timeout=3)     # not in the system CAs
            self.assertTrue(untrusted["cert_error"])
            wrong_name = c.probe_http("https", "other.example.com", port, "127.0.0.1", timeout=3, cafile=crt)
            self.assertTrue(wrong_name["cert_error"])


class PlainHttpOnHttps(unittest.TestCase):
    """Plain HTTP served where HTTPS is expected (browsers: "SSL received a record that exceeded the maximum length")."""

    def test_sitio_https_que_contesta_http(self):
        srv = ProbeHttpServer.serve(200)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        with mock.patch.object(c, "runtime_env", return_value={}):
            level, title, details = c.probe_services(f"site.example.com:{port} {{\n}}\n")[0]
        self.assertEqual((level, title), ("err", f"https://site.example.com:{port}"))
        self.assertIn("plain HTTP", details[0])

    def test_error_tls_sin_http_detras_sigue_siendo_error_generico(self):
        tls_err = {"status": None, "ms": 1, "error": "TLS: WRONG_VERSION_NUMBER", "cert_error": False}
        closed = {"status": None, "ms": 1, "error": "Connection refused", "cert_error": False}
        with mock.patch.object(c, "runtime_env", return_value={}), \
             mock.patch.object(c, "probe_http", side_effect=lambda sch, *a, **k: tls_err if sch == "https" else closed):
            level, __, details = c.probe_services("a.example.com {\n}\n")[0]
        self.assertEqual(level, "err")
        self.assertIn("No answer from Caddy", details[0])

    def _inspect(self, ports):
        def fake_run(cmd, **k):
            return (0, json.dumps(ports)) if cmd[:2] == ["docker", "inspect"] else (1, "")
        return fake_run

    def test_puerto_publicado_en_http_fuera_de_caddy(self):
        ports = {"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3000"}]}
        plain = lambda sch, *a, **k: {"status": 200 if sch == "http" else None, "error": None, "cert_error": False}
        with mock.patch.object(c, "run", side_effect=self._inspect(ports)), mock.patch.object(c, "probe_http", side_effect=plain):
            self.assertEqual(c.published_plain_http(BLOCK_BASE), [("fileshare", 3000)])

    def test_no_avisa_si_no_esta_publicado_o_solo_en_loopback_o_hace_tls(self):
        plain = lambda sch, *a, **k: {"status": 200 if sch == "http" else None, "error": None, "cert_error": False}
        both = lambda sch, *a, **k: {"status": 200, "error": None, "cert_error": False}
        with mock.patch.object(c, "probe_http", side_effect=plain):
            for ports in ({"3000/tcp": None}, {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3000"}]}, {}):
                with mock.patch.object(c, "run", side_effect=self._inspect(ports)):
                    self.assertEqual(c.published_plain_http(BLOCK_BASE), [], ports)
        ports = {"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3000"}]}
        with mock.patch.object(c, "run", side_effect=self._inspect(ports)), mock.patch.object(c, "probe_http", side_effect=both):
            self.assertEqual(c.published_plain_http(BLOCK_BASE), [])

    def test_upstream_con_esquema_cruzado(self):
        seen = []
        def fake_run(cmd, **k):
            seen.append(cmd[-1])
            return 0, "  HTTP/1.1 200 OK" if cmd[-1].startswith("https://") else "wget: error"
        with mock.patch.object(c, "run", side_effect=fake_run):
            self.assertTrue(c.upstream_answers("app", "8443", True))
            self.assertFalse(c.upstream_answers("app", "8443", False))
        self.assertEqual(seen, ["https://app:8443", "http://app:8443"])


BLOCK_BASE = """\
{$DOMAIN} {
\treverse_proxy fileshare:3000
}

app.example.com {
\trespond "hi"
}
"""


class Blocklist(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(c.normalize_network("203.0.113.5"), "203.0.113.5")
        self.assertEqual(c.normalize_network(" 198.51.100.77/24 "), "198.51.100.0/24")
        self.assertEqual(c.normalize_network("2001:db8::1"), "2001:db8::1")
        for bad in ("", "abc", "127.0.0.1", "0.0.0.0", "10.0.0.0/7", "0.0.0.0/0", "::1", "224.0.0.1", "300.1.1.1", "2001:db8::/16"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                c.normalize_network(bad)

    def test_bloquear_anade_snippet_e_import_en_cada_sitio(self):
        new = c.set_blocked_ips(BLOCK_BASE, ["203.0.113.5", "198.51.100.0/24"])
        self.assertEqual(c.get_blocked_ips(new), ["203.0.113.5", "198.51.100.0/24"])
        self.assertEqual(new.count("\timport caddy_tui_blocklist"), 2)
        self.assertIn("(caddy_tui_blocklist) {", new)
        self.assertIn("abort @caddy_tui_blocked", new)
        self.assertEqual([b["addr"][0] for b in c.find_site_blocks(new)], ["{$DOMAIN}", "app.example.com"])
        self.assertEqual(c.sites_without_blocklist(new), [])

    def test_es_idempotente(self):
        once = c.set_blocked_ips(BLOCK_BASE, ["203.0.113.5"])
        self.assertEqual(once, c.set_blocked_ips(once, ["203.0.113.5"]))
        self.assertEqual(once, c.sync_blocklist(once))

    def test_desbloquear_el_ultimo_deja_el_fichero_como_estaba(self):
        once = c.set_blocked_ips(BLOCK_BASE, ["203.0.113.5"])
        self.assertEqual(c.set_blocked_ips(once, []), BLOCK_BASE)
        two = c.set_blocked_ips(once, ["203.0.113.5", "198.51.100.9"])
        self.assertEqual(c.get_blocked_ips(c.set_blocked_ips(two, ["198.51.100.9"])), ["198.51.100.9"])

    def test_sin_lista_sync_no_toca_nada(self):
        self.assertEqual(c.sync_blocklist(BLOCK_BASE), BLOCK_BASE)

    def test_el_snippet_se_define_antes_del_primer_sitio(self):
        new = c.set_blocked_ips(BLOCK_BASE, ["203.0.113.5"])
        self.assertLess(new.index("(caddy_tui_blocklist) {"), new.index("{$DOMAIN} {"))
        # with global options and snippets first, it goes right before the first site
        base = "{\n\temail a@b.c\n}\n\n(mine) {\n\tencode gzip\n}\n\n# first site\nfoo.example.com {\n\trespond \"x\"\n}\n"
        new = c.set_blocked_ips(base, ["203.0.113.5"])
        self.assertLess(new.index("(mine)"), new.index("(caddy_tui_blocklist)"))
        self.assertLess(new.index("(caddy_tui_blocklist)"), new.index("# first site"))
        self.assertTrue(new.startswith("{\n\temail"))
        self.assertEqual(c.set_blocked_ips(new, []), base)

    def test_un_sitio_nuevo_queda_protegido_tras_sync(self):
        once = c.set_blocked_ips(BLOCK_BASE, ["203.0.113.5"])
        with_new = once + "\nnew.example.com {\n\trespond \"x\"\n}\n"
        self.assertEqual(c.sites_without_blocklist(with_new), ["new.example.com"])
        self.assertEqual(c.sites_without_blocklist(c.sync_blocklist(with_new)), [])

    def test_bloque_con_import_sigue_siendo_simple(self):
        v = dict(host="x.example.com", upstream="10.0.0.9:80", https_upstream=False, skip_verify=False,
                 gzip=True, headers=False, log=True, user="", password="")
        block = c.build_site_block(v).rstrip("\n").split("\n")
        block.insert(1, "\timport caddy_tui_blocklist")
        self.assertIsNotNone(c.parse_simple_block(block))

    def test_comentarios_y_snippets_ajenos_se_respetan(self):
        base = "(mine) {\n\tencode gzip\n}\n\n# hello\nsite.example.com {\n\timport mine\n}\n"
        blocked = c.set_blocked_ips(base, ["203.0.113.5"])
        self.assertIn("(mine) {", blocked)
        self.assertIn("# hello", blocked)
        self.assertEqual(c.set_blocked_ips(blocked, []), base)


class BlocklistFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.caddyfile = os.path.join(d, "Caddyfile")
        with open(self.caddyfile, "w", encoding="utf-8") as f:
            f.write(BLOCK_BASE)
        for name, val in (("CADDYFILE", self.caddyfile), ("BASE", d), ("BACKUPS", os.path.join(d, "bk"))):
            p = mock.patch.object(c, name, val)
            p.start()
            self.addCleanup(p.stop)
        for name, kw in (("validate_text", dict(return_value=(True, ""))), ("container_state", dict(return_value=("exited", "")))):
            p = mock.patch.object(c, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.app = c.App.__new__(c.App)
        self.shown, self.asked = [], []
        self.app.show = lambda t, x: self.shown.append((t, x))
        self.app.busy = lambda m: None
        self.app.draw = lambda: None
        self.app.ask = lambda q, d=False: (self.asked.append(q), True)[1]

    def current(self):
        with open(self.caddyfile, encoding="utf-8") as f:
            return f.read()

    def test_bloquear_ip(self):
        self.app.block_ip("203.0.113.5")
        self.assertEqual(c.get_blocked_ips(self.current()), ["203.0.113.5"])
        self.assertTrue(os.listdir(os.path.join(self.tmp.name, "bk")))

    def test_ip_privada_pide_confirmacion_extra(self):
        self.app.block_ip("192.168.1.50")
        self.assertTrue(any("private" in q for q in self.asked))

    def test_no_bloquea_loopback_ni_rangos_enormes(self):
        for bad in ("127.0.0.1", "10.0.0.0/4"):
            self.app.block_ip(bad)
            self.assertEqual(c.get_blocked_ips(self.current()), [])
        self.assertIn("not a valid", self.shown[-1][1].lower())

    def test_ya_bloqueada(self):
        self.app.block_ip("203.0.113.5")
        self.app.block_ip("203.0.113.5")
        self.assertIn("already blocked", self.shown[-1][1])
        self.assertEqual(c.get_blocked_ips(self.current()), ["203.0.113.5"])

    def test_desbloquear_desde_el_menu(self):
        self.app.block_ip("203.0.113.5")
        self.app.block_ip("198.51.100.9")
        seq = iter([2])                                   # options: block, review, unblock 203..., unblock 198..., cancel
        self.app.pick = lambda t, items: next(seq)
        self.app.act_blocklist()
        self.assertEqual(c.get_blocked_ips(self.current()), ["198.51.100.9"])

    def test_el_asistente_de_alta_mantiene_la_proteccion(self):
        self.app.block_ip("203.0.113.5")
        new = c.sync_blocklist(self.current().rstrip("\n") + "\n\nnew.example.com {\n\trespond \"x\"\n}\n")
        self.assertEqual(c.sites_without_blocklist(new), [])


def http_error(code, headers=None, url="https://x/"):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return c.urllib.error.HTTPError(url, code, "err", msg, io.BytesIO(b""))


class FakeResponse:
    def __init__(self, headers=None, body=b""):
        self.headers, self._body = headers or {}, body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._body


class ImageRegistry(unittest.TestCase):
    def test_parse_image(self):
        P = c.parse_image
        self.assertEqual(P("caddy:2-alpine"), ("registry-1.docker.io", "library/caddy", "2-alpine"))
        self.assertEqual(P("caddy"), ("registry-1.docker.io", "library/caddy", "latest"))
        self.assertEqual(P("user/app:1.0"), ("registry-1.docker.io", "user/app", "1.0"))
        self.assertEqual(P("docker.io/library/caddy:2"), ("registry-1.docker.io", "library/caddy", "2"))
        self.assertEqual(P("ghcr.io/org/app:release"), ("ghcr.io", "org/app", "release"))
        self.assertEqual(P("localhost:5000/app"), ("localhost:5000", "app", "latest"))
        self.assertEqual(P("reg.example.com:5000/a/b:tag"), ("reg.example.com:5000", "a/b", "tag"))
        self.assertIsNone(P("caddy@sha256:" + "a" * 64))

    def test_digest_con_token_anonimo(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            auth = None if isinstance(req, str) else req.get_header("Authorization")
            calls.append((url, auth))
            if url.startswith("https://auth.example/token"):
                return FakeResponse(body=b'{"token": "TOK"}')
            if auth == "Bearer TOK":
                return FakeResponse(headers={"Docker-Content-Digest": "sha256:abc"})
            raise http_error(401, {"WWW-Authenticate": 'Bearer realm="https://auth.example/token",service="reg",scope="repository:library/caddy:pull"'})
        with mock.patch.object(c.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(c.registry_digest("caddy:2-alpine"), "sha256:abc")
        self.assertIn("service=reg", calls[1][0])
        self.assertIn("https://registry-1.docker.io/v2/library/caddy/manifests/2-alpine", calls[0][0])

    def test_errores_del_registro(self):
        for exc, expected in ((http_error(404), "HTTP 404"), (c.urllib.error.URLError("no route"), "no route")):
            with mock.patch.object(c.urllib.request, "urlopen", side_effect=exc):
                with self.assertRaisesRegex(RuntimeError, expected):
                    c.registry_digest("caddy:2")
        with mock.patch.object(c.urllib.request, "urlopen", return_value=FakeResponse()):
            with self.assertRaises(RuntimeError):
                c.registry_digest("caddy:2")
        with self.assertRaises(RuntimeError):
            c.registry_digest("caddy@sha256:" + "a" * 64)

    def test_cache(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CACHE_HOME": d}):
            with mock.patch.object(c, "registry_digest", return_value="sha256:one") as m:
                self.assertEqual(c.cached_registry_digest("caddy:2", now=1000), "sha256:one")
                self.assertEqual(c.cached_registry_digest("caddy:2", now=1000 + 3600), "sha256:one")
                self.assertEqual(m.call_count, 1)                      # served from the cache
                c.cached_registry_digest("caddy:2", now=1000 + 7 * 3600)
                self.assertEqual(m.call_count, 2)                      # expired
                c.cached_registry_digest("caddy:3", now=1000 + 7 * 3600)
                self.assertEqual(m.call_count, 3)                      # another image


class ImageStatus(unittest.TestCase):
    def status(self, tag_id="sha256:aaa", repo_digests='["caddy@sha256:d1"]', digest="sha256:d1", error=None):
        def fake_run(cmd, **k):
            joined = " ".join(cmd)
            if "caddy version" in joined:
                return 0, "v2.8.0 h1:xyz"
            if "{{.Image}}" in joined:
                return 0, "sha256:aaa"
            if "{{.Id}}" in joined:
                return 0, tag_id
            if "RepoDigests" in joined:
                return 0, repo_digests
            return 1, ""

        def digest_fn(ref):
            if error:
                raise RuntimeError(error)
            return digest
        with mock.patch.object(c, "run", side_effect=fake_run), mock.patch.object(c, "CONTAINER", "cx"), \
             mock.patch.object(c, "IMAGE", "caddy:2-alpine"):
            return c.check_image_update(digest_fn)

    def test_estados(self):
        self.assertEqual(self.status()["status"], "current")
        self.assertEqual(self.status(digest="sha256:new")["status"], "update")
        self.assertEqual(self.status(tag_id="sha256:bbb")["status"], "pulled")        # tag moved locally, container is behind
        self.assertEqual(self.status(repo_digests="[]")["status"], "local")
        info = self.status(error="HTTP 429")
        self.assertEqual((info["status"], info["error"]), ("unknown", "HTTP 429"))
        self.assertEqual(self.status()["version"], "v2.8.0")


class ImageUpdateFlow(unittest.TestCase):
    def setUp(self):
        self.cmds, self.shown = [], []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cf = os.path.join(self.tmp.name, "Caddyfile")
        with open(cf, "w") as f:
            f.write(":80 {\n}\n")
        for name, val in (("CADDYFILE", cf), ("COMPOSE", "/x/docker-compose.yml"), ("SERVICE", "caddy"), ("CONTAINER", "cx"), ("IMAGE", "caddy:2-alpine")):
            p = mock.patch.object(c, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.versions = iter(["v2.9.0"])
        for name, kw in (("run", dict(side_effect=lambda cmd, **k: (self.cmds.append(cmd), (0, ""))[1])),
                         ("validate_text", dict(return_value=(True, ""))),
                         ("caddy_version", dict(side_effect=lambda: next(self.versions))),
                         ("time", dict())):
            p = mock.patch.object(c, name, **kw) if name != "time" else mock.patch.object(c.time, "sleep")
            p.start()
            self.addCleanup(p.stop)
        self.app = c.App.__new__(c.App)
        self.app.show = lambda t, x: self.shown.append((t, x))
        self.app.busy = lambda m: None
        self.app.refresh_state = lambda force=False: None
        self.info = {"status": "update", "ref": "caddy:2-alpine", "version": "v2.8.0", "error": None, "image_id": "sha256:" + "a" * 64}

    def test_actualiza_valida_y_recrea(self):
        self.app.update_image(self.info)
        self.assertEqual(self.cmds[0][-2:], ["pull", "caddy"])
        self.assertIn("--force-recreate", self.cmds[1])
        self.assertIn("v2.8.0 → v2.9.0", self.shown[-1][1])
        self.assertIn("docker tag aaaaaaaaaaaa caddy:2-alpine", self.shown[-1][1])

    def test_si_la_nueva_version_rechaza_el_caddyfile_no_se_recrea(self):
        with mock.patch.object(c, "validate_text", return_value=(False, "unknown directive")):
            self.app.update_image(self.info)
        self.assertEqual(len(self.cmds), 1)                    # pulled, but never recreated
        self.assertIn("unknown directive", self.shown[-1][1])
        self.assertIn("untouched", self.shown[-1][0])

    def test_fallo_al_descargar(self):
        with mock.patch.object(c, "run", return_value=(1, "pull access denied")):
            self.app.update_image(self.info)
        self.assertIn("pull access denied", self.shown[-1][1])

    def test_ya_descargada_solo_recrea(self):
        self.app.update_image(self.info, pull=False)
        self.assertNotIn("pull", [p for cmd in self.cmds for p in cmd])
        self.assertIn("--force-recreate", self.cmds[0])

    def test_sin_compose_descarga_y_avisa(self):
        with mock.patch.object(c, "COMPOSE", ""):
            self.app.update_image(self.info)
        self.assertEqual(self.cmds, [["docker", "pull", "caddy:2-alpine"]])
        self.assertIn("recreate it yourself", self.shown[-1][1])


class InstallPieces(unittest.TestCase):
    def test_compose_generado(self):
        text = c.render_install_compose(8080, 8443, "mycaddy")
        for frag in ("image: caddy:2-alpine", "container_name: mycaddy", '"8080:80"', '"8443:443"', '"8443:443/udp"',
                     "./Caddyfile:/etc/caddy/Caddyfile:ro", "caddy_data:/data", "host.docker.internal:host-gateway"):
            self.assertIn(frag, text)

    def test_caddyfile_generado(self):
        full = c.render_install_caddyfile("app.example.com", "192.168.1.5:8080", "me@example.com")
        self.assertTrue(full.startswith("{\n\temail me@example.com\n}"))
        self.assertIn("app.example.com {", full)
        self.assertIn("reverse_proxy 192.168.1.5:8080", full)
        self.assertEqual([b["addr"] for b in c.find_site_blocks(full)], [["app.example.com"]])
        placeholder = c.render_install_caddyfile()
        self.assertIn(":80 {", placeholder)
        self.assertNotIn("reverse_proxy", placeholder)

    def test_docker_run_equivalente(self):
        cmd = c.docker_run_command("/opt/caddy", 80, 443, "caddy")
        self.assertEqual(cmd[:3], ["docker", "run", "-d"])
        self.assertIn("80:80", cmd)
        self.assertIn("/opt/caddy/Caddyfile:/etc/caddy/Caddyfile:ro", cmd)
        self.assertEqual(cmd[-1], "caddy:2-alpine")

    def test_puerto_en_uso(self):
        s = socket.socket()
        s.bind(("0.0.0.0", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            self.assertTrue(c.port_in_use(port))
        finally:
            s.close()
        self.assertFalse(c.port_in_use(port))

    def test_paquetes_de_docker_segun_la_distro(self):
        plan = lambda ids, cands, apt=True: c.docker_install_plan(ids, lambda p: cands.get(p, ""), apt)
        self.assertEqual(plan(["debian"], {"docker-compose": "1.29.2-3"}), ["docker.io"])                       # bookworm: only v1
        self.assertEqual(plan(["ubuntu", "debian"], {"docker-compose-v2": "2.40.3-1", "docker-compose": "1.29.2-6"}),
                         ["docker.io", "docker-compose-v2"])
        self.assertEqual(plan(["debian"], {"docker-compose": "2.26.1-4"}), ["docker.io", "docker-compose"])    # trixie
        self.assertEqual(plan(["debian"], {"docker-compose-plugin": "2.29.0-1"}), ["docker.io", "docker-compose-plugin"])
        self.assertIsNone(plan(["fedora"], {}))
        self.assertIsNone(plan(["debian"], {}, apt=False))

    def test_configure_sin_contenedores_lanza_NoCaddyContainer(self):
        args = argparse.Namespace(container=None)
        with mock.patch.object(c, "list_caddy_containers", return_value=[]):
            with self.assertRaises(c.NoCaddyContainer) as cm:
                c.configure(args)
        self.assertIsInstance(cm.exception, SystemExit)             # uncaught it behaves like sys.exit(message)
        self.assertIn("No Caddy container", cm.exception.code)


class EnsureDocker(unittest.TestCase):
    def run_ensure(self, which, geteuid=0, plan=("docker.io",), answers=(True,), results=None):
        calls, said = [], io.StringIO()
        results = list(results or [])

        def fake_run(cmd, **k):
            calls.append(cmd)
            return results.pop(0) if results else (0, "")
        ans = iter(answers)
        with mock.patch.object(c.shutil, "which", return_value=which), mock.patch.object(os, "geteuid", return_value=geteuid), \
             mock.patch.object(c, "docker_install_plan", return_value=list(plan) if plan else None), \
             mock.patch.object(c, "ask_yes", side_effect=lambda *a, **k: next(ans)), \
             mock.patch.object(c, "run", side_effect=fake_run), mock.patch.object(c, "_systemd_available", return_value=True), \
             mock.patch.object(c, "compose_available", return_value=True), mock.patch.dict(os.environ, {}, clear=True), \
             contextlib.redirect_stdout(said):
            ok = c.ensure_docker()
        return ok, calls, said.getvalue()

    def test_ya_instalado(self):
        self.assertEqual(self.run_ensure("/usr/bin/docker")[:2], (True, []))

    def test_distro_no_soportada(self):
        ok, calls, out = self.run_ensure(None, plan=None)
        self.assertFalse(ok)
        self.assertIn("docs.docker.com", out)

    def test_sin_root_no_instala(self):
        ok, calls, out = self.run_ensure(None, geteuid=1000)
        self.assertFalse(ok)
        self.assertEqual(calls, [])
        self.assertIn("sudo caddy-tui --install", out)

    def test_el_usuario_dice_que_no(self):
        ok, calls, __ = self.run_ensure(None, answers=(False,))
        self.assertEqual((ok, calls), (False, []))

    def test_instala_y_activa_el_servicio(self):
        ok, calls, out = self.run_ensure(None, plan=("docker.io", "docker-compose-v2"))
        self.assertTrue(ok)
        self.assertEqual(calls[0], ["apt-get", "update"])
        self.assertEqual(calls[1], ["apt-get", "install", "-y", "docker.io", "docker-compose-v2"])
        self.assertIn(["systemctl", "enable", "--now", "docker"], calls)
        self.assertIn("Docker installed", out)

    def test_fallo_de_apt(self):
        ok, calls, out = self.run_ensure(None, results=[(0, ""), (100, "E: Unable to locate package")])
        self.assertFalse(ok)
        self.assertIn("Unable to locate", out)


class InstallFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "caddy")
        self.calls = []
        self.out = io.StringIO()

    def go(self, lines=("", "", "", ""), yes=(True,), compose=True, busy=(), validate=(True, ""), args_dir=None, existing=()):
        line_it, yes_it = iter(lines), iter(yes)
        patches = [
            mock.patch.object(c, "native_caddy_running", return_value=False),
            mock.patch.object(c, "list_caddy_containers", return_value=list(existing)),
            mock.patch.object(c, "resolve_host", return_value=["203.0.113.9"]),
            mock.patch.object(c, "port_in_use", side_effect=lambda p: p in busy),
            mock.patch.object(c, "port_owner", return_value="nginx"),
            mock.patch.object(c, "compose_available", return_value=compose),
            mock.patch.object(c, "validate_standalone", return_value=validate),
            mock.patch.object(c, "inspect_container", return_value=None),
            mock.patch.object(c, "container_state_of", return_value=("running", "")),
            mock.patch.object(c, "ask_line", side_effect=lambda *a, **k: next(line_it)),
            mock.patch.object(c, "ask_yes", side_effect=lambda *a, **k: next(yes_it, True)),
            mock.patch.object(c, "run", side_effect=lambda cmd, **k: (self.calls.append(cmd), (0, ""))[1]),
            mock.patch.object(c.time, "sleep"),
            mock.patch.object(c, "BASE", self.tmp.name),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        args = argparse.Namespace(dir=args_dir)
        with contextlib.redirect_stdout(self.out):
            return c.install_flow(args)

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def test_flujo_completo_con_compose(self):
        # folder, domain, upstream, email
        name = self.go(lines=(self.dir, "app.example.com", "192.168.1.5:8080", "me@example.com"))
        self.assertEqual(name, "caddy")
        self.assertIn("reverse_proxy 192.168.1.5:8080", self.read("Caddyfile"))
        self.assertIn("email me@example.com", self.read("Caddyfile"))
        self.assertIn('"80:80"', self.read("docker-compose.yml"))
        self.assertEqual(self.calls[-1][:4], ["docker", "compose", "-f", os.path.join(self.dir, "docker-compose.yml")])
        self.assertEqual(self.calls[-1][-2:], ["up", "-d"])

    def test_sin_dominio_crea_un_sitio_provisional(self):
        self.assertEqual(self.go(lines=(self.dir, "")), "caddy")
        self.assertIn(":80 {", self.read("Caddyfile"))

    def test_sin_compose_usa_docker_run(self):
        self.assertEqual(self.go(lines=(self.dir, ""), compose=False), "caddy")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "docker-compose.yml")))
        self.assertEqual(self.calls[-1][:3], ["docker", "run", "-d"])

    def test_puertos_ocupados_ofrece_alternativos(self):
        self.go(lines=(self.dir, ""), busy=(80,))
        self.assertIn('"8080:80"', self.read("docker-compose.yml"))
        self.assertIn("nginx", self.out.getvalue())

    def test_puertos_ocupados_y_el_usuario_rechaza(self):
        self.assertIsNone(self.go(lines=(self.dir, ""), busy=(80, 443), yes=(False,)))
        self.assertFalse(os.path.exists(self.dir))

    def test_no_pisa_una_carpeta_con_configuracion(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "Caddyfile"), "w") as f:
            f.write("keep")
        self.assertIsNone(self.go(lines=(self.dir,)))
        self.assertEqual(self.read("Caddyfile"), "keep")

    def test_configuracion_invalida_no_escribe_nada(self):
        self.assertIsNone(self.go(lines=(self.dir, ""), validate=(False, "boom")))
        self.assertFalse(os.path.exists(self.dir))
        self.assertIn("boom", self.out.getvalue())

    def test_dominio_invalido_o_sin_destino(self):
        self.assertIsNone(self.go(lines=(self.dir, "bad domain!")))
        self.assertIsNone(self.go(lines=(self.dir + "2", "app.example.com", "")))

    def test_confirmacion_negativa(self):
        self.assertIsNone(self.go(lines=(self.dir, ""), yes=(False,)))
        self.assertFalse(os.path.exists(self.dir))

    def test_ya_hay_un_caddy_pregunta_antes(self):
        self.assertIsNone(self.go(lines=(self.dir, ""), existing=[("old", "caddy:2", "running")], yes=(False,)))


def ev(ip, status=200, uri="/", ts=1000.0, host="a.example.com"):
    return (ts, host, "GET", uri, ip, status, 10, 0.01)


class Scanners(unittest.TestCase):
    def test_parse_span(self):
        for text, secs in (("30m", 1800), ("24h", 86400), ("7d", 604800), ("90", 5400), ("0", 0)):
            self.assertEqual(c.parse_span(text), secs)
        for bad in ("", "x", "1w", "-5", "1.5h"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                c.parse_span(bad)

    def test_ips_publicas(self):
        for ip in ("45.83.64.1", "8.8.8.8", "2001:4860:4860::8888"):
            self.assertTrue(c.is_public_ip(ip), ip)
        for ip in ("192.168.1.5", "10.0.0.1", "127.0.0.1", "172.18.0.1", "169.254.1.1", "::1", "fd00::1", "203.0.113.5", "-", "abc"):
            self.assertFalse(c.is_public_ip(ip), ip)

    def test_rutas_de_escáner_en_tres_distintas(self):
        events = [ev("45.83.64.7", 404, u) for u in ("/.env", "/wp-login.php", "/phpmyadmin/index.php")]
        found = c.find_scanners(events)
        self.assertEqual([o["ip"] for o in found], ["45.83.64.7"])
        self.assertEqual(found[0]["reasons"][0], ("probes", 3))
        self.assertEqual(c.find_scanners(events[:2]), [])                     # two probes are not enough

    def test_muchos_errores(self):
        events = [ev("45.83.64.8", 404, f"/p{i}") for i in range(35)] + [ev("45.83.64.8", 200)]
        found = c.find_scanners(events)
        self.assertEqual(found[0]["reasons"][0][0], "errors")
        self.assertGreaterEqual(found[0]["reasons"][0][2], 90)

    def test_un_usuario_normal_con_algun_404_no_cuenta(self):
        events = [ev("45.83.64.9", 200, f"/page{i}") for i in range(40)] + [ev("45.83.64.9", 404, "/old")] * 3
        self.assertEqual(c.find_scanners(events), [])

    def test_fuerza_bruta(self):
        events = [ev("45.83.64.10", 401, "/login") for __ in range(12)]
        self.assertEqual(c.find_scanners(events)[0]["reasons"], [("auth", 12)])
        events += [ev("45.83.64.10", 200, "/ok")] * 5                       # it got in: a real user who forgot the password
        self.assertEqual(c.find_scanners(events), [])

    def test_nunca_privadas_ni_permitidas_ni_ya_bloqueadas(self):
        probes = ["/.env", "/wp-login.php", "/phpmyadmin/"]
        for ip in ("192.168.1.50", "127.0.0.1"):
            self.assertEqual(c.find_scanners([ev(ip, 404, u) for u in probes]), [], ip)
        events = [ev("45.83.64.7", 404, u) for u in probes]
        self.assertEqual(c.find_scanners(events, allow=["45.83.64.0/24"]), [])
        self.assertEqual(c.find_scanners(events, blocked={"45.83.64.7"}), [])

    def test_ordena_por_actividad(self):
        events = [ev("45.83.64.1", 404, u) for u in ("/.env", "/.git/config", "/wp-admin/x")]
        events += [ev("45.83.64.2", 404, u) for u in ("/.env", "/.git/config", "/wp-admin/x")] * 3
        self.assertEqual([o["ip"] for o in c.find_scanners(events)], ["45.83.64.2", "45.83.64.1"])

    def test_texto_de_motivos(self):
        text = c.reason_text([("probes", 4), ("errors", 40, 95), ("auth", 11)])
        self.assertIn("4 scanner paths", text)
        self.assertIn("95% of 40 requests", text)
        self.assertIn("11 failed logins", text)

    def test_parse_access_line(self):
        line = json.dumps({"logger": "http.log.access.log0", "ts": 5.5, "status": 404, "size": 3, "duration": 0.1,
                           "request": {"host": "h", "method": "GET", "uri": "/x", "client_ip": "1.2.3.4"}})
        self.assertEqual(c.parse_access_line(line), (5.5, "h", "GET", "/x", "1.2.3.4", 404, 3, 0.1))
        self.assertIsNone(c.parse_access_line('{"logger": "tls", "msg": "x"}'))
        self.assertIsNone(c.parse_access_line("not json"))


class Bans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"CADDY_TUI_STATE_DIR": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def test_registro_y_caducidad(self):
        c.register_ban("1.1.1.1", "scanner", 3600, now=1000)
        c.register_ban("2.2.2.2", "manual", 0, now=1000)                      # permanent
        bans = c.load_bans()
        self.assertEqual(bans["1.1.1.1"]["until"], 4600)
        self.assertIsNone(bans["2.2.2.2"]["until"])
        blocked = ["1.1.1.1", "2.2.2.2"]
        self.assertEqual(c.expired_bans(bans, blocked, now=2000), [])
        self.assertEqual(c.expired_bans(bans, blocked, now=5000), ["1.1.1.1"])
        self.assertEqual(c.expired_bans(bans, ["2.2.2.2"], now=5000), [])     # already unblocked by hand
        c.forget_ban("1.1.1.1")
        self.assertNotIn("1.1.1.1", c.load_bans())

    def test_sin_fichero(self):
        self.assertEqual(c.load_bans(), {})

    def test_tiempo_restante(self):
        self.assertEqual(c.time_left(1000 + 7200, now=1000), "in 2 h")
        self.assertEqual(c.time_left(1000 + 90, now=1000), "in 1 min")
        self.assertEqual(c.time_left(1000 + 3 * 86400, now=1000), "in 3 d")


class AutoBlockRun(unittest.TestCase):
    PROBES = ["/.env", "/wp-login.php", "/phpmyadmin/"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.caddyfile = os.path.join(d, "Caddyfile")
        with open(self.caddyfile, "w", encoding="utf-8") as f:
            f.write(BLOCK_BASE)
        envs = {"CADDY_TUI_STATE_DIR": d}
        for p in (mock.patch.dict(os.environ, envs, clear=True), mock.patch.object(c, "CADDYFILE", self.caddyfile),
                  mock.patch.object(c, "BASE", d), mock.patch.object(c, "BACKUPS", os.path.join(d, "bk")),
                  mock.patch.object(c, "validate_text", return_value=(True, "")),
                  mock.patch.object(c, "container_state", return_value=("exited", ""))):
            p.start()
            self.addCleanup(p.stop)
        self.args = argparse.Namespace(window=None, ban_time=None, dry_run=False, quiet=False)

    def current(self):
        with open(self.caddyfile, encoding="utf-8") as f:
            return f.read()

    def run_it(self, events, **kw):
        for k, v in kw.items():
            setattr(self.args, k, v)
        out = io.StringIO()
        with mock.patch.object(c, "read_access_events", return_value=events), contextlib.redirect_stdout(out):
            code = c.run_auto_block(self.args)
        return code, out.getvalue()

    def scanner(self, ip="45.83.64.7"):
        return [ev(ip, 404, u) for u in self.PROBES]

    def test_banea_registra_y_guarda(self):
        code, out = self.run_it(self.scanner())
        self.assertEqual(code, 0)
        self.assertEqual(c.get_blocked_ips(self.current()), ["45.83.64.7"])
        ban = c.load_bans()["45.83.64.7"]
        self.assertIn("scanner paths", ban["reason"])
        self.assertAlmostEqual(ban["until"] - ban["ts"], 86400, delta=1)
        self.assertIn("Banned 45.83.64.7", out)

    def test_dry_run_no_cambia_nada(self):
        code, out = self.run_it(self.scanner(), dry_run=True)
        self.assertEqual((code, c.get_blocked_ips(self.current()), c.load_bans()), (0, [], {}))
        self.assertIn("dry run", out)

    def test_segunda_pasada_no_repite(self):
        self.run_it(self.scanner())
        code, out = self.run_it(self.scanner())
        self.assertEqual(c.get_blocked_ips(self.current()), ["45.83.64.7"])
        self.assertIn("Nothing to do", out)

    def test_levanta_los_baneos_caducados(self):
        self.run_it(self.scanner())
        bans = c.load_bans()
        bans["45.83.64.7"]["until"] = time.time() - 5
        c.save_bans(bans)
        code, out = self.run_it([])
        self.assertEqual(c.get_blocked_ips(self.current()), [])
        self.assertNotIn("45.83.64.7", c.load_bans())
        self.assertIn("Ban lifted: 45.83.64.7", out)
        self.assertEqual(self.current(), BLOCK_BASE)                           # the file is back to how it was

    def test_baneo_permanente(self):
        self.run_it(self.scanner(), ban_time="0")
        self.assertIsNone(c.load_bans()["45.83.64.7"]["until"])

    def test_no_toca_privadas_ni_permitidas(self):
        os.environ["AUTOBLOCK_ALLOW"] = "45.83.64.0/24"
        self.run_it(self.scanner() + self.scanner("192.168.1.9"))
        self.assertEqual(c.get_blocked_ips(self.current()), [])

    def test_limite_por_pasada(self):
        events = []
        for n in range(30):
            events += self.scanner(f"45.83.64.{n + 1}")
        self.run_it(events)
        self.assertEqual(len(c.get_blocked_ips(self.current())), 20)

    def test_rango_de_tiempo_invalido(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_it([], ban_time="soon")[0], 2)

    def test_si_falla_la_validacion_no_registra_nada(self):
        with mock.patch.object(c, "validate_text", return_value=(False, "bad")), contextlib.redirect_stderr(io.StringIO()):
            code, __ = self.run_it(self.scanner())
        self.assertEqual((code, c.load_bans(), self.current()), (2, {}, BLOCK_BASE))

    def test_avisa_por_telegram(self):
        sent = []
        os.environ.update(TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="42")
        with mock.patch.object(c, "telegram_send", side_effect=lambda t, ch, txt: sent.append(txt)):
            self.run_it(self.scanner())
        self.assertEqual(len(sent), 1)
        self.assertIn("45.83.64.7", sent[0])


class ReviewFlow(BlocklistFlow):
    """Interactive ban of a suspicious address (re-uses the BlocklistFlow harness)."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"CADDY_TUI_STATE_DIR": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.events = [ev("45.83.64.7", 404, u) for u in ("/.env", "/wp-login.php", "/phpmyadmin/")]

    def test_banea_24h_la_elegida(self):
        seq = iter([0, 0])                                  # first suspicious address ; "For 24 hours"
        self.app.pick = lambda t, items: next(seq)
        self.app.review_suspicious(self.events)
        self.assertEqual(c.get_blocked_ips(self.current()), ["45.83.64.7"])
        ban = c.load_bans()["45.83.64.7"]
        self.assertAlmostEqual(ban["until"] - ban["ts"], 86400, delta=1)
        self.assertIn("scanner paths", ban["reason"])

    def test_permanente(self):
        seq = iter([0, 2])
        self.app.pick = lambda t, items: next(seq)
        self.app.review_suspicious(self.events)
        self.assertIsNone(c.load_bans()["45.83.64.7"]["until"])

    def test_sin_sospechosas(self):
        self.app.pick = lambda t, items: self.fail("nothing to pick")
        self.app.review_suspicious([ev("45.83.64.7", 200)])
        self.assertIn("No suspicious", self.shown[-1][1])

    def test_desbloquear_olvida_el_baneo(self):
        seq = iter([0, 0])
        self.app.pick = lambda t, items: next(seq)
        self.app.review_suspicious(self.events)
        seq2 = iter([2])                                    # block, review, unblock 45.83.64.7
        self.app.pick = lambda t, items: next(seq2)
        self.app.act_blocklist()
        self.assertEqual((c.get_blocked_ips(self.current()), c.load_bans()), ([], {}))

    def test_los_baneos_caducados_se_levantan_al_abrir_la_pantalla(self):
        seq = iter([0, 0])
        self.app.pick = lambda t, items: next(seq)
        self.app.review_suspicious(self.events)
        bans = c.load_bans()
        bans["45.83.64.7"]["until"] = time.time() - 1
        c.save_bans(bans)
        self.app.pick = lambda t, items: None               # just open and close the screen
        self.app.act_blocklist()
        self.assertEqual((c.get_blocked_ips(self.current()), c.load_bans()), ([], {}))


class ReviewAndSetup(unittest.TestCase):
    def test_unidades_con_y_sin_autobloqueo(self):
        self.assertIn("ExecStartPre=-/usr/bin/caddy-tui --auto-block", c.render_service("/usr/bin/caddy-tui", True))
        self.assertNotIn("ExecStartPre", c.render_service("/usr/bin/caddy-tui"))
        cron = c.render_cron("/usr/bin/caddy-tui", 15, True)
        self.assertIn("/usr/bin/caddy-tui --auto-block >/dev/null 2>&1; /usr/bin/caddy-tui --check", cron)
        self.assertNotIn("--auto-block", c.render_cron("/usr/bin/caddy-tui", 15))


class SiteTemplates(unittest.TestCase):
    REDIRECT = dict(host="www.example.com", target="https://example.com", keep_path=True, permanent=True, log=True)
    MAINT = dict(host="app.example.com", message='Back at "noon" \\ soon', status=503, log=True)
    ROUTES = dict(host="app.example.com", upstream="10.0.0.6:80", gzip=True, log=True,
                  rules=[("/api", "10.0.0.5:3000", True), ("/static", "10.0.0.7:8080", False)])

    @staticmethod
    def lines(block):
        return block.rstrip("\n").split("\n")

    def test_redirect(self):
        block = c.build_redirect_block(self.REDIRECT)
        self.assertIn("redir https://example.com{uri} permanent", block)
        self.assertEqual(c.parse_redirect_block(self.lines(block)), self.REDIRECT)
        temp = dict(self.REDIRECT, keep_path=False, permanent=False, log=False)
        block = c.build_redirect_block(temp)
        self.assertIn("redir https://example.com temporary", block)
        self.assertNotIn("log", block.split("\n", 2)[2])
        self.assertEqual(c.parse_redirect_block(self.lines(block)), temp)

    def test_mantenimiento_con_comillas_y_barras(self):
        block = c.build_maintenance_block(self.MAINT)
        self.assertIn('respond "Back at \\"noon\\" \\\\ soon" 503', block)
        self.assertEqual(c.parse_maintenance_block(self.lines(block)), self.MAINT)

    def test_rutas(self):
        block = c.build_routes_block(self.ROUTES)
        self.assertIn("handle_path /api/* {", block)
        self.assertIn("\thandle /static/* {", block)
        self.assertIn("\thandle {\n\t\treverse_proxy 10.0.0.6:80", block)
        self.assertEqual(c.parse_routes_block(self.lines(block)), self.ROUTES)

    def test_route_pattern(self):
        for raw in ("/api", "/api/", "/api/*", " /api "):
            self.assertEqual(c.route_pattern(raw), "/api/*")
        self.assertEqual(c.route_pattern("/"), "/*")

    def test_parse_any_block_distingue_los_tipos(self):
        cases = {"proxy": c.build_site_block(dict(host="x.example.com", upstream="10.0.0.1:80", https_upstream=False,
                                                  skip_verify=False, gzip=True, headers=False, log=True, user="", password="")),
                 "redirect": c.build_redirect_block(self.REDIRECT), "maintenance": c.build_maintenance_block(self.MAINT),
                 "routes": c.build_routes_block(self.ROUTES)}
        for kind, block in cases.items():
            self.assertEqual(c.parse_any_block(self.lines(block))[0], kind, kind)
            self.assertEqual(c.parse_any_block(self.lines(block))[0], kind)
        self.assertIsNone(c.parse_any_block(["a.example.com {", '\trespond "x" 200', '\theader X-Y "1"', "}"]))

    def test_bloques_a_mano_no_se_confunden(self):
        # extra directive -> not a redirect / maintenance / routes
        self.assertIsNone(c.parse_redirect_block(["x.example.com {", "\tredir https://a.b permanent", "\tencode gzip", "}"]))
        self.assertIsNone(c.parse_maintenance_block(["x.example.com {", '\trespond "m" 503', "\tencode gzip", "}"]))
        self.assertIsNone(c.parse_routes_block(["x.example.com {", "\thandle {", "\t\treverse_proxy a:1", "\t}", "}"]))   # no path rule

    def test_el_import_de_la_blocklist_no_estorba(self):
        for block, parser in ((c.build_redirect_block(self.REDIRECT), c.parse_redirect_block),
                              (c.build_maintenance_block(self.MAINT), c.parse_maintenance_block),
                              (c.build_routes_block(self.ROUTES), c.parse_routes_block)):
            lines = self.lines(block)
            lines.insert(2, "\timport caddy_tui_blocklist")
            self.assertIsNotNone(parser(lines))

    def test_etiquetas(self):
        label = lambda b: c.block_label(self.lines(b))
        self.assertIn("→  https://example.com  [redirect]", label(c.build_redirect_block(self.REDIRECT)))
        self.assertIn("[maintenance]", label(c.build_maintenance_block(self.MAINT)))
        routes = label(c.build_routes_block(self.ROUTES))
        self.assertIn("[routes]", routes)
        self.assertIn("10.0.0.5:3000", routes)

    def test_build_block_for(self):
        self.assertIn("redir", c.build_block_for("redirect", self.REDIRECT, None, "edited"))
        self.assertIn("# Service edited", c.build_block_for("routes", self.ROUTES, None, "edited"))
        self.assertIn("reverse_proxy 10.0.0.1:80", c.build_block_for("proxy", dict(
            host="x.example.com", upstream="10.0.0.1:80", https_upstream=False, skip_verify=False, gzip=False,
            headers=False, log=False, user="", password=""), None))


class TemplateFlows(unittest.TestCase):
    """act_add / act_manage with the form of each kind replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.caddyfile = os.path.join(d, "Caddyfile")
        with open(self.caddyfile, "w", encoding="utf-8") as f:
            f.write(BLOCK_BASE)
        for name, val in (("CADDYFILE", self.caddyfile), ("BASE", d), ("BACKUPS", os.path.join(d, "bk"))):
            p = mock.patch.object(c, name, val)
            p.start()
            self.addCleanup(p.stop)
        for name, kw in (("validate_text", dict(return_value=(True, ""))), ("container_state", dict(return_value=("exited", "")))):
            p = mock.patch.object(c, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.app = c.App.__new__(c.App)
        self.shown = []
        self.app.show = lambda t, x: self.shown.append((t, x))
        self.app.busy = lambda m: None
        self.app.draw = lambda: None
        self.app.ask = lambda q, d=False: True

    def current(self):
        with open(self.caddyfile, encoding="utf-8") as f:
            return f.read()

    def add(self, kind_index, values):
        self.app.pick = lambda title, items: kind_index
        self.app.site_form = lambda kind, title, initial, existing, pw_hash=None, editing=False: values
        self.app.act_add()

    def test_alta_de_redireccion(self):
        self.add(2, SiteTemplates.REDIRECT)
        new = self.current()
        self.assertIn("redir https://example.com{uri} permanent", new)
        self.assertIn("app.example.com {", new)                  # the rest is untouched
        self.assertEqual(c.parse_any_block(new.split("\n")[new.split("\n").index("www.example.com {") :][:4])[0], "redirect")

    def test_alta_de_rutas_y_mantenimiento(self):
        self.add(1, SiteTemplates.ROUTES)
        self.assertIn("handle_path /api/* {", self.current())
        self.add(3, dict(SiteTemplates.MAINT, host="m.example.com"))
        self.assertIn('respond "Back at', self.current())

    def test_el_tipo_se_pregunta_y_se_puede_cancelar(self):
        asked = []
        self.app.pick = lambda title, items: (asked.append(items), None)[1]
        self.app.act_add()
        self.assertEqual(len(asked[0]), 4)
        self.assertEqual(self.current(), BLOCK_BASE)

    def test_alta_de_proxy_sigue_igual(self):
        v = dict(host="p.example.com", upstream="10.0.0.9:80", https_upstream=False, skip_verify=False, gzip=True,
                 headers=False, log=True, user="", password="")
        self.add(0, v)
        self.assertIn("reverse_proxy 10.0.0.9:80", self.current())

    def test_editar_una_redireccion_con_el_formulario(self):
        self.add(2, SiteTemplates.REDIRECT)
        text = self.current()
        blocks = c.find_site_blocks(text)
        idx = [b["addr"][0] for b in blocks].index("www.example.com")
        seq = iter([idx, 0])                                  # that site ; "Edit with the form"
        self.app.pick = lambda t, i: next(seq)
        seen = {}

        def fake_form(kind, title, initial, existing, pw_hash=None, editing=False):
            seen.update(kind=kind, initial=initial)
            return dict(initial, target="https://other.example.org", permanent=False)
        self.app.site_form = fake_form
        self.app.act_manage()
        self.assertEqual(seen["kind"], "redirect")
        new = self.current()
        self.assertIn("redir https://other.example.org{uri} temporary", new)
        self.assertIn("# Service edited with caddy-tui", new)


class PublishPortsOnAdd(unittest.TestCase):
    """A new site on its own port must also be published in the docker compose."""
    PROXY = dict(host="n.example.com:9443", upstream="10.0.0.9:80", https_upstream=False, skip_verify=False,
                 gzip=True, headers=False, log=True, user="", password="")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.caddyfile = os.path.join(d, "Caddyfile")
        self.compose = os.path.join(d, "docker-compose.yml")
        for path, text in ((self.caddyfile, "app.example.com {\n\treverse_proxy a:1\n}\n"), (self.compose, COMPOSE)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        for name, val in (("CADDYFILE", self.caddyfile), ("COMPOSE", self.compose), ("SERVICE", "caddy"),
                          ("BASE", d), ("BACKUPS", os.path.join(d, "bk"))):
            p = mock.patch.object(c, name, val)
            p.start()
            self.addCleanup(p.stop)
        for name, kw in (("validate_text", dict(return_value=(True, ""))),
                         ("container_state", dict(return_value=("exited", ""))),
                         ("run", dict(return_value=(0, "")))):
            p = mock.patch.object(c, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.app = c.App.__new__(c.App)
        self.questions, self.recreated = [], []
        self.answer = True
        self.app.show = lambda t, x: None
        self.app.busy = lambda m: None
        self.app.draw = lambda: None
        self.app.ask = lambda q, d=False: (self.questions.append(q), self.answer)[1]
        self.app.act_compose = lambda *a, **k: self.recreated.append(a)
        self.app.pick = lambda title, items: 0
        self.app.site_form = lambda *a, **k: dict(self.PROXY)

    def test_puertos_publicados_en_el_fichero(self):
        self.assertEqual(c.compose_service_ports(COMPOSE, "caddy"), {80, 443})
        self.assertEqual(c.compose_service_ports('services:\n  caddy:\n    ports:\n      - 127.0.0.1:8080:80/tcp\n      - 9000:9000\n', "caddy"), {8080, 9000})

    def test_detecta_el_puerto_que_falta(self):
        self.assertEqual(c.compose_missing_ports("a.com:9443 {\n\trespond ok\n}\n"), [9443])
        self.assertEqual(c.compose_missing_ports("a.com {\n\trespond ok\n}\n"), [])

    def test_alta_ofrece_publicar_y_recrear(self):
        self.app.act_add()
        self.assertIn('- "9443:9443"', slurp(self.compose))
        self.assertTrue(any("Publish port" in q for q in self.questions))
        self.assertEqual(self.recreated, [("Recreate", "up", "-d", "--force-recreate", "--no-deps", "caddy")])

    def test_si_se_rechaza_no_se_toca_el_compose(self):
        self.answer = False
        self.app.act_add()
        self.assertEqual(slurp(self.compose), COMPOSE)
        self.assertEqual(self.recreated, [])

    def test_sin_puerto_nuevo_no_pregunta(self):
        self.app.site_form = lambda *a, **k: dict(self.PROXY, host="n.example.com")
        self.app.act_add()
        self.assertFalse(any("Publish port" in q for q in self.questions))

    def test_detecta_el_puerto_en_un_caddyfile_ya_guardado(self):
        with open(self.caddyfile, "a", encoding="utf-8") as f:
            f.write("n.example.com:9443 {\n\trespond ok\n}\n")
        self.assertEqual(c.compose_missing_ports(slurp(self.caddyfile)), [9443])


class ExportImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.src = os.path.join(d, "src")
        os.makedirs(self.src)
        for name, text in (("Caddyfile", "{$DOMAIN} {\n\trespond \"hi\"\n}\n"), ("docker-compose.yml", "services: {}\n"),
                           (".env", "DOMAIN=app.example.com\nSECRET=s3\n")):
            with open(os.path.join(self.src, name), "w") as f:
                f.write(text)
        envs = {"CADDY_TUI_STATE_DIR": d}
        for p in (mock.patch.dict(os.environ, envs, clear=True), mock.patch.object(c, "CADDYFILE", os.path.join(self.src, "Caddyfile")),
                  mock.patch.object(c, "COMPOSE", os.path.join(self.src, "docker-compose.yml")),
                  mock.patch.object(c, "ENVFILE", os.path.join(self.src, ".env")), mock.patch.object(c, "ALERT_ENV", os.path.join(d, "alert.env")),
                  mock.patch.object(c, "CONTAINER", "caddy"), mock.patch.object(c, "IMAGE", "caddy:2-alpine"),
                  mock.patch.object(c, "SERVICE", "caddy")):
            p.start()
            self.addCleanup(p.stop)
        self.bundle = os.path.join(d, "export.tar.gz")

    def test_ida_y_vuelta(self):
        with mock.patch.object(c, "export_certs", return_value=b"CERTS"):
            manifest = c.build_export(self.bundle, with_certs=True)
        self.assertEqual(stat.S_IMODE(os.stat(self.bundle).st_mode), 0o600)
        self.assertEqual(sorted(manifest["files"]), [".env", "Caddyfile", "caddy_data.tar.gz", "docker-compose.yml"])
        m2, files = c.read_export(self.bundle)
        self.assertEqual(files["caddy_data.tar.gz"], b"CERTS")
        self.assertIn(b"DOMAIN=app.example.com", files[".env"])
        self.assertEqual((m2["image"], m2["container"], m2["host"] != ""), ("caddy:2-alpine", "caddy", True))

    def test_sin_certificados_ni_alertas_por_defecto(self):
        with open(c.ALERT_ENV, "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=1:x\n")
        manifest = c.build_export(self.bundle)
        self.assertNotIn("caddy_data.tar.gz", manifest["files"])
        self.assertNotIn("alert.env", manifest["files"])
        self.assertIn("alert.env", c.build_export(self.bundle + "2", with_alerts=True)["files"])

    def test_sin_compose(self):
        with mock.patch.object(c, "COMPOSE", ""):
            self.assertNotIn("docker-compose.yml", c.build_export(self.bundle)["files"])

    def rewrite(self, mutate):
        """Re-pack the bundle after `mutate(members_dict)`."""
        c.build_export(self.bundle)
        with tarfile.open(self.bundle, "r:gz") as tar:
            members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
        mutate(members)
        with tarfile.open(self.bundle, "w:gz") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    def test_detecta_corrupcion(self):
        self.rewrite(lambda m: m.update({"Caddyfile": b"tampered"}))
        with self.assertRaisesRegex(ValueError, "Caddyfile"):
            c.read_export(self.bundle)

    def test_rechaza_entradas_inesperadas(self):
        self.rewrite(lambda m: m.update({"../../etc/passwd": b"x"}))
        with self.assertRaisesRegex(ValueError, "Unexpected entry"):
            c.read_export(self.bundle)

    def test_sin_manifiesto_o_no_es_un_export(self):
        self.rewrite(lambda m: m.pop("manifest.json"))
        with self.assertRaises(ValueError):
            c.read_export(self.bundle)
        with open(self.bundle, "w") as f:
            f.write("not a tar")
        with self.assertRaisesRegex(ValueError, "Not a caddy-tui export"):
            c.read_export(self.bundle)
        with self.assertRaises(ValueError):
            c.read_export(os.path.join(self.tmp.name, "missing.tar.gz"))

    def test_symlinks_no_se_aceptan(self):
        c.build_export(self.bundle)
        with tarfile.open(self.bundle, "a" if False else "w:gz") as tar:
            info = tarfile.TarInfo("Caddyfile")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
        with self.assertRaisesRegex(ValueError, "Unexpected entry"):
            c.read_export(self.bundle)


class ImportFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        src = os.path.join(d, "src")
        os.makedirs(src)
        for name, text in (("Caddyfile", "{$DOMAIN} {\n\trespond \"hi\"\n}\n"), ("docker-compose.yml", "services: {caddy: {}}\n"),
                           (".env", "DOMAIN=app.example.com\n")):
            with open(os.path.join(src, name), "w") as f:
                f.write(text)
        self.bundle = os.path.join(d, "x.tar.gz")
        with mock.patch.object(c, "CADDYFILE", os.path.join(src, "Caddyfile")), mock.patch.object(c, "COMPOSE", os.path.join(src, "docker-compose.yml")), \
             mock.patch.object(c, "ENVFILE", os.path.join(src, ".env")), mock.patch.object(c, "CONTAINER", "mycaddy"), \
             mock.patch.object(c, "IMAGE", "caddy:2-alpine"), mock.patch.object(c, "SERVICE", "caddy"), \
             mock.patch.dict(os.environ, {"CADDY_TUI_STATE_DIR": d}), mock.patch.object(c, "export_certs", return_value=b"CERTS"):
            c.build_export(self.bundle, with_certs=True)
        self.dest = os.path.join(d, "dest")
        self.calls, self.bytes_calls, self.out = [], [], io.StringIO()

    def go(self, compose=True, validate=(True, ""), busy=(), yes=True, answers=(), existing=False, bundle=None):
        ans = iter(answers)
        args = argparse.Namespace(dir=self.dest, yes=yes)

        def fake_run(cmd, **k):
            self.calls.append(cmd)
            return (0, "cid123\n") if "ps" in cmd else (0, "")
        patches = [
            mock.patch.object(c, "native_caddy_running", return_value=False), mock.patch.object(c, "port_in_use", side_effect=lambda p: p in busy),
            mock.patch.object(c, "compose_available", return_value=compose), mock.patch.object(c, "validate_standalone", return_value=validate),
            mock.patch.object(c, "inspect_container", return_value={"x": 1} if existing else None),
            mock.patch.object(c, "container_state_of", return_value=("running", "")), mock.patch.object(c, "run", side_effect=fake_run),
            mock.patch.object(c, "run_bytes", side_effect=lambda cmd, data=None, **k: (self.bytes_calls.append((cmd, data)), (0, b"", ""))[1]),
            mock.patch.object(c, "ask_yes", side_effect=lambda *a, **k: next(ans, True)), mock.patch.object(c.time, "sleep"),
            mock.patch.object(c, "BASE", self.tmp.name),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with contextlib.redirect_stdout(self.out):
            return c.import_flow(args, bundle or self.bundle)

    def read(self, name):
        return slurp(os.path.join(self.dest, name))

    def test_restaura_ficheros_certificados_y_arranca(self):
        self.assertEqual(self.go(), "mycaddy")
        self.assertIn("respond", self.read("Caddyfile"))
        self.assertIn("services:", self.read("docker-compose.yml"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dest, ".env")).st_mode), 0o600)
        cmd, data = self.bytes_calls[0]
        self.assertEqual(data, b"CERTS")
        self.assertIn("--volumes-from", cmd)
        self.assertIn("cid123", cmd)
        self.assertIn("create", [x for call in self.calls for x in call])
        self.assertEqual(self.calls[-1][-2:], ["up", "-d"])
        self.assertIn("Certificates and ACME account restored", self.out.getvalue())

    def test_valida_con_las_variables_del_env(self):
        self.go()
        self.assertEqual(c.validate_standalone.call_args[0][2], {"DOMAIN": "app.example.com"})   # the patched one from go()

    def test_sin_compose_usa_docker_run(self):
        self.assertEqual(self.go(compose=False), "mycaddy")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "docker-compose.yml")))
        self.assertEqual(self.calls[-1][:3], ["docker", "run", "-d"])

    def test_no_pisa_una_carpeta_con_configuracion(self):
        os.makedirs(self.dest)
        with open(os.path.join(self.dest, "Caddyfile"), "w") as f:
            f.write("keep")
        self.assertIsNone(self.go())
        self.assertEqual(self.read("Caddyfile"), "keep")

    def test_config_invalida_no_escribe_nada(self):
        self.assertIsNone(self.go(validate=(False, "boom")))
        self.assertFalse(os.path.exists(self.dest))

    def test_contenedor_con_el_mismo_nombre(self):
        self.assertIsNone(self.go(existing=True))
        self.assertIn("already exists", self.out.getvalue())

    def test_pide_confirmacion_si_no_es_yes(self):
        self.assertIsNone(self.go(yes=False, answers=(False,)))
        self.assertFalse(os.path.exists(self.dest))

    def test_export_danado(self):
        with open(self.bundle, "w") as f:
            f.write("junk")
        self.assertIsNone(self.go())
        self.assertIn("Not a caddy-tui export", self.out.getvalue())


class DockerAvailability(unittest.TestCase):
    def status(self, rc, out):
        with mock.patch.object(c, "run", return_value=(rc, out)):
            return c.docker_status()[0]

    def test_clasifica_los_errores_reales_de_docker(self):
        self.assertEqual(self.status(0, "29.1.3"), "ok")
        self.assertEqual(self.status(1, "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"), "denied")
        self.assertEqual(self.status(1, "permission denied while trying to connect to the Docker daemon socket"), "denied")
        self.assertEqual(self.status(1, "failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is correct and if the daemon is running"), "stopped")
        self.assertEqual(self.status(1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"), "stopped")

    def ensure(self, statuses, interactive=True, root=True, systemd=True, answer=True, start_rc=0):
        said, calls, seq = io.StringIO(), [], iter(statuses)
        with mock.patch.object(c, "docker_status", side_effect=lambda: (next(seq), "")), \
             mock.patch.object(os, "geteuid", return_value=0 if root else 1000), \
             mock.patch.object(c, "_systemd_available", return_value=systemd), \
             mock.patch.object(c, "ask_yes", return_value=answer), \
             mock.patch.object(c, "run", side_effect=lambda cmd, **k: (calls.append(cmd), (start_rc, "boom"))[1]), \
             contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            return c.ensure_docker_running(interactive), calls, said.getvalue()

    def test_todo_bien(self):
        self.assertEqual(self.ensure(["ok"]), (True, [], ""))

    def test_sin_permiso_recomienda_sudo(self):
        ok, calls, out = self.ensure(["denied"])
        self.assertFalse(ok)
        self.assertEqual(calls, [])
        self.assertIn("sudo", out)

    def test_parado_y_root_ofrece_arrancarlo(self):
        ok, calls, out = self.ensure(["stopped", "ok"])
        self.assertTrue(ok)
        self.assertEqual(calls, [["systemctl", "enable", "--now", "docker"]])
        self.assertIn("Docker is running", out)

    def test_parado_y_el_usuario_dice_que_no(self):
        ok, calls, out = self.ensure(["stopped"], answer=False)
        self.assertFalse(ok)
        self.assertEqual(calls, [])
        self.assertIn("sudo systemctl start docker", out)

    def test_parado_sin_root_o_sin_systemd_o_sin_interactivo_solo_explica(self):
        for kw in (dict(root=False), dict(systemd=False), dict(interactive=False)):
            ok, calls, out = self.ensure(["stopped"], **kw)
            self.assertFalse(ok)
            self.assertEqual(calls, [])
            self.assertIn("sudo systemctl start docker", out)

    def test_no_consigue_arrancarlo(self):
        ok, calls, out = self.ensure(["stopped", "stopped"], start_rc=1)
        self.assertFalse(ok)
        self.assertIn("Could not start Docker", out)


class SeveralCaddy(unittest.TestCase):
    ROWS = [("web-caddy", "caddy:2-alpine", "running"), ("api-caddy", "caddy:2", "exited")]

    def setUp(self):
        self.snap = c.snapshot_config()
        self.addCleanup(c.restore_config, self.snap)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"CADDY_TUI_STATE_DIR": self.tmp.name, "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "cfg")}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.args = argparse.Namespace(container="x", caddyfile="/f", dir="/d", compose="/c", service="s", image="i", json=False,
                                       quiet=False, notify=False)

    def fake_configure(self, fail=()):
        seen = []

        def configure(ns):
            seen.append(ns)
            if ns.container in fail:
                raise SystemExit(f"cannot configure {ns.container}")
            c.CONTAINER, c.CADDYFILE = ns.container, f"/etc/{ns.container}/Caddyfile"
        return configure, seen

    def test_cambiar_de_caddy_ignora_las_opciones_del_primero(self):
        fake, seen = self.fake_configure()
        with mock.patch.object(c, "configure", side_effect=fake):
            self.assertEqual(c.configure_other(self.args, "api-caddy"), (True, ""))
        self.assertEqual(c.CONTAINER, "api-caddy")
        ns = seen[0]
        self.assertEqual((ns.container, ns.caddyfile, ns.dir, ns.compose, ns.service, ns.image), ("api-caddy", None, None, None, None, None))

    def test_si_falla_se_queda_el_anterior(self):
        c.CONTAINER, c.CADDYFILE = "web-caddy", "/etc/web/Caddyfile"
        fake, __ = self.fake_configure(fail=("api-caddy",))
        with mock.patch.object(c, "configure", side_effect=fake):
            ok, msg = c.configure_other(self.args, "api-caddy")
        self.assertEqual((ok, msg, c.CONTAINER, c.CADDYFILE), (False, "cannot configure api-caddy", "web-caddy", "/etc/web/Caddyfile"))

    def run_all(self, codes, rows=None, fail=(), **kw):
        fake, __ = self.fake_configure(fail=fail)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(c, "list_caddy_containers", return_value=self.ROWS if rows is None else rows), \
             mock.patch.object(c, "configure", side_effect=fake), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = c.run_for_all(self.args, lambda a: (print(f"ran {c.CONTAINER}"), codes[c.CONTAINER])[1], **kw)
        return code, out.getvalue(), err.getvalue()

    def test_run_for_all_devuelve_el_peor_codigo(self):
        code, out, __ = self.run_all({"web-caddy": 0, "api-caddy": 2})
        self.assertEqual(code, 2)
        self.assertIn("== web-caddy ==\nran web-caddy", out)
        self.assertIn("== api-caddy ==\nran api-caddy", out)
        self.assertFalse(c.MULTI)

    def test_un_solo_caddy_no_pone_cabeceras(self):
        code, out, __ = self.run_all({"web-caddy": 1}, rows=self.ROWS[:1])
        self.assertEqual((code, out), (1, "ran web-caddy\n"))

    def test_si_uno_no_se_puede_configurar_es_3_pero_sigue_con_los_demas(self):
        code, out, err = self.run_all({"web-caddy": 0, "api-caddy": 0}, fail=("web-caddy",))
        self.assertEqual(code, 3)
        self.assertIn("ran api-caddy", out)
        self.assertIn("cannot configure web-caddy", err)

    def test_ninguno_es_3(self):
        code, __, err = self.run_all({}, rows=[])
        self.assertEqual(code, 3)
        self.assertIn("No Caddy container", err)

    def test_estado_de_alertas_separado_por_contenedor(self):
        c.CONTAINER = "web-caddy"
        a = c.state_path()
        c.CONTAINER = "api/caddy"
        b = c.state_path()
        self.assertNotEqual(a, b)
        self.assertTrue(b.endswith("alert-state-api_caddy.json"))

    def test_estado_antiguo_compartido_se_lee_como_semilla(self):
        with open(os.path.join(self.tmp.name, "alert-state.json"), "w") as f:
            json.dump({"failing": True, "digest": "abc"}, f)
        c.CONTAINER = "web-caddy"
        self.assertEqual(c.read_alert_state()["digest"], "abc")
        with open(c.state_path(), "w") as f:
            json.dump({"failing": False}, f)
        self.assertEqual(c.read_alert_state(), {"failing": False})               # the new file wins

    def test_baneos_por_contenedor(self):
        c.CONTAINER = "web-caddy"
        c.register_ban("1.1.1.1", "scanner", 3600, now=1000)
        c.CONTAINER = "api-caddy"
        self.assertEqual(c.load_bans(), {})
        c.register_ban("2.2.2.2", "scanner", 0, now=1000)
        self.assertEqual(sorted(c.load_bans()), ["2.2.2.2"])
        c.CONTAINER = "web-caddy"
        self.assertEqual(sorted(c.load_bans()), ["1.1.1.1"])

    def test_el_fichero_de_baneos_antiguo_lo_adopta_el_primero_que_lo_lee(self):
        with open(c.bans_path(), "w") as f:
            json.dump({"9.9.9.9": {"reason": "old", "ts": 1, "until": None}}, f)
        c.CONTAINER = "web-caddy"
        self.assertEqual(list(c.load_bans()), ["9.9.9.9"])
        c.register_ban("1.1.1.1", "", 0, now=5)
        self.assertEqual(sorted(c.load_bans()), ["1.1.1.1", "9.9.9.9"])
        c.CONTAINER = "api-caddy"
        self.assertEqual(c.load_bans(), {})

    def test_las_notificaciones_llevan_el_nombre_del_contenedor_si_hay_varios(self):
        os.environ.update(TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="42")
        sent = []
        c.CONTAINER = "web-caddy"
        items = [("err", "Port missing", [])]
        with mock.patch.object(c, "telegram_send", side_effect=lambda t, ch, txt: sent.append(txt)):
            c.notify_result(items, 2)
            c.MULTI = True
            try:
                c.CONTAINER = "api-caddy"
                c.notify_result(items, 2)
            finally:
                c.MULTI = False
        self.assertNotIn("/web-caddy", sent[0])
        self.assertIn("/api-caddy:", sent[1])

    def test_run_check_recoge_el_json_en_vez_de_imprimirlo(self):
        self.args.json = True
        collected = []
        c.CONTAINER = "web-caddy"
        out = io.StringIO()
        with mock.patch.object(c, "diagnose", return_value=([("ok", "fine", [])], set(), {})), contextlib.redirect_stdout(out):
            code = c.run_check(self.args, collect=collected)
        self.assertEqual((code, out.getvalue(), collected[0]["container"], collected[0]["status"]), (0, "", "web-caddy", "ok"))

    def test_unidades_para_todos(self):
        self.assertIn("ExecStart=/x --check --quiet --notify --all", c.render_service("/x", False, True))
        self.assertIn("ExecStartPre=-/x --auto-block --all", c.render_service("/x", True, True))
        self.assertNotIn("--all", c.render_service("/x", True, False))
        cron = c.render_cron("/x", 30, True, True)
        self.assertIn("/x --auto-block --all >/dev/null 2>&1; /x --check --quiet --notify --all", cron)

    def test_print_caddies(self):
        info = {"Mounts": [{"Destination": "/etc/caddy/Caddyfile", "Source": "/srv/web/Caddyfile"}]}
        out = io.StringIO()
        with mock.patch.object(c, "list_caddy_containers", return_value=self.ROWS), \
             mock.patch.object(c, "inspect_container", side_effect=lambda n: info if n == "web-caddy" else None), \
             contextlib.redirect_stdout(out):
            self.assertEqual(c.print_caddies(), 0)
        text = out.getvalue()
        self.assertIn("web-caddy", text)
        self.assertIn("/srv/web/Caddyfile", text)
        self.assertIn("api-caddy", text)

    def test_config_json_conserva_otras_claves(self):
        c.update_config(lang="es")
        c.update_config(container="web-caddy")
        self.assertEqual(c.load_config(), {"lang": "es", "container": "web-caddy"})
        self.assertEqual(c.load_saved_lang(), "es")

    def test_eleccion_al_arrancar_recuerda_el_ultimo(self):
        c.update_config(container="api-caddy")
        out = io.StringIO()
        args = argparse.Namespace(container=None, caddyfile=None, dir=None, compose=None, service=None, image=None)
        with mock.patch.object(c, "list_caddy_containers", return_value=self.ROWS), mock.patch.object(c.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value=""), mock.patch.object(c, "inspect_container", return_value=None), \
             contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                c.configure(args)                                  # inspect fails: we only look at who was chosen
        self.assertIn("2) api-caddy", out.getvalue())
        self.assertIn("←", out.getvalue().split("2) api-caddy")[1].splitlines()[0])

    def test_act_switch(self):
        app = c.App.__new__(c.App)
        shown = []
        app.show = lambda title, text: shown.append((title, text))
        app.refresh_state = lambda force=False: None
        app.args = self.args
        c.CONTAINER = "web-caddy"
        fake, __ = self.fake_configure()
        with mock.patch.object(c, "list_caddy_containers", return_value=self.ROWS), mock.patch.object(c, "configure", side_effect=fake):
            app.pick = lambda title, items: (self.assertTrue(items[0].startswith("● ")), 1)[1]       # the active one is marked
            app.act_switch()
        self.assertEqual(c.CONTAINER, "api-caddy")
        self.assertIn("Now managing api-caddy", shown[-1][1])
        self.assertEqual(c.load_config()["container"], "api-caddy")
        # choosing the active one does nothing; a single Caddy explains it
        app.pick = lambda title, items: 1
        with mock.patch.object(c, "list_caddy_containers", return_value=self.ROWS):
            app.act_switch()
        with mock.patch.object(c, "list_caddy_containers", return_value=self.ROWS[:1]):
            app.act_switch()
        self.assertIn("Only one Caddy", shown[-1][1])


def sev(ts, host="a.example.com", ip="45.83.64.7", status=200, size=100, dur=0.01, uri="/"):
    return (ts, host, "GET", uri, ip, status, size, dur)


class Statistics(unittest.TestCase):
    H = 3600
    T0 = 1_760_000_000 // 3600 * 3600          # an exact hour

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"CADDY_TUI_STATE_DIR": self.tmp.name}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.snap = c.snapshot_config()
        self.addCleanup(c.restore_config, self.snap)
        c.CONTAINER = "web-caddy"
        self.db = c.stats_db()
        self.addCleanup(self.db.close)

    EVENTS = None

    def events(self):
        t = self.T0
        return [sev(t + 10), sev(t + 20, status=404, size=50), sev(t + 30, status=500, dur=2.0),
                sev(t + 3700, host="b.example.com", ip="45.83.64.8"), sev(t + 3710, host="b.example.com", ip="45.83.64.8", status=301)]

    def test_agrega_por_hora_y_sitio(self):
        hourly, ips, last, n = c.aggregate_events(self.events())
        self.assertEqual(n, 5)
        self.assertEqual(last, self.T0 + 3710)
        a = hourly[(self.T0, "a.example.com")]
        self.assertEqual(a[:6], [3, 250, 1, 0, 1, 1])               # requests, bytes, 2xx, 3xx, 4xx, 5xx
        self.assertAlmostEqual(a[6], 2.02)
        self.assertEqual(a[7], 2.0)
        self.assertEqual(hourly[(self.T0 + self.H, "b.example.com")][:6], [2, 200, 1, 1, 0, 0])
        day = self.T0 // 86400 * 86400
        self.assertEqual(ips[(day, "45.83.64.7")], [3, 2])           # 3 requests, 2 of them errors

    def test_ignora_lo_ya_contado(self):
        __, __, last, n = c.aggregate_events(self.events(), since_ts=self.T0 + 20)
        self.assertEqual((n, last), (3, self.T0 + 3710))

    def test_recolectar_dos_veces_no_duplica_y_suma_lo_nuevo(self):
        now = self.T0 + 7200
        self.assertEqual(c.collect_stats(self.db, events=self.events(), now=now), 5)
        self.assertEqual(c.collect_stats(self.db, events=self.events(), now=now), 0)           # same log again: cursor
        extra = self.events() + [sev(self.T0 + 3800, host="b.example.com", ip="45.83.64.8", status=502)]
        self.assertEqual(c.collect_stats(self.db, events=extra, now=now), 1)
        rep = c.stats_report(self.db, "web-caddy", self.T0, self.T0 + 2 * self.H)
        self.assertEqual((rep["requests"], rep["s2"], rep["s3"], rep["s4"], rep["s5"]), (6, 2, 1, 1, 2))

    def test_cada_contenedor_tiene_lo_suyo(self):
        c.collect_stats(self.db, events=self.events(), now=self.T0 + 7200)
        c.CONTAINER = "api-caddy"
        self.assertEqual(c.collect_stats(self.db, events=[sev(self.T0 + 5)], now=self.T0 + 7200), 1)
        self.assertEqual(c.stats_report(self.db, "api-caddy", self.T0, self.T0 + self.H)["requests"], 1)
        self.assertEqual(c.stats_report(self.db, "web-caddy", self.T0, self.T0 + self.H)["requests"], 3)

    def test_informe(self):
        c.collect_stats(self.db, events=self.events(), now=self.T0 + 7200)
        rep = c.stats_report(self.db, "web-caddy", self.T0, self.T0 + 2 * self.H)
        self.assertEqual((rep["requests"], rep["bytes"]), (5, 450))
        self.assertEqual([r[0] for r in rep["by_site"]], ["a.example.com", "b.example.com"])
        self.assertEqual(rep["dur_max"], 2.0)
        self.assertEqual(rep["top_ips"][0], ("45.83.64.7", 3, 2))
        self.assertEqual((rep["first"], rep["last"]), (self.T0, self.T0 + self.H))
        only_first = c.stats_report(self.db, "web-caddy", self.T0, self.T0 + self.H)
        self.assertEqual(only_first["requests"], 3)

    def test_eventos_muy_antiguos_no_entran(self):
        old = [sev(self.T0 - 200 * 86400)]
        self.assertEqual(c.collect_stats(self.db, events=old, now=self.T0), 0)

    def test_poda(self):
        c.collect_stats(self.db, events=self.events(), now=self.T0 + 7200)
        c.prune_stats(self.db, now=self.T0 + 91 * 86400)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM hourly").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM daily_ips").fetchone()[0], 0)
        c.collect_stats(self.db, events=[sev(self.T0 + 91 * 86400)], now=self.T0 + 91 * 86400)
        c.prune_stats(self.db, now=self.T0 + 92 * 86400)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM hourly").fetchone()[0], 1)

    def test_base_de_datos_privada(self):
        self.assertEqual(stat.S_IMODE(os.stat(c.stats_path()).st_mode), 0o600)

    def test_texto_del_informe(self):
        c.collect_stats(self.db, events=self.events(), now=self.T0 + 7200)
        rep = c.stats_report(self.db, "web-caddy", self.T0, self.T0 + 2 * self.H)
        text = c.render_stats(rep, "TITLE", hourly_rows=True)
        for frag in ("TITLE", "Requests: 5", "5xx 1 (20.0%)", "Busiest hour", "Requests per hour", "a.example.com", "b.example.com",
                     "45.83.64.7", "Data available"):
            self.assertIn(frag, text)
        by_day = c.render_stats(rep, "T", hourly_rows=False)
        self.assertIn("Requests per day", by_day)
        self.assertEqual(c.render_stats(c.stats_report(self.db, "web-caddy", 0, 1), "T", True), "No data for this period yet.")

    def test_period_report_y_cli(self):
        with mock.patch.object(c, "iter_access_events", return_value=iter(self.events())):
            rep, text = c.period_report("7d", now=self.T0 + 7200)
        self.assertEqual(rep["requests"], 5)
        self.assertIn("last 7d · web-caddy", text)
        out = io.StringIO()
        args = argparse.Namespace(stats="7d", json=True)
        with mock.patch.object(c, "iter_access_events", return_value=iter([])), mock.patch.object(c.time, "time", return_value=self.T0 + 7200), \
             contextlib.redirect_stdout(out):
            self.assertEqual(c.run_stats(args), 0)
        data = json.loads(out.getvalue())
        self.assertEqual((data["requests"], data["container"], data["period"]), (5, "web-caddy", "7d"))
        self.assertEqual(data["sites"][0]["host"], "a.example.com")

    def test_recolectar_desde_la_linea_de_comandos(self):
        out = io.StringIO()
        with mock.patch.object(c, "iter_access_events", return_value=iter(self.events())), mock.patch.object(c.time, "time", return_value=self.T0 + 7200), \
             contextlib.redirect_stdout(out):
            c.run_collect_stats(argparse.Namespace(quiet=False))
        self.assertIn("5 new request(s) counted for web-caddy", out.getvalue())

    def test_unidades_con_estadisticas(self):
        self.assertIn("ExecStartPre=-/x --collect-stats --quiet", c.render_service("/x", False, False, True))
        self.assertIn("--collect-stats --quiet --all", c.render_service("/x", False, True, True))
        self.assertNotIn("--collect-stats", c.render_service("/x"))
        self.assertIn("/x --collect-stats --quiet >/dev/null 2>&1; /x --check", c.render_cron("/x", 60, False, False, True))

    def test_pantalla_de_estadisticas(self):
        app = c.App.__new__(c.App)
        shown = []
        app.show = lambda title, text: shown.append((title, text))
        app.busy = lambda m: None
        app.pick = lambda title, items: 1                                  # "Last 7 days"
        with mock.patch.object(c, "container_state", return_value=("running", "")), \
             mock.patch.object(c, "iter_access_events", return_value=iter(self.events())), mock.patch.object(c.time, "time", return_value=self.T0 + 7200):
            app.act_stats()
        self.assertIn("Requests: 5", shown[-1][1])


class ValidateWithoutNewContainers(unittest.TestCase):
    """validate_text must not create containers while Caddy runs (periodic checks would flood docker events)."""
    TEXT = "a.example.com {\n}\n"

    def setUp(self):
        self.calls = []
        self.snap = c.snapshot_config()
        self.addCleanup(c.restore_config, self.snap)
        c.CONTAINER = "caddy"

    def validate(self, text=TEXT, state="running", cenv=None, live=None, exec_result=(0, ""), run_result=(0, "Valid configuration")):
        """`live` = what the container is serving (None: different from `text`)."""
        def fake_run(cmd, **k):
            self.calls.append((cmd, k.get("stdin")))
            if cmd[:2] == ["docker", "exec"] and "cat" in cmd and c.CONF_IN_CONTAINER in cmd:
                return 0, (live if live is not None else "something else\n")
            return exec_result if cmd[:2] == ["docker", "exec"] else run_result
        with mock.patch.object(c, "container_state", return_value=(state, "")), \
             mock.patch.object(c, "container_env", return_value=cenv or {}), \
             mock.patch.object(c, "run", side_effect=fake_run), \
             mock.patch.object(c, "runtime_env", return_value={}), \
             mock.patch.object(c, "CADDYDIR", tempfile.gettempdir()):
            return c.validate_text(text)

    def subcommands(self):
        return [cmd[1] for cmd, __ in self.calls if cmd[0] == "docker"]

    def validated_with(self):
        return [cmd for cmd, __ in self.calls if "validate" in " ".join(cmd) or cmd[:2] == ["docker", "run"]]

    def test_lo_que_sirve_el_contenedor_se_valida_el_propio_fichero(self):
        ok, __ = self.validate(live=self.TEXT, exec_result=(0, "Valid configuration"))
        self.assertTrue(ok)
        self.assertEqual(set(self.subcommands()), {"exec"})                    # not a single `docker run`
        cmd = self.validated_with()[0]
        self.assertEqual(cmd[-4:], ["--config", c.CONF_IN_CONTAINER, "--adapter", "caddyfile"])   # the very file: imports as for the real server

    def test_un_candidato_viaja_por_stdin_dentro_del_contenedor(self):
        ok, __ = self.validate(live="old\n", exec_result=(0, "Valid configuration"))
        self.assertTrue(ok)
        self.assertEqual(set(self.subcommands()), {"exec"})
        self.assertEqual([stdin for cmd, stdin in self.calls if stdin], [self.TEXT])

    def test_el_resultado_negativo_llega_tal_cual(self):
        ok, out = self.validate(live=self.TEXT, exec_result=(1, "adapting config using caddyfile: syntax error"))
        self.assertFalse(ok)
        self.assertIn("syntax error", out)
        self.assertEqual(set(self.subcommands()), {"exec"})

    def test_parado_usa_el_contenedor_desechable(self):
        self.validate(state="exited")
        self.assertEqual(self.subcommands(), ["run"])

    def test_candidato_con_import_de_fichero_relativo_cae_al_contenedor_desechable(self):
        ok, __ = self.validate(live="old\n", exec_result=(1, "File to import not found: seguridad"))
        self.assertTrue(ok)                                                  # the answer is the one from the fallback
        self.assertIn("run", self.subcommands())

    def test_imagen_sin_shell_cae_al_contenedor_desechable(self):
        for rc in (125, 126, 127):
            self.calls.clear()
            self.validate(live="old\n", exec_result=(rc, "sh: not found"))
            self.assertIn("run", self.subcommands(), rc)
        self.calls.clear()
        self.validate(live="old\n", exec_result=(1, 'OCI runtime exec failed: exec: "sh": executable file not found in $PATH'))
        self.assertIn("run", self.subcommands())

    def test_variable_que_el_contenedor_no_tiene_va_directa_al_desechable(self):
        text = "{$NEWVAR} {\n}\n"
        self.validate(text=text, cenv={"DOMAIN": "x"})
        self.assertEqual(self.subcommands(), ["run"])
        self.calls.clear()
        self.validate(text=text, cenv={"NEWVAR": "x.example.com"})           # the container has it: exec
        self.assertNotIn("run", self.subcommands())
        self.calls.clear()
        self.validate(text="{$OPTIONAL:fallback} {\n}\n")                   # has a default: exec
        self.assertNotIn("run", self.subcommands())


class DockerDesktopPaths(unittest.TestCase):
    def test_host_path(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "Caddyfile")
            open(real, "w").close()
            self.assertEqual(c.host_path("/host_mnt" + real), real)                    # Docker Desktop for Mac
            self.assertEqual(c.host_path("/run/desktop/mnt/host" + real), real)        # Docker Desktop / WSL
            self.assertEqual(c.host_path(real), real)                                  # a normal Linux path is untouched
            self.assertEqual(c.host_path("/host_mnt/no/such/file"), "/host_mnt/no/such/file")   # nothing to map to: unchanged
            self.assertEqual(c.host_path(""), "")
            self.assertEqual(c.host_path("-"), "-")

    def test_configure_usa_la_ruta_de_la_maquina(self):
        snap = c.snapshot_config()
        self.addCleanup(c.restore_config, snap)
        with tempfile.TemporaryDirectory() as d:
            cf = os.path.join(d, "Caddyfile")
            open(cf, "w").close()
            info = {"Config": {"Image": "caddy:2", "Labels": {}}, "Mounts": [
                {"Type": "bind", "Destination": "/etc/caddy/Caddyfile", "Source": "/host_mnt" + cf}]}
            args = argparse.Namespace(container="x", caddyfile=None, dir=None, compose=None, service=None, image=None)
            with mock.patch.object(c, "inspect_container", return_value=info):
                c.configure(args)
            self.assertEqual(c.CADDYFILE, cf)


class Translations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ids = source_msgids()
        cls.po = {f[:-3]: read_po(os.path.join(HERE, "..", "po", f))
                  for f in os.listdir(os.path.join(HERE, "..", "po")) if f.endswith(".po")}

    def test_la_plantilla_pot_esta_al_dia(self):
        """po/caddy-tui.pot lists exactly the strings in the source (checked without xgettext, whose output
        differs between versions, e.g. flags or strings nested in f-strings)."""
        pot = read_po(os.path.join(HERE, "..", "po", "caddy-tui.pot"))
        self.assertEqual(sorted(self.ids - set(pot)), [], "run `make pot`: missing from the .pot")
        self.assertEqual(sorted(set(pot) - self.ids), [], "run `make pot`: obsolete in the .pot")

    def test_hay_catalogos(self):
        self.assertIn("es", self.po)

    def test_todo_msgid_esta_traducido_y_no_sobra_nada(self):
        for lang, entries in self.po.items():
            with self.subTest(lang=lang):
                self.assertEqual(sorted(self.ids - set(entries)), [], "untranslated")
                self.assertEqual(sorted(set(entries) - self.ids), [], "obsolete")
                self.assertFalse([k for k, v in entries.items() if not v], "empty translations")

    def test_mismos_marcadores_de_formato(self):
        fields = lambda t: sorted(re.findall(r"\{[^{}]*\}", t))
        for lang, entries in self.po.items():
            for k, v in entries.items():
                with self.subTest(lang=lang, msgid=k):
                    self.assertEqual(fields(k), fields(v))

    def test_traduce_y_vuelve_a_ingles(self):
        base = os.path.join(HERE, "..", "locale")
        if not os.path.isdir(base):
            self.skipTest("run `make mo` first")
        try:
            self.assertEqual(c.set_language("es"), "es")
            self.assertEqual(c._("Quit"), "Salir")
            self.assertEqual(c.state_label("running"), "en marcha")
        finally:
            c.set_language("en")
        self.assertEqual(c._("Quit"), "Quit")


class LanguageChoice(unittest.TestCase):
    def pick(self, cli=None, env=None, saved=None, avail=("en", "es")):
        env = dict(env or {})
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(c, "available_languages", return_value=list(avail)), \
             mock.patch.object(c, "load_saved_lang", return_value=saved):
            return c.pick_language(cli)

    def test_prioridad(self):
        self.assertEqual(self.pick(cli="en", env={"CADDY_TUI_LANG": "es"}, saved="es"), "en")
        self.assertEqual(self.pick(env={"CADDY_TUI_LANG": "es"}, saved="en"), "es")
        self.assertEqual(self.pick(saved="es", env={"LANG": "en_US.UTF-8"}), "es")
        self.assertEqual(self.pick(env={"LANG": "es_ES.UTF-8"}), "es")

    def test_auto_ignora_lo_guardado(self):
        self.assertEqual(self.pick(cli="auto", saved="es", env={"LANG": "en_US.UTF-8"}), "en")

    def test_idioma_no_disponible_cae_a_ingles(self):
        self.assertEqual(self.pick(env={"LANG": "fr_FR.UTF-8"}), "en")
        self.assertEqual(self.pick(cli="fr"), "en")

    def test_locale_c_es_ingles(self):
        self.assertEqual(self.pick(env={"LANG": "C"}), "en")
        self.assertEqual(self.pick(env={"LANGUAGE": "es:en", "LANG": "C"}), "es")


if __name__ == "__main__":
    unittest.main()
