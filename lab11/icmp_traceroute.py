import argparse
import random
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
MAX_ICMP_SEQ = 0xFFFF

ICMP_ERROR_MARKS = {
    (3, 0): "!N",
    (3, 1): "!H",
    (3, 2): "!P",
    (3, 3): "!port",
    (3, 4): "!F",
    (3, 5): "!source-route",
    (3, 6): "!network-unknown",
    (3, 7): "!host-unknown",
    (3, 9): "!network-prohibited",
    (3, 10): "!host-prohibited",
    (3, 13): "!X",
}


def internet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"

    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
        total = (total & 0xFFFF) + (total >> 16)

    return ~total & 0xFFFF


def build_echo_request(
    request_id: int, seq: int, payload_size: int, sent_at: float
) -> bytes:
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
        request_id,
        seq,
    )
    checksum = internet_checksum(header + payload)
    header = struct.pack(
        ICMP_HEADER_FORMAT,
        ICMP_ECHO_REQUEST,
        0,
        checksum,
        request_id,
        seq,
    )
    return header + payload


parser = argparse.ArgumentParser()
parser.add_argument("host")
parser.add_argument("--count", type=int, default=3)
parser.add_argument("--timeout", type=float, default=1.0)
parser.add_argument("--interval", type=float, default=0.0)
parser.add_argument("--max-hops", type=int, default=30)
parser.add_argument("--payload-size", type=int, default=56)
parser.add_argument("--no-names", action="store_true")
args = parser.parse_args()

if args.count <= 0:
    parser.error("--count must be greater than 0")
if args.timeout <= 0:
    parser.error("--timeout must be greater than 0")
if args.interval < 0:
    parser.error("--interval must be greater than or equal to 0")
if not 1 <= args.max_hops <= 255:
    parser.error("--max-hops must be in range 1..255")
if args.payload_size < TIMESTAMP_SIZE:
    parser.error(f"--payload-size must be at least {TIMESTAMP_SIZE}")
if args.count * args.max_hops > MAX_ICMP_SEQ:
    parser.error(f"--count * --max-hops must be no greater than {MAX_ICMP_SEQ}")

try:
    target_ip = socket.gethostbyname(args.host)
except socket.gaierror:
    print(f"Could not resolve host {args.host}.", file=sys.stderr)
    raise

try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
except PermissionError:
    print(
        "Raw ICMP sockets require admin/root privileges (try: sudo python3 icmp_traceroute.py ...)",
        file=sys.stderr,
    )
    raise
except OSError:
    print("Could not create raw ICMP socket.", file=sys.stderr)
    raise

request_id = random.randrange(MAX_ICMP_SEQ + 1)
seq = 0
names: dict[str, str] = {}

print(
    f"Tracing route to {args.host} ({target_ip}), {args.max_hops} hops max, "
    f"{args.count} probes per hop"
)
try:
    for ttl in range(1, args.max_hops + 1):
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        responses: list[tuple[str, float, str] | None] = []
        target_reached = False
        trace_stopped = False
        stop_reason = ""

        for probe_num in range(1, args.count + 1):
            seq += 1
            sent_at = time.monotonic()
            packet = build_echo_request(request_id, seq, args.payload_size, sent_at)
            sock.sendto(packet, (target_ip, 0))

            response: tuple[str, float, str] | None = None
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
                if data[9] != socket.IPPROTO_ICMP:
                    continue

                icmp_packet = data[ip_header_size:]
                if internet_checksum(icmp_packet) != 0:
                    continue

                icmp_type, icmp_code, _, reply_id, reply_seq = struct.unpack(
                    ICMP_HEADER_FORMAT,
                    icmp_packet[:ICMP_HEADER_SIZE],
                )
                rtt_ms = (time.monotonic() - sent_at) * 1000

                if (
                    icmp_type == ICMP_ECHO_REPLY
                    and addr[0] == target_ip
                    and reply_id == request_id
                    and reply_seq == seq
                ):
                    response = (addr[0], rtt_ms, "")
                    target_reached = True
                    break

                if icmp_type not in {
                    ICMP_DESTINATION_UNREACHABLE,
                    ICMP_TIME_EXCEEDED,
                }:
                    continue

                original_packet = icmp_packet[ICMP_HEADER_SIZE:]
                if len(original_packet) < 20 + ICMP_HEADER_SIZE:
                    continue

                original_ip_protocol = original_packet[9]
                original_dst_ip = socket.inet_ntoa(original_packet[16:20])
                if (
                    original_ip_protocol != socket.IPPROTO_ICMP
                    or original_dst_ip != target_ip
                ):
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
                    original_type != ICMP_ECHO_REQUEST
                    or original_id != request_id
                    or original_seq != seq
                ):
                    continue

                mark = ""
                if icmp_type == ICMP_DESTINATION_UNREACHABLE:
                    mark = ICMP_ERROR_MARKS.get((icmp_type, icmp_code), f"!{icmp_code}")
                    trace_stopped = True
                    stop_reason = f"Trace stopped: destination unreachable {mark}."

                response = (addr[0], rtt_ms, mark)
                break

            responses.append(response)
            if args.interval > 0 and probe_num != args.count:
                time.sleep(args.interval)

        line_parts = [f"{ttl:2d}"]
        probe_addr_labels: list[str | None] = []
        for response in responses:
            if response is None:
                line_parts.append(f"{'*':>10}")
                probe_addr_labels.append(None)
                continue

            addr, rtt_ms, mark = response
            rtt_text = f"{rtt_ms:.3f} ms"
            if mark:
                rtt_text += f" {mark}"
            line_parts.append(f"{rtt_text:>10}")

            if args.no_names:
                probe_addr_labels.append(addr)
            else:
                if addr not in names:
                    try:
                        names[addr] = socket.gethostbyaddr(addr)[0]
                    except (socket.herror, socket.gaierror):
                        names[addr] = ""

                if names[addr]:
                    probe_addr_labels.append(f"{names[addr]} ({addr})")
                else:
                    probe_addr_labels.append(f"{addr} (name not found)")

        unique_addr_labels = []
        for addr_label in probe_addr_labels:
            if addr_label is not None and addr_label not in unique_addr_labels:
                unique_addr_labels.append(addr_label)

        if len(unique_addr_labels) == 1:
            line_parts.append(unique_addr_labels[0])
        elif len(unique_addr_labels) > 1:
            for probe_num, addr_label in enumerate(probe_addr_labels, 1):
                if addr_label is not None:
                    line_parts.append(f"probe {probe_num}: {addr_label}")

        print("  ".join(line_parts))

        if target_reached:
            print("Trace complete.")
            break
        if trace_stopped:
            print(stop_reason)
            break
except KeyboardInterrupt:
    pass
finally:
    sock.close()
