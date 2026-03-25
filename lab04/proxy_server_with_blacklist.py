from __future__ import annotations
import argparse
import hashlib
import html
import http.client
import json
import socket
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# https://www.rfc-editor.org/rfc/rfc9110.html#section-7.6.1-7
HOP_BY_HOP_HEADERS = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "te",
    "transfer-encoding",
    "upgrade",
}

LOG_PATH = Path("proxy_with_blacklist.log").resolve()


class DiskCache:
    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir.resolve()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._cache_dir / "index.json"
        self._lock = threading.Lock()
        self._meta_info_index: dict[str, dict] = {}

        if not self._index_path.exists():
            self._meta_info_index = {}
            return

        try:
            self._meta_info_index = json.loads(
                self._index_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            self._meta_info_index = {}

    def _save_index(self) -> None:
        tmp_path = self._index_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self._meta_info_index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._index_path)

    def _make_key(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def get(self, url: str) -> dict | None:
        key = self._make_key(url)
        with self._lock:
            meta_info = self._meta_info_index.get(key)
            if meta_info is None:
                return None

            body_path = self._cache_dir / meta_info["body_file"]
            if not body_path.exists():
                self._meta_info_index.pop(key, None)
                self._save_index()
                return None

            try:
                body = body_path.read_bytes()
            except OSError:
                return None

            entry = dict(meta_info)
            entry["body"] = body
            return entry

    def put(
        self,
        url: str,
        status_code: int,
        reason: str,
        response_headers: list[tuple[str, str]],
        response_body: bytes,
    ) -> None:
        key = self._make_key(url)
        body_file = f"{key}.bin"
        body_path = self._cache_dir / body_file
        body_path.write_bytes(response_body)

        entry = {
            "url": url,
            "status_code": status_code,
            "reason": reason,
            "headers": [[header, val] for header, val in response_headers],
            "etag": self._find_header(response_headers, "ETag"),
            "last_modified": self._find_header(response_headers, "Last-Modified"),
            "body_file": body_file,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self._lock:
            self._meta_info_index[key] = entry
            self._save_index()

    def refresh(self, url: str, new_headers: list[tuple[str, str]]) -> dict | None:
        key = self._make_key(url)
        with self._lock:
            meta_info = self._meta_info_index.get(key)
            if meta_info is None:
                return None

            old_headers: list[list[str]] | list[tuple[str, str]] = meta_info.get(
                "headers", []
            )
            new_header_names = {header.lower() for header, _ in new_headers}
            merged_headers: list[tuple[str, str]] = []
            for header, val in old_headers:
                if header.lower() in new_header_names:
                    continue
                header_lower = header.lower()
                if (
                    header_lower in HOP_BY_HOP_HEADERS
                    or header_lower == "content-length"
                ):
                    continue
                merged_headers.append((header, val))
            for header, val in new_headers:
                header_lower = header.lower()
                if (
                    header_lower in HOP_BY_HOP_HEADERS
                    or header_lower == "content-length"
                ):
                    continue
                merged_headers.append((header, val))

            meta_info["headers"] = [[header, val] for header, val in merged_headers]
            meta_info["etag"] = self._find_header(merged_headers, "ETag")
            meta_info["last_modified"] = self._find_header(
                merged_headers, "Last-Modified"
            )
            meta_info["cached_at"] = datetime.now().isoformat(timespec="seconds")
            self._save_index()

        return self.get(url)

    def _find_header(
        self,
        headers: list[tuple[str, str]] | list[list[str]],
        header_name: str,
    ) -> str | None:
        for header, val in headers:
            if header.lower() == header_name.lower():
                return val
        return None


class BlacklistConfig:
    def __init__(self, config_path: Path) -> None:
        config_path = config_path.resolve()
        if not config_path.exists():
            raise FileNotFoundError(
                f"Blacklist config file was not found: {config_path}"
            )

        try:
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Blacklist config file contains invalid JSON: {e}") from e

        blocked_domains = raw_config.get("blocked_domains", [])
        blocked_urls = raw_config.get("blocked_urls", [])
        if not isinstance(blocked_domains, list) or not isinstance(blocked_urls, list):
            raise ValueError(
                "Blacklist config must contain lists: 'blocked_domains' and 'blocked_urls'"
            )

        self._blocked_domains = {
            str(domain).strip().lower()
            for domain in blocked_domains
            if str(domain).strip()
        }
        self._blocked_urls = {
            self._normalize_url(str(url).strip())
            for url in blocked_urls
            if str(url).strip()
        }

    def is_blocked(self, url: str) -> bool:
        normalized_url = self._normalize_url(url)
        hostname = (urlparse(normalized_url).hostname or "").lower()
        if (
            hostname
            and any(
                hostname == blocked_domain or hostname.endswith(f".{blocked_domain}")
                for blocked_domain in self._blocked_domains
            )
        ) or (normalized_url in self._blocked_urls):
            return True

        return False

    def _normalize_url(self, url: str) -> str:
        stripped_url = url.strip()
        if stripped_url.startswith(("http://", "https://")):
            return stripped_url.rstrip("/")
        return f"http://{stripped_url}".rstrip("/")


CACHE = DiskCache(Path("proxy_cache"))
BLACKLIST = BlacklistConfig(Path("blacklist_config.json"))


class ProxyWithBlacklistHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle_proxy_request()

    def do_POST(self) -> None:
        self._handle_proxy_request()

    def do_CONNECT(self) -> None:
        self._send_error_response(501, "HTTPS is not supported")

    def do_PUT(self) -> None:
        self._send_error_response(405, "Only GET and POST requests are allowed")

    def do_DELETE(self) -> None:
        self._send_error_response(405, "Only GET and POST requests are allowed")

    def do_HEAD(self) -> None:
        self._send_error_response(405, "Only GET and POST requests are allowed")

    def _handle_proxy_request(self) -> None:
        target_url = self._extract_target_url()
        if not target_url:
            self._send_error_response(400, "Couldn't identify target URL")
            return

        parsed_url = urlparse(target_url)
        if parsed_url.scheme.lower() != "http":
            self._send_error_response(400, "Only http:// is supported", target_url)
            return

        if not parsed_url.hostname:
            self._send_error_response(400, "URL does not contain host name", target_url)
            return

        if BLACKLIST.is_blocked(target_url):
            body = (
                Path("blocked_by_blacklist.html")
                .read_text(encoding="utf-8")
                .replace("{{blocked_url}}", html.escape(target_url))
                .encode("utf-8")
            )
            self.send_response(403, "Forbidden")

            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("X-Proxy-Block", "DENIED")
            self.send_header("X-Proxy-Cache", "BYPASS")
            self.end_headers()

            self.wfile.write(body)
            self.wfile.flush()

            self._write_log(target_url, 403, extra_msg="BLOCKED BY BLACKLIST")
            return

        request_body = self._read_request_body()
        path = parsed_url.path or "/"
        if parsed_url.query:
            path += f"?{parsed_url.query}"

        headers: dict[str, str] = {}
        for key, val in self.headers.items():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            headers[key] = val

        host = parsed_url.hostname or ""
        if parsed_url.port and parsed_url.port != 80:
            host = f"{host}:{parsed_url.port}"
        headers["Host"] = host
        headers["Connection"] = "close"
        headers["Accept-Encoding"] = "identity"

        cached_entry: dict | None = None
        cache_status = "BYPASS"
        if self.command == "GET":
            cached_entry = CACHE.get(target_url)
            if cached_entry is None:
                cache_status = "MISS"
            else:
                cache_status = "REVALIDATE"
                if cached_entry.get("etag"):
                    headers["If-None-Match"] = cached_entry["etag"]
                if cached_entry.get("last_modified"):
                    headers["If-Modified-Since"] = cached_entry["last_modified"]

        port = parsed_url.port or 80
        conn: http.client.HTTPConnection | None = None
        try:
            conn = http.client.HTTPConnection(
                host=parsed_url.hostname,
                port=port,
                timeout=30.0,
            )
            conn.request(
                method=self.command,
                url=path,
                body=request_body,
                headers=headers,
            )
            resp = conn.getresponse()
            response_body = resp.read()
            raw_response_headers = resp.getheaders()
            response_headers: list[tuple[str, str]] = []
            for header, val in raw_response_headers:
                header_lower = header.lower()
                if (
                    header_lower in HOP_BY_HOP_HEADERS
                    or header_lower == "content-length"
                ):
                    continue
                response_headers.append((header, val))

            if (
                self.command == "GET"
                and cached_entry is not None
                and resp.status == 304
            ):
                refreshed_entry = CACHE.refresh(target_url, response_headers)
                entry = refreshed_entry or cached_entry
                status_code = int(entry.get("status_code", 200))
                reason = entry.get("reason", "OK")
                entry_headers: list[list[str]] = entry.get("headers", [])
                entry_response_body: bytes = entry.get("body", b"")
                self.send_response(status_code, reason)

                for header, val in entry_headers:
                    header_lower = header.lower()
                    if (
                        header_lower in HOP_BY_HOP_HEADERS
                        or header_lower == "content-length"
                    ):
                        continue
                    self.send_header(header, val)
                self.send_header("Content-Length", str(len(entry_response_body)))
                self.send_header("Connection", "close")
                self.send_header("X-Proxy-Block", "ALLOWED")
                self.send_header("X-Proxy-Cache", "HIT")
                self.end_headers()

                if entry_response_body:
                    self.wfile.write(entry_response_body)
                self.wfile.flush()

                self._write_log(
                    target_url,
                    entry.get("status_code", 200),
                    extra_msg="ALLOWED | CACHE HIT | validated by 304 Not Modified",
                )
                return

            if self.command == "GET" and resp.status == 200:
                CACHE.put(
                    url=target_url,
                    status_code=resp.status,
                    reason=resp.reason,
                    response_headers=response_headers,
                    response_body=response_body,
                )
                if cached_entry is None:
                    cache_status = "MISS"
                else:
                    cache_status = "UPDATED"

            self.send_response(resp.status, resp.reason)

            for header, val in response_headers:
                header_lower = header.lower()
                if (
                    header_lower in HOP_BY_HOP_HEADERS
                    or header_lower == "content-length"
                ):
                    continue
                self.send_header(header, val)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.send_header("X-Proxy-Block", "ALLOWED")
            self.send_header("X-Proxy-Cache", cache_status)
            self.end_headers()

            if response_body:
                self.wfile.write(response_body)
            self.wfile.flush()

            extra_blacklist_and_caching_msg = "ALLOWED"
            if self.command == "GET":
                if cache_status == "MISS":
                    extra_blacklist_and_caching_msg = (
                        "ALLOWED | CACHE MISS | stored on disk"
                    )
                elif cache_status == "UPDATED":
                    extra_blacklist_and_caching_msg = (
                        "ALLOWED | CACHE UPDATED | server returned a new version"
                    )
                elif cache_status == "REVALIDATE":
                    extra_blacklist_and_caching_msg = "ALLOWED | CACHE REVALIDATE | validators were sent, server returned full response"
                else:
                    extra_blacklist_and_caching_msg = f"ALLOWED | CACHE {cache_status}"

            self._write_log(
                target_url, resp.status, extra_msg=extra_blacklist_and_caching_msg
            )
        except socket.gaierror:
            self._send_error_response(
                502, f"Failed to resolve host name: {parsed_url.hostname}", target_url
            )
        except TimeoutError:
            self._send_error_response(
                504, f"Timeout when accessing {parsed_url.hostname}", target_url
            )
        except (ConnectionError, http.client.HTTPException) as e:
            self._send_error_response(
                502, f"Connection error with target server: {e}", target_url
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _extract_target_url(self) -> str | None:
        raw_path = self.path.strip()
        if not raw_path:
            return None

        if raw_path.startswith(("http://", "https://")):
            return raw_path

        if raw_path.startswith("/"):
            cand = raw_path[1:]
            if not cand:
                return None
            if cand.startswith(("http://", "https://")):
                return cand
            return f"http://{cand}"

        return None

    def _read_request_body(self) -> bytes | None:
        content_length = self.headers.get("Content-Length")
        if not content_length:
            return None

        try:
            length = int(content_length)
        except ValueError:
            return None

        if length <= 0:
            return None

        return self.rfile.read(length)

    def _send_error_response(
        self, status_code: int, msg: str, url: str | None = None
    ) -> None:
        body = (
            f"{status_code} {self.responses.get(status_code, ('Error',))[0]}\n"
            f"{msg}\n"
        ).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("X-Proxy-Block", "ERROR")
        self.send_header("X-Proxy-Cache", "ERROR")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self._write_log(url or self.path, status_code, extra_msg=msg)

    def _write_log(
        self, url: str, status_code: int, extra_msg: str | None = None
    ) -> None:
        log = f"[{datetime.now()}] {self.command} {url} -> {status_code}"
        if extra_msg:
            log += f" | {extra_msg}"
        log += "\n"
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(log)


parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8888)
args = parser.parse_args(sys.argv[1:])

PROXY_HOST = "127.0.0.1"
server = ThreadingHTTPServer((PROXY_HOST, args.port), ProxyWithBlacklistHandler)
print(f"Proxy server with blacklist is running and listening: {PROXY_HOST}:{args.port}")
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
