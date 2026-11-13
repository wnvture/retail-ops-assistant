import base64
import http.client
import json
import re
import socket
import subprocess
import sys
import threading
import time
import unittest


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process = subprocess.Popen([sys.executable, "-u", "-m", "retail_ops", "serve", "--port", "0", "--no-browser"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = []
        ready = threading.Event()

        def read_start():
            lines.append(cls.process.stdout.readline())
            ready.set()

        threading.Thread(target=read_start, daemon=True).start()
        if not ready.wait(10) or not lines or not re.search(r"127.0.0.1:(\d+)", lines[0]):
            cls.process.kill()
            raise RuntimeError("Local server failed to start")
        cls.port = int(re.search(r"127.0.0.1:(\d+)", lines[0]).group(1))
        status, headers, body = cls.request("GET", "/")
        cls.token = re.search(rb'name="local-token" content="([^"]+)"', body).group(1).decode()

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.communicate(timeout=10)

    @classmethod
    def request(cls, method, path, payload=None, extra=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Local-Token"] = token
        headers.update(extra or {})
        conn.request(method, path, body=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        response = conn.getresponse()
        status, result_headers, body = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return status, result_headers, body

    def test_static_assets_and_templates_available(self):
        for path in ("/", "/app.js", "/style.css", "/templates.zip"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn("Content-Security-Policy", headers)
                self.assertGreater(len(body), 20)

    def test_post_without_token_or_foreign_origin_rejected(self):
        self.assertEqual(self.request("POST", "/api/demo", {})[0], 403)
        self.assertEqual(self.request("POST", "/api/demo", {}, {"Origin": "https://example.com"}, self.token)[0], 403)
        self.assertEqual(self.request("GET", "/", extra={"Host": "attacker.example"})[0], 403)

    def test_demo_full_http_path(self):
        status, headers, body = self.request("POST", "/api/demo", {}, token=self.token)
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["summary"]["draft_ready"], 3)
        self.assertTrue(base64.b64decode(result["zip_base64"]).startswith(b"PK"))

    def test_validation_error_is_readable(self):
        status, _, body = self.request("POST", "/api/analyze", {}, token=self.token)
        self.assertEqual(status, 400)
        self.assertIn("Загрузите", json.loads(body)["error"])

    def test_estimate_http_path(self):
        status, _, body = self.request("POST", "/api/estimate", {"tasks": [{"minutes": 100, "fraction": 0.7}], "days": 20}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["saved_percent"], 70)
