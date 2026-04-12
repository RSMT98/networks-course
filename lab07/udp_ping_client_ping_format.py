import argparse
import socket
import time

BUFFER_SIZE = 4096
TIMEOUT = 1

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(TIMEOUT)

rtts: list[float] = []
sent_packets = 0
received_packets = 0

print(f"UDP ping client is sending requests to {args.host}:{args.port}")
try:
    for i in range(1, 11):
        sent_time = time.time()
        msg = f"Ping {i} {sent_time:.6f}"
        print(f"Sending: {msg}")
        try:
            sock.sendto(msg.encode("utf-8"), (args.host, args.port))
            sent_packets += 1
            data, addr = sock.recvfrom(BUFFER_SIZE)
            rtt = time.time() - sent_time
            rtts.append(rtt)
            received_packets += 1
            resp = data.decode("utf-8")
            print(f"Reply from {addr[0]}:{addr[1]}: {resp}")
            print(f"RTT = {rtt:.6f} seconds")
        except socket.timeout:
            print("Request timed out")
except KeyboardInterrupt:
    pass
finally:
    sock.close()

lost_packets = sent_packets - received_packets
loss_rate = 0.0
if sent_packets:
    loss_rate = lost_packets / sent_packets * 100
print(f"\nPing statistics for {args.host}:{args.port}:")
print(
    f"Packets: Sent = {sent_packets}, Received = {received_packets}, "
    f"Lost = {lost_packets} ({loss_rate:.1f}% loss)"
)
if rtts:
    min_rtt = min(rtts)
    avg_rtt = sum(rtts) / len(rtts)
    max_rtt = max(rtts)
    print(f"RTT min/avg/max = " f"{min_rtt:.6f}/{avg_rtt:.6f}/{max_rtt:.6f} seconds")
else:
    print("RTT min/avg/max = n/a (no packets received)")
