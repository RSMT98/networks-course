import argparse
import socket

BUFFER_SIZE = 4096
MIN_PORT = 1
MAX_PORT = 65535

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="::1")
parser.add_argument("--port", type=int, default=8888)
args = parser.parse_args()

if not MIN_PORT <= args.port <= MAX_PORT:
    parser.error(f"--port must be in range {MIN_PORT}..{MAX_PORT}")

sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)

sock.bind((args.host, args.port))
sock.listen()

print(f"IPv6 TCP echo server is running on [{args.host}]:{args.port}")
try:
    while True:
        conn, addr = sock.accept()
        print(f"Accepted connection from [{addr[0]}]:{addr[1]}")

        with conn:
            chunks = []
            while True:
                data = conn.recv(BUFFER_SIZE)
                if not data:
                    break

                chunks.append(data)

            msg = b"".join(chunks).decode("utf-8", errors="replace")
            resp = msg.upper()
            conn.sendall(resp.encode("utf-8"))
            print(f"Received from [{addr[0]}]:{addr[1]} -> {msg}")
            print(f"Sent to [{addr[0]}]:{addr[1]} <- {resp}")
except KeyboardInterrupt:
    pass
finally:
    sock.close()
