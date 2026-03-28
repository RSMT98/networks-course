import json
import socket
import subprocess
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8888)
args = parser.parse_args()

HOST = "127.0.0.1"
BUFFER_SIZE = 4096
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, args.port))
    sock.listen()

    print(f"Server is running on {HOST}:{args.port}")
    print("Waiting for connections...")

    while True:
        conn, addr = sock.accept()
        with conn:
            print(f"The client is connected: {addr}")

            try:
                batches: list[bytes] = []
                while True:
                    data = conn.recv(BUFFER_SIZE)
                    if not data:
                        break
                    batches.append(data)
                raw_request = b"".join(batches)
                if not raw_request:
                    raise ValueError("Empty request")

                command = raw_request.decode("utf-8")
                completed = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                output = completed.stdout
                if completed.stderr:
                    if output and not output.endswith("\n"):
                        output += "\n"
                    output += "[stderr]\n" + completed.stderr
                resp = {
                    "ok": completed.returncode == 0,
                    "requested_command": command,
                    "return_code": completed.returncode,
                    "output": output,
                }
            except FileNotFoundError as e:
                resp = {
                    "ok": False,
                    "error": f"Requested command wasn't found in the server system: {e}",
                }
            except Exception as e:
                resp = {
                    "ok": False,
                    "error": str(e),
                }

            conn.sendall(json.dumps(resp, ensure_ascii=False, indent=2).encode("utf-8"))
            print(f"The client is disconnected: {addr}")
