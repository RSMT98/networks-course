import argparse
import socket
import time

BUFFER_SIZE = 4096
TIMEOUT = 1

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(TIMEOUT)

print(f"UDP ping client is sending requests to {args.host}:{args.port}")
try:
    for i in range(1, 11):
        sent_time = time.time()
        msg = f"Ping {i} {sent_time:.6f}"
        print(f"Sending: {msg}")
        try:
            sock.sendto(msg.encode("utf-8"), (args.host, args.port))
            data, addr = sock.recvfrom(BUFFER_SIZE)
            rtt = time.time() - sent_time
            resp = data.decode("utf-8")
            print(f"Reply from {addr[0]}:{addr[1]}: {resp}")
            print(f"RTT = {rtt:.6f} seconds")
        except socket.timeout:
            print("Request timed out")
except KeyboardInterrupt:
    pass
finally:
    sock.close()
