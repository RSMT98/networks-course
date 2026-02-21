import socket
import sys
import os
import mimetypes
from urllib.parse import unquote

if len(sys.argv) != 2:
    print("Usage: python server.py <server_port>")
    sys.exit(1)
server_port = int(sys.argv[1])
server_host = '127.0.0.1'
doc_root = os.path.abspath('.')
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server_socket.bind((server_host, server_port))
server_socket.listen(1)
print(f"The server is running on http://{server_host}:{server_port}")
try:
    while True:
        connection_socket, addr = server_socket.accept()
        try:
            request = connection_socket.recv(4096).decode('utf-8', errors='replace')
            if not request:
                connection_socket.close()
                continue
            first_line = request.split('\r\n', 1)[0]
            parts = first_line.split(' ')
            if len(parts) < 3:
                connection_socket.close()
                continue
            _, raw_path, _ = parts
            path = unquote(raw_path)
            if path == '/':
                path = '/hello.html'
            safe_path = os.path.normpath(os.path.join(doc_root, path.lstrip('/')))
            if not safe_path.startswith(doc_root) or not os.path.isfile(safe_path):
                raise FileNotFoundError
            with open(safe_path, 'rb') as f:
                body = f.read()
            content_type = mimetypes.guess_type(safe_path)[0] or 'application/octet-stream'
            headers = [
                "HTTP/1.1 200 OK",
                f"Content-Type: {content_type}",
                f"Content-Length: {len(body)}",
                "Connection: close",
            ]
            response_headers = "\r\n".join(headers) + "\r\n\r\n"
            connection_socket.sendall(response_headers.encode('utf-8'))
            connection_socket.sendall(body)
        except FileNotFoundError:
            body_html = (
                '<!DOCTYPE html>'
                '<html lang="ru">'
                '<head>'
                '  <meta charset="UTF-8">'
                '  <title>Error</title>'
                '</head>'
                '<body>'
                '  <h1>404 Not Found</h1>'
                '</body>'
                '</html>'
            )
            body = body_html.encode('utf-8')
            headers = [
                "HTTP/1.1 404 Not Found",
                "Content-Type: text/html; charset=utf-8",
                f"Content-Length: {len(body)}",
                "Connection: close",
            ]
            response_headers = "\r\n".join(headers) + "\r\n\r\n"
            connection_socket.sendall(response_headers.encode('utf-8'))
            connection_socket.sendall(body)
        except Exception as e:
            print(f"Error handling request from {addr}: {e}")
        finally:
            connection_socket.close()
except KeyboardInterrupt:
    pass
finally:
    server_socket.close()
