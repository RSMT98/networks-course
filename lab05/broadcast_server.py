import socket
import sys
import time
import argparse
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8888)
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

print(f"Broadcast server is running on 255.255.255.255:{args.port}")
try:
    while True:
        msg = f"Current server time: {datetime.now()}"
        sock.sendto(msg.encode("utf-8"), ("255.255.255.255", args.port))
        print(f"Sent: '{msg}'")
        time.sleep(1)
except KeyboardInterrupt:
    pass
except OSError as e:
    print(f"Socket error: {e}", file=sys.stderr)
    sys.exit(1)
finally:
    sock.close()
