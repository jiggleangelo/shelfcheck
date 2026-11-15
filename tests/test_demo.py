import socket
import threading
import unittest
from urllib.parse import urlencode

from demo import BANNER, MAX_BODY, create_server


class HostedDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=b"", headers=None):
        headers = dict(headers or {})
        headers.setdefault("Host", f"127.0.0.1:{self.port}")
        headers.setdefault("Connection", "close")
        if method == "POST":
            headers.setdefault("Content-Length", str(len(body)))
        request = [f"{method} {path} HTTP/1.0"]
        request.extend(f"{name}: {value}" for name, value in headers.items())
        payload = ("\r\n".join(request) + "\r\n\r\n").encode("ascii") + body

        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout(10)
        self.assertEqual(0, connection.connect_ex(("127.0.0.1", self.port)))
        try:
            connection.sendall(payload)
            chunks = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            connection.close()
        raw = b"".join(chunks)
        head, response_body = raw.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split()[1])
        response_headers = {}
        for line in lines[1:]:
            name, value = line.split(":", 1)
            response_headers[name.lower()] = value.strip()
        return status, response_headers, response_body

    def test_health_and_complete_core_flow(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(200, status)
        self.assertEqual(b"ok", body)

        status, _, body = self.request("GET", "/")
        page = body.decode("utf-8")
        self.assertEqual(200, status)
        self.assertIn(BANNER, page)
        self.assertIn("Northfield Socks", page)
        self.assertIn("Generate my fake weekly report", page)

        submitted_brand = "Example <script>alert(1)</script>"
        encoded = urlencode({
            "brand": submitted_brand,
            "category": "fictional trail socks",
            "price_band": "under $30",
        }).encode("utf-8")
        status, headers, body = self.request(
            "POST",
            "/run",
            encoded,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(303, status)
        self.assertEqual(b"", body)
        self.assertTrue(headers["location"].startswith("/report?id="))

        status, _, body = self.request("GET", headers["location"])
        report = body.decode("utf-8")
        self.assertEqual(200, status)
        self.assertIn(BANNER, report)
        self.assertIn("500", report)
        self.assertIn("Shopping share of voice", report)
        self.assertIn("Agent traffic by confidence tier", report)
        self.assertIn("Shopify readiness", report)
        self.assertIn("Fake email preview", report)
        self.assertIn("95% interval", report)
        self.assertIn("Example &lt;script&gt;alert(1)&lt;/script&gt;", report)
        self.assertNotIn(submitted_brand, report)

    def test_errors_have_banner_and_large_bodies_are_rejected(self):
        status, _, body = self.request("GET", "/missing")
        self.assertEqual(404, status)
        self.assertIn(BANNER, body.decode("utf-8"))

        status, _, body = self.request(
            "POST",
            "/run",
            b"",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(MAX_BODY + 1),
            },
        )
        self.assertEqual(413, status)
        page = body.decode("utf-8")
        self.assertIn(BANNER, page)
        self.assertIn("64 KB", page)


if __name__ == "__main__":
    unittest.main()
