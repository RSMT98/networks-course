import argparse
import socket
from pathlib import Path

from stop_and_wait_protocol import StopAndWaitProtocol

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--output-dir", default="out")
parser.add_argument("--timeout", type=float, default=0.3)
parser.add_argument("--loss-rate", type=float, default=0.3)
parser.add_argument("--corrupt-rate", type=float, default=0.0)
parser.add_argument("--max-retries", type=int, default=30)
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((args.host, args.port))
    protocol = StopAndWaitProtocol(
        sock,
        timeout=args.timeout,
        loss_rate=args.loss_rate,
        corrupt_rate=args.corrupt_rate,
        max_retries=args.max_retries,
    )
    print(f"Stop-and-Wait server is running on {args.host}:{args.port}")
    print(
        f"Parameters: loss_rate={args.loss_rate:.2f}, corrupt_rate={args.corrupt_rate:.2f}, timeout={args.timeout:.1f}s"
    )
    while True:
        try:
            result = protocol.receive_file(Path(args.output_dir))
        except ValueError as e:
            print(f"Transfer error: {e}")
            continue

        print(
            f"Saved uploaded file: {result.path}, bytes={result.bytes_count}, frames={result.frames_count}"
        )
except KeyboardInterrupt:
    pass
finally:
    sock.close()
