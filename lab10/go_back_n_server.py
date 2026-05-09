import argparse
import json
import socket
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

MAX_SEQ_SPACE_SIZE = 2**32

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--output-dir", default="out")
parser.add_argument("--seq-space-size", type=int, default=MAX_SEQ_SPACE_SIZE)
parser.add_argument("--timeout", type=float, default=0.5)
parser.add_argument("--done-timeout", type=float, default=2.0)
parser.add_argument("--loss-rate", type=float, default=0.0)
parser.add_argument("--corrupt-rate", type=float, default=0.0)
args = parser.parse_args()

try:
    validate_common_args(args.loss_rate, args.corrupt_rate, args.timeout)
    if not 2 <= args.seq_space_size <= MAX_SEQ_SPACE_SIZE:
        raise ValueError(f"--seq-space-size must be in range 2..{MAX_SEQ_SPACE_SIZE}")
    if args.done_timeout <= 0:
        raise ValueError("--done-timeout must be greater than 0")
except ValueError as e:
    parser.error(str(e))

output_dir = Path(args.output_dir)
output_dir.mkdir(parents=True, exist_ok=True)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((args.host, args.port))
sock.settimeout(args.timeout)
dst = None

print(f"Go-Back-N server is running on {args.host}:{args.port}")
print(
    f"Parameters: output_dir={output_dir}, seq_space_size={args.seq_space_size}, "
    f"timeout={args.timeout:.3f}s, done_timeout={args.done_timeout:.3f}s, "
    f"loss_rate={args.loss_rate:.2f}, corrupt_rate={args.corrupt_rate:.2f}"
)
try:
    while True:
        client_addr: tuple[str, int] | None = None
        expected_packet = 0
        output_path: Path | None = None
        expected_size = 0
        received_bytes = 0
        received_data_frames = 0
        total_frames = 0
        completed_at: float | None = None
        print("Waiting for next file transfer.")

        while True:
            try:
                frame, addr = recv_frame(sock, return_invalid=True)
            except socket.timeout:
                if completed_at is not None:
                    if time.monotonic() - completed_at >= args.done_timeout:
                        print("DONE was not received, but drain timeout expired.")
                        break
                continue

            if frame is None:
                if client_addr is not None and addr == client_addr:
                    ack = Frame(FrameType.ACK, expected_packet % args.seq_space_size)
                    send_frame(
                        sock,
                        client_addr,
                        ack,
                        loss_rate=args.loss_rate,
                        corrupt_rate=args.corrupt_rate,
                        side=ProtocolSide.SERVER,
                    )
                    print(
                        f"Corrupted frame from {format_addr(addr)}. Sent duplicate ACK next_seq={ack.seq}"
                    )
                continue

            if client_addr is None:
                if frame.frame_type != FrameType.START:
                    print(
                        f"Ignored {frame.frame_type.name} seq={frame.seq} from {format_addr(addr)} before START"
                    )
                    continue
            elif addr != client_addr:
                print(f"Ignored frame from unexpected address {format_addr(addr)}")
                continue

            if frame.frame_type == FrameType.DONE:
                if completed_at is None:
                    print("Received DONE before transfer completion. Frame ignored.")
                    ack = Frame(FrameType.ACK, expected_packet % args.seq_space_size)
                    send_frame(
                        sock,
                        client_addr,
                        ack,
                        loss_rate=args.loss_rate,
                        corrupt_rate=args.corrupt_rate,
                        side=ProtocolSide.SERVER,
                    )
                    print(f"Sent duplicate ACK next_seq={ack.seq}")
                    continue

                print(f"Received DONE seq={frame.seq} from client.")
                break

            if frame.frame_type == FrameType.ACK:
                print(f"Ignored ACK seq={frame.seq} while receiving file")
                continue

            if frame.frame_type == FrameType.ERROR:
                print(f"Ignored ERROR seq={frame.seq} while receiving file")
                continue

            if completed_at is not None:
                ack = Frame(FrameType.ACK, expected_packet % args.seq_space_size)
                send_frame(
                    sock,
                    client_addr,
                    ack,
                    loss_rate=args.loss_rate,
                    corrupt_rate=args.corrupt_rate,
                    side=ProtocolSide.SERVER,
                )
                print(
                    f"Duplicate frame after completion: {frame.frame_type.name} seq={frame.seq}. Sent duplicate ACK next_seq={ack.seq}."
                )
                continue

            expected_seq = expected_packet % args.seq_space_size
            if frame.seq != expected_seq:
                print(
                    f"Out-of-order {frame.frame_type.name} seq={frame.seq}, expected_seq={expected_seq}, expected_packet={expected_packet}"
                )
                ack = Frame(FrameType.ACK, expected_seq)
                ack_addr = client_addr or addr
                send_frame(
                    sock,
                    ack_addr,
                    ack,
                    loss_rate=args.loss_rate,
                    corrupt_rate=args.corrupt_rate,
                    side=ProtocolSide.SERVER,
                )
                print(f"Sent duplicate ACK next_seq={ack.seq}")
                continue

            if expected_packet == 0:
                expected_frame_type = FrameType.START
            elif expected_packet == total_frames - 1:
                expected_frame_type = FrameType.END
            else:
                expected_frame_type = FrameType.DATA

            if frame.frame_type != expected_frame_type:
                print(
                    f"Unexpected {frame.frame_type.name} at packet={expected_packet}, expected={expected_frame_type.name}. Frame ignored."
                )
                ack = Frame(FrameType.ACK, expected_seq)
                send_frame(
                    sock,
                    client_addr,
                    ack,
                    loss_rate=args.loss_rate,
                    corrupt_rate=args.corrupt_rate,
                    side=ProtocolSide.SERVER,
                )
                print(f"Sent duplicate ACK next_seq={ack.seq}")
                continue

            if frame.frame_type == FrameType.START:
                try:
                    meta_info = json.loads(frame.payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    error_msg = f"Invalid START payload: {e}"
                    print(error_msg)
                    error = Frame(
                        FrameType.ERROR, expected_seq, error_msg.encode("utf-8")
                    )
                    send_frame(
                        sock,
                        addr,
                        error,
                        loss_rate=args.loss_rate,
                        corrupt_rate=args.corrupt_rate,
                        side=ProtocolSide.SERVER,
                    )
                    print(f"Sent ERROR to {format_addr(addr)}: {error_msg}")
                    continue

                filename = meta_info.get("filename")
                file_size = meta_info.get("size")
                chunk_size = meta_info.get("chunk_size")
                window_size = meta_info.get("window_size")
                seq_space_size = meta_info.get("seq_space_size")
                raw_total_frames = meta_info.get("total_frames")
                if (
                    not isinstance(filename, str)
                    or not filename
                    or not isinstance(file_size, int)
                    or file_size < 0
                    or not isinstance(chunk_size, int)
                    or chunk_size <= 0
                    or not isinstance(window_size, int)
                    or window_size <= 0
                    or not isinstance(seq_space_size, int)
                    or seq_space_size != args.seq_space_size
                    or window_size >= seq_space_size
                    or not isinstance(raw_total_frames, int)
                    or raw_total_frames < 2
                ):
                    error_msg = f"Invalid START metadata: {meta_info}"
                    print(error_msg)
                    error = Frame(
                        FrameType.ERROR, expected_seq, error_msg.encode("utf-8")
                    )
                    send_frame(
                        sock,
                        addr,
                        error,
                        loss_rate=args.loss_rate,
                        corrupt_rate=args.corrupt_rate,
                        side=ProtocolSide.SERVER,
                    )
                    print(f"Sent ERROR to {format_addr(addr)}: {error_msg}")
                    continue

                client_addr = addr
                print(f"Accepted client {format_addr(client_addr)}")
                output_path = output_dir / Path(filename).name
                dst = output_path.open("wb")
                expected_size = file_size
                received_bytes = 0
                received_data_frames = 0
                total_frames = raw_total_frames
                print(
                    f"Started receiving file={output_path.name}, "
                    f"expected_size={expected_size}, chunk_size={chunk_size}, "
                    f"client_window={window_size}, seq_space_size={seq_space_size}, "
                    f"total_frames={total_frames}"
                )
            elif frame.frame_type == FrameType.DATA:
                if dst is None or output_path is None:
                    print("Received DATA before START. Frame ignored.")
                    continue

                dst.write(frame.payload)
                received_bytes += len(frame.payload)
                received_data_frames += 1
                print(
                    f"Accepted DATA packet={expected_packet}, seq={frame.seq}, "
                    f"bytes={len(frame.payload)}, total={received_bytes}; "
                    f"next_expected_packet={expected_packet + 1}"
                )
            elif frame.frame_type == FrameType.END:
                if dst is None or output_path is None:
                    print("Received END before START. Frame ignored.")
                    continue
                if expected_packet != total_frames - 1:
                    print(
                        f"Invalid END packet position: packet={expected_packet}, expected_end_packet={total_frames - 1}. Frame ignored."
                    )
                    ack = Frame(FrameType.ACK, expected_seq)
                    send_frame(
                        sock,
                        client_addr,
                        ack,
                        loss_rate=args.loss_rate,
                        corrupt_rate=args.corrupt_rate,
                        side=ProtocolSide.SERVER,
                    )
                    print(f"Sent duplicate ACK next_seq={ack.seq}")
                    continue
                if received_data_frames != total_frames - 2:
                    print(
                        f"DATA frame count mismatch: expected={total_frames - 2}, received={received_data_frames}"
                    )

                dst.close()
                dst = None
                if received_bytes != expected_size:
                    print(
                        f"File size mismatch: expected={expected_size}, received={received_bytes}"
                    )
                print(
                    f"File received: path={output_path}, bytes={received_bytes}, data_frames={received_data_frames}, total_frames={total_frames}"
                )
                completed_at = time.monotonic()
            else:
                print(f"Ignored unexpected frame type={frame.frame_type.name}")
                continue

            accepted_packet = expected_packet
            expected_packet += 1
            ack = Frame(FrameType.ACK, expected_packet % args.seq_space_size)
            send_frame(
                sock,
                client_addr,
                ack,
                loss_rate=args.loss_rate,
                corrupt_rate=args.corrupt_rate,
                side=ProtocolSide.SERVER,
            )
            print(
                f"Sent ACK next_seq={ack.seq}; acked_packet={accepted_packet}; "
                f"receiver_state=expected_packet={expected_packet}, "
                f"expected_seq={expected_packet % args.seq_space_size}"
            )
except KeyboardInterrupt:
    pass
finally:
    if dst is not None:
        dst.close()
    sock.close()
