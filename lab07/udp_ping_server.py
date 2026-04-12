import argparse
import random
import socket

BUFFER_SIZE = 4096

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((args.host, args.port))

print(f"UDP ping server is running on {args.host}:{args.port}")
try:
    while True:
        data, addr = sock.recvfrom(BUFFER_SIZE)
        msg = data.decode("utf-8")
        print(f"Received from {addr[0]}:{addr[1]} -> {msg}")

        if random.random() < 0.2:
            print(f"Packet loss for {addr[0]}:{addr[1]}")
            continue

        resp = msg.upper().encode("utf-8")
        sock.sendto(resp, addr)
        print(f"Sent to {addr[0]}:{addr[1]} <- {resp.decode('utf-8')}")
except KeyboardInterrupt:
    pass
finally:
    sock.close()
