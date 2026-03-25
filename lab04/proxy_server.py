import argparse
import http.client
import socket
import sys
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

LOG_PATH = Path("proxy.log").resolve()


class ProxyHandler(BaseHTTPRequestHandler):
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
            self._send_error_response(400, "Only http:// is supported")
            return

        if not parsed_url.hostname:
            self._send_error_response(400, "URL does not contain host name")
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
            self.send_response(resp.status, resp.reason)

            for header, val in resp.getheaders():
                header_lower = header.lower()
                if header_lower in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(header, val)
            self.send_header("Connection", "close")
            self.end_headers()

            if response_body:
                self.wfile.write(response_body)
            self.wfile.flush()

            self._write_log(target_url, resp.status)
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
server = ThreadingHTTPServer((PROXY_HOST, args.port), ProxyHandler)
print(f"Proxy server is running and listening: {PROXY_HOST}:{args.port}")
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
