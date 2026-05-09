import argparse
import json
import socket
import sys
import time
from pathlib import Path

from go_back_n_protocol import (
    Frame,
    FrameType,
    ProtocolSide,
    format_addr,
    recv_frame,
    send_frame,
    validate_common_args,
)

MAX_CHUNK_SIZE = 60000
MAX_SEQ_SPACE_SIZE = 2**32


def format_packets(packet_nums: range | list[int], seq_space_size: int) -> str:
    packet_nums = list(packet_nums)
    if not packet_nums:
        return "[]"
    if len(packet_nums) <= 8:
        return (
            "["
            + ", ".join(
                f"{packet_num}/seq={packet_num % seq_space_size}"
                for packet_num in packet_nums
            )
            + "]"
        )

    return (
        f"{packet_nums[0]}..{packet_nums[-1]} "
        f"({len(packet_nums)} packets, seq "
        f"{packet_nums[0] % seq_space_size}.."
        f"{packet_nums[-1] % seq_space_size} % {seq_space_size})"
    )


def format_client_state(
    base: int,
    next_packet: int,
    total_frames: int,
    window_size: int,
    seq_space_size: int,
) -> str:
    window = range(base, min(base + window_size, total_frames))
    acked = range(0, base)
    sent_not_acked = range(base, next_packet)
    not_sent = range(next_packet, total_frames)
    return (
        f"window={format_packets(window, seq_space_size)}, acked={format_packets(acked, seq_space_size)}, "
        f"sent_not_acked={format_packets(sent_not_acked, seq_space_size)}, not_sent={format_packets(not_sent, seq_space_size)}"
    )


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--file", required=True)
parser.add_argument("--chunk-size", type=int, default=512)
parser.add_argument("--window-size", type=int, default=4)
parser.add_argument("--seq-space-size", type=int, default=MAX_SEQ_SPACE_SIZE)
parser.add_argument("--timeout", type=float, default=0.5)
parser.add_argument("--loss-rate", type=float, default=0.0)
parser.add_argument("--corrupt-rate", type=float, default=0.0)
parser.add_argument("--max-retries", type=int, default=30)
args = parser.parse_args()

try:
    validate_common_args(args.loss_rate, args.corrupt_rate, args.timeout)
    if args.chunk_size <= 0 or args.chunk_size > MAX_CHUNK_SIZE:
        raise ValueError(f"--chunk-size must be in range 1..{MAX_CHUNK_SIZE}")
    if args.window_size <= 0:
        raise ValueError("--window-size must be greater than 0")
    if not 2 <= args.seq_space_size <= MAX_SEQ_SPACE_SIZE:
        raise ValueError(f"--seq-space-size must be in range 2..{MAX_SEQ_SPACE_SIZE}")
    if args.window_size >= args.seq_space_size:
        raise ValueError("--window-size must be less than --seq-space-size for GBN")
    if args.max_retries <= 0:
        raise ValueError("--max-retries must be greater than 0")
except ValueError as e:
    parser.error(str(e))

filepath = Path(args.file)
if not filepath.is_file():
    print(f"File not found: {filepath}", file=sys.stderr)
    raise

data_frames: list[Frame] = []
with filepath.open("rb") as src:
    packet_num = 1
    while True:
        chunk = src.read(args.chunk_size)
        if not chunk:
            break

        data_frames.append(
            Frame(FrameType.DATA, packet_num % args.seq_space_size, chunk)
        )
        packet_num += 1

total_frames = len(data_frames) + 2
meta_info = {
    "filename": filepath.name,
    "size": filepath.stat().st_size,
    "chunk_size": args.chunk_size,
    "window_size": args.window_size,
    "seq_space_size": args.seq_space_size,
    "total_frames": total_frames,
}
frames = [
    Frame(
        FrameType.START,
        0,
        json.dumps(meta_info, ensure_ascii=False).encode("utf-8"),
    ),
    *data_frames,
    Frame(FrameType.END, (total_frames - 1) % args.seq_space_size),
]

try:
    server_ip = socket.gethostbyname(args.host)
except socket.gaierror:
    print(f"Could not resolve host {args.host}.", file=sys.stderr)
    raise

server_addr = (server_ip, args.port)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(args.timeout)

base = 0
next_packet = 0
timer_started_at: float | None = None
retries_count = 0

print(f"Go-Back-N client is sending file to {format_addr(server_addr)}")
print(
    f"Parameters: file={filepath}, chunk_size={args.chunk_size}, "
    f"window_size={args.window_size}, seq_space_size={args.seq_space_size}, "
    f"timeout={args.timeout:.3f}s, max_retries={args.max_retries}, "
    f"loss_rate={args.loss_rate:.2f}, corrupt_rate={args.corrupt_rate:.2f}"
)
print(f"Prepared frames: total={total_frames}, data={len(data_frames)}")
try:
    while base < total_frames:
        while next_packet < base + args.window_size and next_packet < total_frames:
            frame = frames[next_packet]
            send_frame(
                sock,
                server_addr,
                frame,
                loss_rate=args.loss_rate,
                corrupt_rate=args.corrupt_rate,
                side=ProtocolSide.CLIENT,
            )
            print(
                f"Sent {frame.frame_type.name} packet={next_packet}, seq={frame.seq}, "
                f"bytes={len(frame.payload)}, checksum=0x{frame.checksum:04x}; "
                f"{format_client_state(base, next_packet + 1, total_frames, args.window_size, args.seq_space_size)}"
            )
            if base == next_packet:
                timer_started_at = time.monotonic()
            next_packet += 1

        if base >= total_frames:
            break

        if timer_started_at is None:
            timer_started_at = time.monotonic()

        ack_frame = None
        ack_addr = None
        timer_deadline = timer_started_at + args.timeout
        try:
            ack_frame, ack_addr = recv_frame(sock, deadline=timer_deadline)
        except socket.timeout:
            pass

        if ack_frame is None or ack_addr is None:
            retries_count += 1
            if retries_count > args.max_retries:
                raise TimeoutError(
                    f"No ACK from {format_addr(server_addr)} after {args.max_retries} retries"
                )
            print(
                f"Timeout waiting for ACK next_seq={base % args.seq_space_size}. "
                f"Go-Back-N resend packets {base}..{next_packet - 1}, retry={retries_count}/{args.max_retries}."
            )
            for resend_packet in range(base, next_packet):
                frame = frames[resend_packet]
                send_frame(
                    sock,
                    server_addr,
                    frame,
                    loss_rate=args.loss_rate,
                    corrupt_rate=args.corrupt_rate,
                    side=ProtocolSide.CLIENT,
                )
                print(
                    f"Resent {frame.frame_type.name} packet={resend_packet}, seq={frame.seq}, "
                    f"bytes={len(frame.payload)}, checksum=0x{frame.checksum:04x}; "
                    f"{format_client_state(base, next_packet, total_frames, args.window_size, args.seq_space_size)}"
                )
            timer_started_at = time.monotonic()
            continue

        if ack_addr != server_addr:
            print(f"Ignored ACK from unexpected address {format_addr(ack_addr)}")
            continue
        if ack_frame.frame_type == FrameType.ERROR:
            error_msg = ack_frame.payload.decode("utf-8", errors="replace")
            raise RuntimeError(f"Server rejected transfer: {error_msg}")
        if ack_frame.frame_type != FrameType.ACK:
            print(
                f"Ignored {ack_frame.frame_type.name} seq={ack_frame.seq} while waiting for ACK"
            )
            continue

        new_acked_count = (
            ack_frame.seq - (base % args.seq_space_size)
        ) % args.seq_space_size
        if new_acked_count == 0:
            print(
                f"Duplicate ACK next_seq={ack_frame.seq}; "
                f"{format_client_state(base, next_packet, total_frames, args.window_size, args.seq_space_size)}"
            )
            continue
        if new_acked_count > (next_packet - base):
            print(
                f"Ignored ACK next_seq={ack_frame.seq}: outside current window; "
                f"{format_client_state(base, next_packet, total_frames, args.window_size, args.seq_space_size)}"
            )
            continue

        old_base = base
        base += new_acked_count
        retries_count = 0
        print(
            f"Received ACK next_seq={ack_frame.seq}, acked_packets={format_packets(range(old_base, base), args.seq_space_size)}; "
            f"{format_client_state(base, next_packet, total_frames, args.window_size, args.seq_space_size)}"
        )
        if base == next_packet:
            timer_started_at = None
        else:
            timer_started_at = time.monotonic()

    done = Frame(FrameType.DONE, total_frames % args.seq_space_size)
    for i in range(1, 4):
        send_frame(
            sock,
            server_addr,
            done,
            loss_rate=args.loss_rate,
            corrupt_rate=args.corrupt_rate,
            side=ProtocolSide.CLIENT,
        )
        print(
            f"Sent DONE seq={done.seq} to {format_addr(server_addr)}, checksum=0x{done.checksum:04x}, copy={i}"
        )

    print(
        f"Transfer finished: file={filepath}, bytes={filepath.stat().st_size}, frames={total_frames}"
    )
except KeyboardInterrupt:
    pass
finally:
    sock.close()
