#!/usr/bin/env python3
"""Minimal XCAppStore-compatible HTTP server."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import mimetypes
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
MAX_PROXY_REQUEST_BODY = 16 * 1024 * 1024


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int


def _body_for_log(body: bytes) -> tuple[str, str]:
    try:
        return "utf-8", body.decode("utf-8")
    except UnicodeDecodeError:
        return "base64", base64.b64encode(body).decode("ascii")


def _default_proxy_logger(record: dict[str, Any]) -> None:
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)


class ReverseProxy:
    """Small, explicit reverse proxy for non-store API routes."""

    def __init__(
        self,
        upstream_base_url: str,
        *,
        timeout: float = 20,
        logger: Callable[[dict[str, Any]], None] = _default_proxy_logger,
    ):
        parsed = urlparse(upstream_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("upstream base URL must use http or https")
        self.scheme = parsed.scheme
        self.hostname = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.host_header = parsed.netloc
        self.base_path = parsed.path.rstrip("/")
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.timeout = timeout
        self.logger = logger

    def forward(self, handler: BaseHTTPRequestHandler, *, send_body: bool) -> None:
        request_body = b""
        request_encoding = "utf-8"
        request_body_log = ""
        connection: http.client.HTTPConnection | None = None
        try:
            transfer_encoding = handler.headers.get("Transfer-Encoding", "").lower()
            if transfer_encoding and transfer_encoding != "identity":
                self._error(handler, HTTPStatus.NOT_IMPLEMENTED, "Chunked requests are not supported")
                return

            raw_length = handler.headers.get("Content-Length", "0")
            try:
                content_length = int(raw_length)
            except ValueError:
                self._error(handler, HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
                return
            if content_length < 0:
                self._error(handler, HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
                return
            if content_length > MAX_PROXY_REQUEST_BODY:
                self._error(handler, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
                return
            if content_length:
                request_body = handler.rfile.read(content_length)
            request_encoding, request_body_log = _body_for_log(request_body)

            request_headers = {
                name: value
                for name, value in handler.headers.items()
                if name.lower() not in HOP_BY_HOP_HEADERS
                and name.lower() not in {"host", "content-length", "accept-encoding"}
            }
            request_headers["Host"] = self.host_header
            request_headers["Accept-Encoding"] = "identity"
            if request_body:
                request_headers["Content-Length"] = str(len(request_body))

            upstream_path = f"{self.base_path}{handler.path}"
            connection_class = (
                http.client.HTTPSConnection
                if self.scheme == "https"
                else http.client.HTTPConnection
            )
            connection_args: dict[str, Any] = {"timeout": self.timeout}
            if self.scheme == "https":
                connection_args["context"] = ssl.create_default_context()
            connection = connection_class(self.hostname, self.port, **connection_args)
            connection.request(
                handler.command,
                upstream_path,
                body=request_body or None,
                headers=request_headers,
            )
            upstream_response = connection.getresponse()
            response_body = upstream_response.read()
            response_encoding, response_body_log = _body_for_log(response_body)
            response_headers = [
                (name, value)
                for name, value in upstream_response.getheaders()
                if name.lower() not in HOP_BY_HOP_HEADERS
                and name.lower() != "content-length"
            ]

            self.logger(
                {
                    "event": "upstream_proxy",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "clientIp": handler.client_address[0],
                    "upstream": self.upstream_base_url,
                    "method": handler.command,
                    "path": handler.path,
                    "requestHeaders": dict(handler.headers.items()),
                    "requestBodyEncoding": request_encoding,
                    "requestBody": request_body_log,
                    "responseStatus": upstream_response.status,
                    "responseHeaders": dict(upstream_response.getheaders()),
                    "responseBodyEncoding": response_encoding,
                    "responseBody": response_body_log,
                }
            )

            handler.send_response(upstream_response.status, upstream_response.reason)
            for name, value in response_headers:
                handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(response_body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            if send_body:
                handler.wfile.write(response_body)
        except Exception as exc:
            self.logger(
                {
                    "event": "upstream_proxy_error",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "clientIp": handler.client_address[0],
                    "upstream": self.upstream_base_url,
                    "method": handler.command,
                    "path": handler.path,
                    "requestHeaders": dict(handler.headers.items()),
                    "requestBodyEncoding": request_encoding,
                    "requestBody": request_body_log,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                }
            )
            self._error(handler, HTTPStatus.BAD_GATEWAY, "Upstream request failed")
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _error(
        handler: BaseHTTPRequestHandler, status: HTTPStatus, message: str
    ) -> None:
        body = json.dumps(
            {"error": {"code": str(status.value), "message": message}},
            separators=(",", ":"),
        ).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)


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


def make_handler(
    store: Store,
    proxy: ReverseProxy | None = None,
    static_proxy: ReverseProxy | None = None,
    intercepted_apk: str | None = None,
    logger: Callable[[dict[str, Any]], None] = _default_proxy_logger,
) -> type[BaseHTTPRequestHandler]:
    intercepted_apk_path = store.apk_path(intercepted_apk) if intercepted_apk else None
    if intercepted_apk and intercepted_apk_path is None:
        raise ValueError(f"intercepted APK is not registered or missing: {intercepted_apk}")

    class Handler(BaseHTTPRequestHandler):
        server_version = "XCAppStoreLocal/1.0"

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch(send_body=False)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(send_body=True)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch(send_body=True)

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch(send_body=True)

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch(send_body=True)

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch(send_body=True)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._dispatch(send_body=True)

        def _dispatch(self, send_body: bool) -> None:
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            is_read = self.command in {"GET", "HEAD"}

            request_host = self.headers.get("Host", "").split(":", 1)[0].rstrip(".").lower()
            if request_host == "gstore-static.xchanger.cn":
                if is_read and route.lower().endswith(".apk") and intercepted_apk_path:
                    logger(
                        {
                            "event": "apk_intercept",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "clientIp": self.client_address[0],
                            "host": request_host,
                            "method": self.command,
                            "path": self.path,
                            "range": self.headers.get("Range", ""),
                            "servedApk": intercepted_apk_path.name,
                        }
                    )
                    self._serve_file(intercepted_apk_path, send_body)
                    return
                if static_proxy is not None:
                    static_proxy.forward(self, send_body=send_body)
                    return
                self._json(
                    {"error": {"code": "404", "message": "Static route not found"}},
                    status=HTTPStatus.NOT_FOUND,
                    send_body=send_body,
                )
                return

            if is_read and route == "/healthz":
                self._json({"status": "ok"}, send_body=send_body)
                return
            if is_read and route == "/api/v1/product/catalog":
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
            if is_read and route == "/api/v1/product/special":
                self._json(
                    {"specials": [{"alias": "local", "name": "本地应用"}]},
                    send_body=send_body,
                )
                return
            if is_read and route == "/api/v1/product/special/local":
                self._json(store.products(), send_body=send_body)
                return
            if is_read and route == "/api/v1/banner/index":
                self._json({"banners": []}, send_body=send_body)
                return
            if is_read and route == "/api/v1/app/version":
                self._json(store.versions(), send_body=send_body)
                return
            if is_read and route == "/api/v1/product/history":
                self._json({"histories": []}, send_body=send_body)
                return
            if is_read and route == "/api/v1/product":
                search = query.get("search", [""])[0]
                self._json(store.products(search), send_body=send_body)
                return
            if is_read and route.startswith("/api/v1/product/"):
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
            if is_read and route.startswith("/files/"):
                self._file(route.removeprefix("/files/"), send_body)
                return
            if proxy is not None:
                proxy.forward(self, send_body=send_body)
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

            self._serve_file(path, send_body)

        def _serve_file(self, path: Path, send_body: bool) -> None:
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
        "--upstream-base-url",
        help="Proxy non-store routes to this upstream base URL",
    )
    parser.add_argument(
        "--static-upstream-base-url",
        help="Proxy non-APK gstore-static.xchanger.cn requests to this URL",
    )
    parser.add_argument(
        "--intercept-apk",
        help="Registered APK filename returned for every gstore-static .apk request",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    store = Store(args.root, args.public_base_url)
    proxy = ReverseProxy(args.upstream_base_url) if args.upstream_base_url else None
    static_proxy = (
        ReverseProxy(args.static_upstream_base_url)
        if args.static_upstream_base_url
        else None
    )
    handler = make_handler(
        store,
        proxy,
        static_proxy,
        intercepted_apk=args.intercept_apk,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving XCAppStore API on http://{args.host}:{args.port}")
    print(f"URLs returned to the car use {args.public_base_url}")
    if proxy:
        print(f"Non-store routes proxy to {proxy.upstream_base_url}")
    if static_proxy:
        print(f"Non-APK static routes proxy to {static_proxy.upstream_base_url}")
    if args.intercept_apk:
        print(f"All gstore-static .apk requests serve {args.intercept_apk}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
