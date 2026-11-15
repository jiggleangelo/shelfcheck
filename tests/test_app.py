import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import NOTICE, create_server


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = "https://app-shelfcheck.manykiln.com"
        self.server = create_server("127.0.0.1", 0, self.tmp.name, self.base)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.tmp.cleanup()

    def request(self, method, path, body=b"", content_type="application/x-www-form-urlencoded", origin=True):
        headers = {"Host":"app-shelfcheck.manykiln.com", "Connection":"close"}
        if method == "POST":
            headers.update({"Content-Length":str(len(body)), "Content-Type":content_type})
            if origin:
                headers["Origin"] = self.base
        lines = [f"{method} {path} HTTP/1.0"] + [f"{k}: {v}" for k,v in headers.items()]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.assertEqual(0, sock.connect_ex(("127.0.0.1", self.port)))
        sock.sendall(raw)
        chunks = []
        while True:
            value = sock.recv(65536)
            if not value:
                break
            chunks.append(value)
        sock.close()
        head, response = b"".join(chunks).split(b"\r\n\r\n", 1)
        header_lines = head.decode("iso-8859-1").split("\r\n")
        headers = {}
        for line in header_lines[1:]:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
        return int(header_lines[0].split()[1]), headers, response

    def start(self, sample=False):
        status, headers, _ = self.request("POST", "/sample" if sample else "/start")
        self.assertEqual(303, status)
        return headers["location"].split("/")[2]

    def test_home_health_workspace_export_delete_and_security(self):
        self.assertEqual((200, b"ok"), (lambda x:(x[0],x[2]))(self.request("GET", "/healthz")))
        status, _, body = self.request("GET", "/")
        page = body.decode()
        self.assertEqual(200, status)
        self.assertIn(NOTICE, page)
        self.assertIn("https://manykiln.com/privacy", page)
        self.assertIn("does not query ChatGPT", page)
        token = self.start()
        self.assertIn("only key", self.request("GET", f"/w/{token}")[2].decode())
        profile = urlencode({"name":"Northfield Socks","domain":"northfield.example","category":"socks","price_band":"mid-range"}).encode()
        self.assertEqual(303, self.request("POST", f"/w/{token}/profile", profile)[0])
        status, _, exported = self.request("GET", f"/w/{token}/export")
        self.assertEqual(200, status)
        self.assertEqual("Northfield Socks", json.loads(exported)["profile"]["name"])
        disk_names = [x.name for x in Path(self.tmp.name).rglob("*")]
        self.assertNotIn(token, disk_names)
        self.assertEqual(403, self.request("POST", f"/w/{token}/settings", b"", origin=False)[0])
        self.assertEqual(303, self.request("POST", f"/w/{token}/delete")[0])
        self.assertEqual(404, self.request("GET", f"/w/{token}")[0])

    def test_sample_has_banner_snapshots_and_diff(self):
        token = self.start(True)
        page = self.request("GET", f"/w/{token}")[2].decode()
        self.assertIn("SAMPLE DATA", page)
        self.assertIn("sample-2", page)
        report = self.request("GET", f"/w/{token}/r/sample-2")[2].decode()
        self.assertIn("Changes since", report)

    def test_limits_host_and_origin(self):
        self.assertEqual(413, self.request("POST", "/start", b"x" * (1024 * 1024 + 1))[0])
        raw = b"GET / HTTP/1.0\r\nHost: bad.example\r\nConnection: close\r\n\r\n"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.assertEqual(0, sock.connect_ex(("127.0.0.1", self.port)))
        sock.sendall(raw)
        response = sock.recv(4096)
        sock.close()
        self.assertIn(b" 400 ", response)


if __name__ == "__main__":
    unittest.main()
