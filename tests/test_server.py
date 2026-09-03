#!/usr/bin/env python3

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import Store, make_handler, parse_range  # noqa: E402


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
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
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

    def test_range_parser(self) -> None:
        self.assertEqual(parse_range("bytes=-3", 10).start, 7)
        self.assertEqual(parse_range("bytes=4-", 10).end, 9)


if __name__ == "__main__":
    unittest.main()
