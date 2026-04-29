import argparse
import errno
import socket

MIN_PORT = 1
MAX_PORT = 65535
PROTOCOLS = (
    ("TCP", socket.SOCK_STREAM),
    ("UDP", socket.SOCK_DGRAM),
)

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--start-port", type=int, required=True)
parser.add_argument("--end-port", type=int, required=True)
args = parser.parse_args()
try:
    if not MIN_PORT <= args.start_port <= MAX_PORT:
        raise ValueError(f"--start-port must be in range {MIN_PORT}..{MAX_PORT}")
    if not MIN_PORT <= args.end_port <= MAX_PORT:
        raise ValueError(f"--end-port must be in range {MIN_PORT}..{MAX_PORT}")
    if args.start_port > args.end_port:
        raise ValueError("--start-port must be less than or equal to --end-port")

    for _, socket_type in PROTOCOLS:
        with socket.socket(socket.AF_INET, socket_type) as sock:
            sock.bind((args.host, 0))
except OSError as e:
    parser.error(f"Could not bind to {args.host}: {e}")
except ValueError as e:
    parser.error(str(e))

available_ports = []
port_check_errors = []
available_counts = {protocol_name: 0 for protocol_name, _ in PROTOCOLS}
for port in range(args.start_port, args.end_port + 1):
    available_protocols = []
    for protocol_name, socket_type in PROTOCOLS:
        try:
            with socket.socket(socket.AF_INET, socket_type) as sock:
                sock.bind((args.host, port))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                continue
            if e.errno in {errno.EACCES, errno.EPERM}:
                port_check_errors.append((port, protocol_name, "permission denied"))
            elif e.errno == errno.EADDRNOTAVAIL:
                port_check_errors.append((port, protocol_name, "address not available"))
            else:
                port_check_errors.append((port, protocol_name, str(e)))
            continue

        available_protocols.append(protocol_name)
        available_counts[protocol_name] += 1

    if available_protocols:
        available_ports.append((port, available_protocols))

print(
    f"Available local ports on {args.host} in range "
    f"{args.start_port}..{args.end_port}:"
)
for port, protocols in available_ports:
    print(f"{port}: {', '.join(protocols)}")
print(f"Total TCP: {available_counts['TCP']}")
print(f"Total UDP: {available_counts['UDP']}")

if port_check_errors:
    print("\nPort check errors:")
    for port, protocol_name, error in port_check_errors:
        print(f"{port}: {protocol_name} -> {error}")
