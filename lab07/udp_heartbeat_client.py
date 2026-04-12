import argparse
import socket
import time

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--count", type=int, default=10)
parser.add_argument("--interval", type=float, default=1.0)
args = parser.parse_args()
if args.count <= 0:
    parser.error("--count must be greater than 0")
if args.interval < 0:
    parser.error("--interval must be greater than or equal to 0")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.connect((args.host, args.port))
client_host, client_port = sock.getsockname()

print(f"UDP heartbeat client {client_host}:{client_port} is sending packets to {args.host}:{args.port}")
print(f"Parameters: count={args.count}, interval={args.interval:.1f}s")
try:
    for i in range(1, args.count + 1):
        sent_time = time.time()
        msg = f"Heartbeat {i} {sent_time:.6f}"
        print(f"Sending: {msg}")
        sock.send(msg.encode("utf-8"))
        if i < args.count and args.interval > 0:
            time.sleep(args.interval)
except KeyboardInterrupt:
    pass
finally:
    sock.close()
