"""Tests for gh-release-mirror. Run: python -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mirror  # noqa: E402


def _free_server(cfg):
    mirror.Handler.cfg = cfg
    httpd = mirror.ThreadingHTTPServer(("127.0.0.1", 0), mirror.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        d = os.path.join(self.tmp, "dragonkin__demo")
        os.makedirs(d)
        self.payload = b"hello-mirror-" * 1000
        with open(os.path.join(d, "app.bin"), "wb") as fh:
            fh.write(self.payload)
        manifest = {
            "repo": "dragonkin/demo",
            "tag": "v1.0.0",
            "assets": [{"repo": "dragonkin/demo", "name": "app.bin",
                        "size": len(self.payload),
                        "sha256": mirror.sha256_of(os.path.join(d, "app.bin"))}],
        }
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        self.cfg = {"repos": ["dragonkin/demo"], "cache_dir": self.tmp,
                    "host": "127.0.0.1", "port": 0, "base_url": None}
        self.httpd, self.base = _free_server(self.cfg)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestServing(Base):
    def _get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=10)

    def test_index_lists_asset(self):
        body = self._get("/").read().decode()
        self.assertIn("app.bin", body)
        self.assertIn("dragonkin/demo", body)

    def test_api(self):
        data = json.loads(self._get("/api/releases").read())
        self.assertEqual(data[0]["tag"], "v1.0.0")

    def test_download_full(self):
        resp = self._get("/files/dragonkin__demo/app.bin")
        self.assertEqual(resp.read(), self.payload)
        self.assertEqual(resp.headers["Accept-Ranges"], "bytes")

    def test_range(self):
        req = urllib.request.Request(self.base + "/files/dragonkin__demo/app.bin",
                                     headers={"Range": "bytes=0-9"})
        resp = urllib.request.urlopen(req, timeout=10)
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.read(), self.payload[:10])

    def test_traversal_blocked(self):
        try:
            urllib.request.urlopen(self.base + "/files/../etc/passwd", timeout=10)
            self.fail("traversal should be rejected")
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, (400, 404))

    def test_health(self):
        self.assertEqual(self._get("/healthz").read().strip(), b"ok")


class TestConfig(unittest.TestCase):
    def _write(self, obj):
        fd, p = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return p

    def test_defaults(self):
        p = self._write({"repos": ["a/b"]})
        cfg = mirror.load_config(p)
        self.assertEqual(cfg["port"], 8080)
        self.assertEqual(cfg["cache_dir"], "./cache")

    def test_bad_repo(self):
        p = self._write({"repos": ["noslash"]})
        with self.assertRaises(ValueError):
            mirror.load_config(p)

    def test_safe_name(self):
        self.assertEqual(mirror._safe_name("../../evil.sh"), "evil.sh")
        self.assertEqual(mirror._safe_name("a b/c d.zip"), "c_d.zip")


if __name__ == "__main__":
    unittest.main()
