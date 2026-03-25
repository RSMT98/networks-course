import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ServerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, dict_json: dict) -> None:
        body = json.dumps(dict_json, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/":
            html = Path("index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/get":
            self._send_json(
                {"message": "GET request is working successfully!", "path": self.path}
            )
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/post":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b""
            self._send_json(
                {
                    "message": "POST request is working successfully!",
                    "path": self.path,
                    "received_body": body.decode("utf-8", errors="replace"),
                    "content_type": self.headers.get("Content-Type"),
                }
            )
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args(sys.argv[1:])

SERVER_HOST = "127.0.0.1"
server = ThreadingHTTPServer((SERVER_HOST, args.port), ServerHandler)
print(f"The server is running on http://{SERVER_HOST}:{args.port}")
print("Endpoints: /, /get, /post")
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
