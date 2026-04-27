import argparse
import socket
from pathlib import Path

from stop_and_wait_protocol import StopAndWaitProtocol

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--file", required=True)
parser.add_argument("--chunk-size", type=int, default=512)
parser.add_argument("--timeout", type=float, default=0.3)
parser.add_argument("--loss-rate", type=float, default=0.3)
parser.add_argument("--corrupt-rate", type=float, default=0.0)
parser.add_argument("--max-retries", type=int, default=30)
args = parser.parse_args()

server_addr = (args.host, args.port)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    protocol = StopAndWaitProtocol(
        sock,
        timeout=args.timeout,
        loss_rate=args.loss_rate,
        corrupt_rate=args.corrupt_rate,
        max_retries=args.max_retries,
    )
    print(f"Stop-and-Wait client is sending file to {args.host}:{args.port}")
    print(
        f"Parameters: chunk_size={args.chunk_size}, loss_rate={args.loss_rate:.2f}, corrupt_rate={args.corrupt_rate:.2f}, timeout={args.timeout:.1f}s"
    )
    protocol.send_file(Path(args.file), server_addr, chunk_size=args.chunk_size)
except KeyboardInterrupt:
    pass
finally:
    sock.close()
