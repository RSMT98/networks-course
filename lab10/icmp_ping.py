import argparse
import os
import socket
import struct
import sys
import time

ICMP_ECHO_REPLY = 0
ICMP_ECHO_REQUEST = 8
ICMP_DESTINATION_UNREACHABLE = 3
ICMP_TIME_EXCEEDED = 11
ICMP_HEADER_FORMAT = "!BBHHH"
ICMP_HEADER_SIZE = struct.calcsize(ICMP_HEADER_FORMAT)
TIMESTAMP_FORMAT = "!d"
TIMESTAMP_SIZE = struct.calcsize(TIMESTAMP_FORMAT)
BUFFER_SIZE = 65535

ICMP_ERROR_MESSAGES = {
    (3, 0): "destination network unreachable",
    (3, 1): "destination host unreachable",
    (3, 2): "destination protocol unreachable",
    (3, 3): "destination port unreachable",
    (3, 4): "fragmentation needed and DF flag is set",
    (3, 5): "source route failed",
    (3, 6): "destination network unknown",
    (3, 7): "destination host unknown",
    (3, 9): "destination network administratively prohibited",
    (3, 10): "destination host administratively prohibited",
    (3, 13): "communication administratively prohibited",
    (11, 0): "time to live exceeded in transit",
    (11, 1): "fragment reassembly time exceeded",
    (12, 0): "bad IP header",
}


def internet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"

    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
        total = (total & 0xFFFF) + (total >> 16)

    return ~total & 0xFFFF


def build_echo_request(id: int, seq: int, payload_size: int, sent_at: float) -> bytes:
    payload = struct.pack(TIMESTAMP_FORMAT, sent_at)
    if payload_size > TIMESTAMP_SIZE:
        payload += bytes(
            (ord("a") + i % 26 for i in range(payload_size - TIMESTAMP_SIZE))
        )

    header = struct.pack(
        ICMP_HEADER_FORMAT,
        ICMP_ECHO_REQUEST,
        0,
        0,
        id,
        seq,
    )
    checksum = internet_checksum(header + payload)
    header = struct.pack(
        ICMP_HEADER_FORMAT,
        ICMP_ECHO_REQUEST,
        0,
        checksum,
        id,
        seq,
    )
    return header + payload


def icmp_error_message(icmp_type: int, icmp_code: int) -> str:
    return ICMP_ERROR_MESSAGES.get(
        (icmp_type, icmp_code),
        f"unhandled ICMP error type={icmp_type}, code={icmp_code}",
    )


parser = argparse.ArgumentParser()
parser.add_argument("host")
parser.add_argument("--count", type=int, default=4)
parser.add_argument("--timeout", type=float, default=1.0)
parser.add_argument("--interval", type=float, default=1.0)
parser.add_argument("--payload-size", type=int, default=56)
parser.add_argument("--ttl", type=int)
args = parser.parse_args()

if args.count <= 0:
    parser.error("--count must be greater than 0")
if args.timeout <= 0:
    parser.error("--timeout must be greater than 0")
if args.interval < 0:
    parser.error("--interval must be greater than or equal to 0")
if args.payload_size < TIMESTAMP_SIZE:
    parser.error(f"--payload-size must be at least {TIMESTAMP_SIZE}")
if args.ttl is not None and not 1 <= args.ttl <= 255:
    parser.error("--ttl must be in range 1..255")

try:
    target_ip = socket.gethostbyname(args.host)
except socket.gaierror:
    print(f"Could not resolve host {args.host}.", file=sys.stderr)
    raise

try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
except PermissionError:
    print(
        "Raw ICMP sockets require admin/root privileges (try: sudo python3 icmp_ping.py ...)",
        file=sys.stderr,
    )
    raise
except OSError:
    print(f"Could not create raw ICMP socket.", file=sys.stderr)
    raise

if args.ttl is not None:
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, args.ttl)

id = os.getpid() & 0xFFFF
rtts: list[float] = []
sent_packets = 0
received_packets = 0
error_packets = 0

print(f"PING {args.host} ({target_ip}) {args.payload_size} data bytes")
try:
    for seq in range(1, args.count + 1):
        send_started_at = time.monotonic()
        packet = build_echo_request(id, seq, args.payload_size, send_started_at)
        sock.sendto(packet, (target_ip, 0))
        sent_packets += 1

        reply_received = False
        deadline = time.monotonic() + args.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(BUFFER_SIZE)
            except socket.timeout:
                break

            if len(data) < 20 + ICMP_HEADER_SIZE:
                continue

            ip_header_size = (data[0] & 0x0F) * 4
            if len(data) < ip_header_size + ICMP_HEADER_SIZE:
                continue

            ttl = data[8]
            icmp_packet = data[ip_header_size:]
            if internet_checksum(icmp_packet) != 0:
                continue

            icmp_type, icmp_code, _, reply_id, reply_seq = struct.unpack(
                ICMP_HEADER_FORMAT,
                icmp_packet[:ICMP_HEADER_SIZE],
            )
            payload = icmp_packet[ICMP_HEADER_SIZE:]

            if icmp_type == ICMP_ECHO_REPLY and reply_id == id and reply_seq == seq:
                received_packets += 1
                if len(payload) >= TIMESTAMP_SIZE:
                    sent_at = struct.unpack(TIMESTAMP_FORMAT, payload[:TIMESTAMP_SIZE])[
                        0
                    ]
                    rtt_ms = (time.monotonic() - sent_at) * 1000
                else:
                    rtt_ms = (time.monotonic() - send_started_at) * 1000
                rtts.append(rtt_ms)
                avg_rtt = sum(rtts) / len(rtts)
                print(
                    f"{len(icmp_packet)} bytes from {addr[0]}: icmp_seq={seq} ttl={ttl} time={rtt_ms:.3f} ms"
                )
                print(
                    f"rtt min/avg/max = {min(rtts):.3f}/{avg_rtt:.3f}/{max(rtts):.3f} ms"
                )
                reply_received = True
                break

            if icmp_type in {ICMP_DESTINATION_UNREACHABLE, ICMP_TIME_EXCEEDED, 12}:
                original_packet = payload
                if len(original_packet) < 20 + ICMP_HEADER_SIZE:
                    continue

                original_ip_header_size = (original_packet[0] & 0x0F) * 4
                original_icmp_start = original_ip_header_size
                if len(original_packet) < original_icmp_start + ICMP_HEADER_SIZE:
                    continue

                original_type, _, _, original_id, original_seq = struct.unpack(
                    ICMP_HEADER_FORMAT,
                    original_packet[
                        original_icmp_start : original_icmp_start + ICMP_HEADER_SIZE
                    ],
                )
                if (
                    original_type == ICMP_ECHO_REQUEST
                    and original_id == id
                    and original_seq == seq
                ):
                    error_packets += 1
                    print(
                        f"ICMP error from {addr[0]}: icmp_seq={seq} type={icmp_type} code={icmp_code} ({icmp_error_message(icmp_type, icmp_code)})"
                    )
                    reply_received = True
                    break

        if not reply_received:
            print(f"Request timeout for icmp_seq {seq}")

        pause = args.interval - (time.monotonic() - send_started_at)
        if seq != args.count and pause > 0:
            time.sleep(pause)
except KeyboardInterrupt:
    pass
finally:
    sock.close()

lost_packets = sent_packets - received_packets
loss_rate = 0.0
if sent_packets:
    loss_rate = lost_packets / sent_packets * 100

print(f"\n--- {args.host} ping statistics ---")
print(
    f"{sent_packets} packets transmitted, {received_packets} received, {error_packets} ICMP errors, {loss_rate:.1f}% packet loss"
)
if rtts:
    print(
        f"rtt min/avg/max = {min(rtts):.3f}/{sum(rtts) / len(rtts):.3f}/{max(rtts):.3f} ms"
    )
else:
    print("rtt min/avg/max = n/a")
