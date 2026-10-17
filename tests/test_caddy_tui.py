import importlib.util
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
