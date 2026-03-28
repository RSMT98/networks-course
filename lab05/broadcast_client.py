import socket
import sys
import argparse

BUFFER_SIZE = 4096

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8888)
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("", args.port))
except OSError as e:
    print(f"Bind error: {e}", file=sys.stderr)
    sys.exit(1)

print(f"Broadcast client is running and listening 0.0.0.0:{args.port}")
print("Waiting for broadcast messages...")
try:
    while True:
        data, addr = sock.recvfrom(BUFFER_SIZE)
        print(
            f"Received from {addr[0]}:{addr[1]} -> {data.decode("utf-8", errors="replace")}"
        )
except KeyboardInterrupt:
    pass
except OSError as e:
    print(f"Socket error: {e}", file=sys.stderr)
    sys.exit(1)
finally:
    sock.close()
