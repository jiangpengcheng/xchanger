#!/usr/bin/env python3
"""Minimal XCAppStore-compatible HTTP server."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int


class Store:
    def __init__(self, root: Path, public_base_url: str):
        self.root = root.resolve()
        self.data_file = self.root / "data" / "apps.json"
        self.apks_dir = (self.root / "apks").resolve()
        self.public_base_url = public_base_url.rstrip("/")

    def apps(self) -> list[dict[str, Any]]:
        with self.data_file.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        apps = payload.get("apps")
        if not isinstance(apps, list):
            raise ValueError(f"{self.data_file} must contain an apps array")
        return apps

    def find(self, product_id: str) -> dict[str, Any] | None:
        return next(
            (app for app in self.apps() if str(app["productId"]) == product_id),
            None,
        )

    def apk_path(self, filename: str) -> Path | None:
        decoded = unquote(filename)
        registered = {
            str(app["apk"]): app for app in self.apps() if isinstance(app.get("apk"), str)
        }
        if decoded not in registered:
            return None
        candidate = (self.apks_dir / decoded).resolve()
        if candidate.parent != self.apks_dir or not candidate.is_file():
            return None
        return candidate

    def download_url(self, app: dict[str, Any]) -> str:
        return f"{self.public_base_url}/files/{quote(str(app['apk']))}"

    def image_url(self, app: dict[str, Any]) -> str:
        icon = app.get("icon")
        if not icon:
            return ""
        return f"{self.public_base_url}/assets/{quote(str(icon))}"

    def product(self, app: dict[str, Any]) -> dict[str, Any]:
        apk_path = self.apk_path(str(app["apk"]))
        size = apk_path.stat().st_size if apk_path else 0
        size_mb = max(size / (1024 * 1024), 0.01)
        source = self.download_url(app)
        return {
            "name": app["name"],
            "productId": str(app["productId"]),
            "infoKeyword": "",
            "infoDescription": app.get("description", ""),
            "image": self.image_url(app),
            "images": [],
            "price": "￥0.00",
            "attributeGroups": {
                "AppPackageName": app["packageName"],
                "AppSrc": source,
                "AppVersionCode": str(app["versionCode"]),
                "AppVersionName": str(app.get("versionName", "")),
                "AppSize": f"{size_mb:.2f}M",
                "AppDeveloper": app.get("developer", "Local"),
                "AppUptime": app.get("updateTime", ""),
                "AppUpdetail": app.get("changes", ""),
                "AppPath": source,
                "AppDownnums": "0",
                # This APK deserializes isNotFree, while the live API currently
                # returns isFree. Supplying both keeps old and new clients happy.
                "isNotFree": "1",
                "isFree": "1",
            },
        }

    def products(self, query: str = "") -> dict[str, Any]:
        needle = query.casefold().strip()
        apps = self.apps()
        if needle:
            apps = [
                app
                for app in apps
                if needle in str(app["name"]).casefold()
                or needle in str(app["packageName"]).casefold()
            ]
        return {
            "products": [self.product(app) for app in apps],
            "link": {
                "total": str(len(apps)),
                "page": "1",
                "current": "1",
                "limit": "100",
            },
            "title": "本地应用",
            "categoryId": "local",
        }

    def versions(self) -> dict[str, Any]:
        result = []
        for app in self.apps():
            product = self.product(app)
            attrs = product["attributeGroups"]
            result.append(
                {
                    "name": app["name"],
                    "productId": str(app["productId"]),
                    "packageName": app["packageName"],
                    "versionCode": str(app["versionCode"]),
                    "versionName": str(app.get("versionName", "")),
                    "size": attrs["AppSize"],
                    "path": attrs["AppSrc"],
                    "image": product["image"],
                    "isFree": "1",
                    "silent": "0",
                }
            )
        return {"apps": result}


def parse_range(value: str | None, size: int) -> ByteRange | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or size <= 0:
        raise ValueError("invalid range")
    first, last = match.groups()
    if not first:
        length = int(last)
        if length <= 0:
            raise ValueError("invalid suffix range")
        return ByteRange(max(0, size - length), size - 1)
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return ByteRange(start, min(end, size - 1))


def make_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "XCAppStoreLocal/1.0"

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch(send_body=False)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(send_body=True)

        def _dispatch(self, send_body: bool) -> None:
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if route == "/healthz":
                self._json({"status": "ok"}, send_body=send_body)
                return
            if route == "/api/v1/product/catalog":
                self._json(
                    {
                        "products": [
                            {
                                "categoryId": "local",
                                "name": "本地应用",
                                "image": "",
                            }
                        ]
                    },
                    send_body=send_body,
                )
                return
            if route == "/api/v1/product/special":
                self._json(
                    {"specials": [{"alias": "local", "name": "本地应用"}]},
                    send_body=send_body,
                )
                return
            if route == "/api/v1/product/special/local":
                self._json(store.products(), send_body=send_body)
                return
            if route == "/api/v1/banner/index":
                self._json({"banners": []}, send_body=send_body)
                return
            if route == "/api/v1/app/version":
                self._json(store.versions(), send_body=send_body)
                return
            if route == "/api/v1/product/history":
                self._json({"histories": []}, send_body=send_body)
                return
            if route == "/api/v1/product":
                search = query.get("search", [""])[0]
                self._json(store.products(search), send_body=send_body)
                return
            if route.startswith("/api/v1/product/"):
                product_id = route.rsplit("/", 1)[-1]
                app = store.find(product_id)
                if app:
                    self._json(store.product(app), send_body=send_body)
                else:
                    self._json(
                        {"error": {"code": "404", "message": "Product not found"}},
                        send_body=send_body,
                    )
                return
            if route.startswith("/files/"):
                self._file(route.removeprefix("/files/"), send_body)
                return
            self._json(
                {"error": {"code": "404", "message": "Route not found"}},
                status=HTTPStatus.NOT_FOUND,
                send_body=send_body,
            )

        def _json(
            self,
            payload: dict[str, Any],
            *,
            status: HTTPStatus = HTTPStatus.OK,
            send_body: bool,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _file(self, filename: str, send_body: bool) -> None:
            path = store.apk_path(filename)
            if path is None:
                self._json(
                    {"error": {"code": "404", "message": "APK not found"}},
                    status=HTTPStatus.NOT_FOUND,
                    send_body=send_body,
                )
                return

            size = path.stat().st_size
            try:
                requested = parse_range(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

            start, end = (0, size - 1) if requested is None else (requested.start, requested.end)
            length = end - start + 1
            self.send_response(HTTPStatus.OK if requested is None else HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if requested is not None:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not send_body:
                return

            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.client_address[0]} - {fmt % args}")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--public-base-url",
        default="http://api.xchanger.cn",
        help="Base URL inserted into APK and image responses",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    store = Store(args.root, args.public_base_url)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    print(f"Serving XCAppStore API on http://{args.host}:{args.port}")
    print(f"URLs returned to the car use {args.public_base_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
