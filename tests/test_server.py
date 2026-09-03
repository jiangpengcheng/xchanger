#!/usr/bin/env python3

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import ReverseProxy, Store, make_handler, parse_range  # noqa: E402


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.received.append((self.command, self.path, body))  # type: ignore[attr-defined]
        response = b'{"accepted":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "data").mkdir()
        (root / "apks").mkdir()
        (root / "apks" / "demo.apk").write_bytes(b"0123456789")
        (root / "data" / "apps.json").write_text(
            json.dumps(
                {
                    "apps": [
                        {
                            "productId": "900001",
                            "name": "Demo",
                            "packageName": "example.demo",
                            "versionCode": 7,
                            "versionName": "1.0",
                            "apk": "demo.apk",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = Store(root, "http://api.xchanger.cn")
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream.received = []  # type: ignore[attr-defined]
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        self.proxy_records: list[dict] = []
        self.intercept_records: list[dict] = []
        proxy = ReverseProxy(
            f"http://127.0.0.1:{self.upstream.server_port}",
            logger=self.proxy_records.append,
        )
        static_proxy = ReverseProxy(
            f"http://127.0.0.1:{self.upstream.server_port}",
            logger=self.proxy_records.append,
        )
        handler = make_handler(
            store,
            proxy,
            static_proxy,
            intercepted_apk="demo.apk",
            logger=self.intercept_records.append,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=2)
        self.temp.cleanup()

    def get_json(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response)

    def test_catalog_and_detail(self) -> None:
        catalog = self.get_json("/api/v1/product?search=Demo")
        self.assertEqual(catalog["products"][0]["name"], "Demo")
        detail = self.get_json("/api/v1/product/900001")
        attrs = detail["attributeGroups"]
        self.assertEqual(attrs["AppPackageName"], "example.demo")
        self.assertEqual(attrs["AppSrc"], "http://api.xchanger.cn/files/demo.apk")

    def test_version_endpoint(self) -> None:
        versions = self.get_json("/api/v1/app/version?packages=example.demo")
        self.assertEqual(versions["apps"][0]["versionCode"], "7")

    def test_empty_hot_search_history(self) -> None:
        history = self.get_json("/api/v1/product/history?type=hot")
        self.assertEqual(history, {"histories": []})

    def test_range_download(self) -> None:
        request = urllib.request.Request(
            self.base + "/files/demo.apk", headers={"Range": "bytes=2-5"}
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")

    def test_all_gstore_apk_paths_serve_intercepted_apk(self) -> None:
        request = urllib.request.Request(
            self.base + "/catalog/original-app.apk?download=1",
            headers={"Host": "gstore-static.xchanger.cn", "Range": "bytes=4-7"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"4567")

        self.assertEqual(len(self.intercept_records), 1)
        record = self.intercept_records[0]
        self.assertEqual(record["path"], "/catalog/original-app.apk?download=1")
        self.assertEqual(record["servedApk"], "demo.apk")

    def test_range_parser(self) -> None:
        self.assertEqual(parse_range("bytes=-3", 10).start, 7)
        self.assertEqual(parse_range("bytes=4-", 10).end, 9)

    def test_non_store_request_is_proxied_and_bodies_are_logged(self) -> None:
        body = b'{"currentVersion":"1.0"}'
        request = urllib.request.Request(
            self.base + "/fota/v2/availbleVersion",
            data=body,
            headers={"Content-Type": "application/json", "X-Test-Token": "visible"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"accepted": True})

        received = self.upstream.received  # type: ignore[attr-defined]
        self.assertEqual(received, [("POST", "/fota/v2/availbleVersion", body)])
        self.assertEqual(len(self.proxy_records), 1)
        record = self.proxy_records[0]
        self.assertEqual(record["requestBody"], body.decode())
        self.assertEqual(record["responseBody"], '{"accepted":true}')
        self.assertEqual(record["requestHeaders"]["X-Test-Token"], "visible")


if __name__ == "__main__":
    unittest.main()
