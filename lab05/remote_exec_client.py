import json
import socket
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--command", required=True)
args = parser.parse_args()

SERVER_HOST = "127.0.0.1"
BUFFER_SIZE = 4096
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.connect((SERVER_HOST, args.port))
    sock.sendall(args.command.encode("utf-8"))
    sock.shutdown(socket.SHUT_WR)

    batches: list[bytes] = []
    while True:
        data = sock.recv(BUFFER_SIZE)
        if not data:
            break
        batches.append(data)
    raw_response = b"".join(batches)

resp = json.loads(raw_response.decode("utf-8"))
if "requested_command" in resp:
    if resp.get("ok"):
        print("Requested command was executed successfully!")
    else:
        print("Requested command was started, but finished with an error.")
    print(f"Requested command: {resp.get('requested_command')}")
    print(f"Return code: {resp.get('return_code')}")
    print("Command output on the server:")
    print("=" * 30)
    print(resp.get("output", ""))
    print("=" * 30)
else:
    print(f"Error: {resp.get("error", "Unknown error")}")
