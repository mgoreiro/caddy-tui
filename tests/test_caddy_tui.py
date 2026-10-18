import argparse
import ast
import contextlib
import io
import importlib.util
import json
import os
import re
import stat
import tempfile
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

    def test_cert_covers(self):
        self.assertTrue(c.cert_covers(["a.example.com"], "a.example.com"))
        self.assertTrue(c.cert_covers(["*.example.com"], "a.example.com"))
        self.assertFalse(c.cert_covers(["*.example.com"], "a.b.example.com"))
        self.assertFalse(c.cert_covers(["a.example.com"], "b.example.com"))

    def test_site_domains(self):
        text = ("{$DOMAIN}:8443 {\n}\nhttp://plain.example.com {\n}\nlocalhost {\n}\nnas.lan {\n}\n"
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
        code, out = self.run_setup(["", "", "y"])
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

    def test_sin_systemd_usa_cron(self):
        with mock.patch.object(c, "_systemd_available", return_value=False):
            self.run_setup(["", "", "y"])
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
        self.run_setup(["", "", "y"])
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


class Translations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ids = source_msgids()
        cls.po = {f[:-3]: read_po(os.path.join(HERE, "..", "po", f))
                  for f in os.listdir(os.path.join(HERE, "..", "po")) if f.endswith(".po")}

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
