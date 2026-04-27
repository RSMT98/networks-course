import argparse
import socket
from pathlib import Path

from stop_and_wait_protocol import DuplexStopAndWaitProtocol

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--peer-host", default="127.0.0.1")
parser.add_argument("--peer-port", type=int, required=True)
parser.add_argument("--file", required=True)
parser.add_argument("--output-dir", required=True)
parser.add_argument("--chunk-size", type=int, default=512)
parser.add_argument("--timeout", type=float, default=0.3)
parser.add_argument("--loss-rate", type=float, default=0.3)
parser.add_argument("--corrupt-rate", type=float, default=0.0)
parser.add_argument("--max-retries", type=int, default=30)
args = parser.parse_args()

peer_addr = (args.peer_host, args.peer_port)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((args.host, args.port))
    protocol = DuplexStopAndWaitProtocol(
        sock,
        peer_addr,
        timeout=args.timeout,
        loss_rate=args.loss_rate,
        corrupt_rate=args.corrupt_rate,
        max_retries=args.max_retries,
    )
    print(f"Stop-and-Wait duplex peer is running on {args.host}:{args.port}")
    print(
        f"Parameters: chunk_size={args.chunk_size}, loss_rate={args.loss_rate:.2f}, corrupt_rate={args.corrupt_rate:.2f}, timeout={args.timeout:.1f}s"
    )

    protocol.start_receiving(Path(args.output_dir))
    protocol.send_file_to_peer(Path(args.file), chunk_size=args.chunk_size)
    result = protocol.wait_for_received_file()
    print(
        f"Duplex exchange finished. Received file: {result.path}, bytes={result.bytes_count}, frames={result.frames_count}"
    )
    protocol.wait_for_drain()
except KeyboardInterrupt:
    pass
finally:
    try:
        protocol.stop_receiving()
    except NameError:
        pass
    sock.close()
