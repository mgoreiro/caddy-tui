import ast
import importlib.util
import json
import os
import re
import unittest
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
