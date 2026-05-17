import argparse
import socket
import sys

BUFFER_SIZE = 4096
MIN_PORT = 1
MAX_PORT = 65535

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="::1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--timeout", type=float, default=5.0)
parser.add_argument("message", nargs="+")
args = parser.parse_args()

if not MIN_PORT <= args.port <= MAX_PORT:
    parser.error(f"--port must be in range {MIN_PORT}..{MAX_PORT}")
if args.timeout <= 0:
    parser.error("--timeout must be greater than 0")

msg = " ".join(args.message)
try:
    addr_info = socket.getaddrinfo(
        args.host,
        args.port,
        socket.AF_INET6,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
    )
except socket.gaierror:
    print(f"Could not resolve IPv6 host {args.host}.", file=sys.stderr)
    raise

sock = None
server_addr = None
last_error = None
for family, socket_type, proto, _, addr in addr_info:
    cur_sock = socket.socket(family, socket_type, proto)
    cur_sock.settimeout(args.timeout)
    try:
        cur_sock.connect(addr)
    except OSError as e:
        last_error = e
        cur_sock.close()
        continue

    sock = cur_sock
    server_addr = addr
    break

if sock is None or server_addr is None:
    print(
        f"Could not connect to [{args.host}]:{args.port}: {last_error}",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    print(f"Connected to [{server_addr[0]}]:{server_addr[1]}")

    sock.sendall(msg.encode("utf-8"))
    sock.shutdown(socket.SHUT_WR)
    print(f"Sent: {msg}")

    chunks = []
    while True:
        data = sock.recv(BUFFER_SIZE)
        if not data:
            break

        chunks.append(data)

    resp = b"".join(chunks).decode("utf-8", errors="replace")
    print(f"Received: {resp}")
finally:
    sock.close()
