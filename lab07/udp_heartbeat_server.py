import argparse
import random
import socket
import time
from dataclasses import dataclass

BUFFER_SIZE = 4096


@dataclass
class HeartbeatClientState:
    last_seq: int
    received_packets: int
    lost_packets: int
    last_seen_at: float
    is_online: bool


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--loss-rate", type=float, default=0.2)
parser.add_argument("--client-timeout", type=float, default=3.0)
parser.add_argument("--check-interval", type=float, default=0.5)
args = parser.parse_args()

if not 0.0 <= args.loss_rate <= 1.0:
    parser.error("--loss-rate must be in range 0..1")
if args.client_timeout <= 0:
    parser.error("--client-timeout must be greater than 0")
if args.check_interval <= 0:
    parser.error("--check-interval must be greater than 0")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((args.host, args.port))
sock.settimeout(args.check_interval)

clients: dict[tuple[str, int], HeartbeatClientState] = {}

print(f"UDP heartbeat server is running on {args.host}:{args.port}")
print(
    "Parameters: "
    f"loss_rate={args.loss_rate:.2f}, client_timeout={args.client_timeout:.1f}s, "
    f"check_interval={args.check_interval:.1f}s"
)
try:
    while True:
        now = time.monotonic()
        for addr, state in list(clients.items()):
            if not state.is_online:
                continue

            idle_time = now - state.last_seen_at
            if idle_time >= args.client_timeout:
                state.is_online = False
                print(
                    f"No heartbeat from {addr[0]}:{addr[1]} for {idle_time:.3f} seconds. "
                    "The client is considered stopped."
                )

        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
        except socket.timeout:
            continue

        received_at = time.time()
        msg = data.decode("utf-8")
        try:
            parts = msg.strip().split()
            if len(parts) != 3 or parts[0] != "Heartbeat":
                raise ValueError(
                    "Expected message in format: Heartbeat <sequence> <timestamp>"
                )

            seq_num = int(parts[1])
            sent_time = float(parts[2])
        except ValueError as e:
            print(
                f"Invalid heartbeat from {addr[0]}:{addr[1]}: {e}. Raw message: {msg!r}"
            )
            continue

        if random.random() < args.loss_rate:
            print(f"Packet loss for {addr[0]}:{addr[1]}, seq={seq_num}")
            continue

        delay = received_at - sent_time
        state = clients.get(addr)
        if state is None:
            state = HeartbeatClientState(
                last_seq=0,
                received_packets=0,
                lost_packets=0,
                last_seen_at=0.0,
                is_online=True,
            )
            clients[addr] = state
            print(f"New heartbeat client: {addr[0]}:{addr[1]}")
        elif not state.is_online:
            state.is_online = True
            print(f"Heartbeat resumed from {addr[0]}:{addr[1]}")
            if seq_num <= state.last_seq:
                print(f"Client {addr[0]}:{addr[1]} started a new heartbeat session.")
                state.last_seq = 0
                state.received_packets = 0
                state.lost_packets = 0

        if seq_num <= state.last_seq:
            print(
                f"Out-of-order heartbeat from {addr[0]}:{addr[1]}: "
                f"received seq={seq_num}, last_seq={state.last_seq}"
            )
        elif state.received_packets > 0 and seq_num > state.last_seq + 1:
            missed_packets = seq_num - state.last_seq - 1
            state.lost_packets += missed_packets
            print(
                f"Detected {missed_packets} lost heartbeat(s) from {addr[0]}:{addr[1]}: "
                f"expected {state.last_seq + 1}, got {seq_num}"
            )

        state.last_seq = max(state.last_seq, seq_num)
        state.received_packets += 1
        state.last_seen_at = time.monotonic()

        print(
            f"Heartbeat from {addr[0]}:{addr[1]}: "
            f"seq={seq_num}, delay={delay:.6f} seconds, "
            f"received={state.received_packets}, lost={state.lost_packets}"
        )
except KeyboardInterrupt:
    pass
finally:
    sock.close()
