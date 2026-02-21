import socket
import sys

if len(sys.argv) != 4:
    print("Usage: python client.py <server_host> <server_port> <filename>")
    sys.exit(1)
server_host = sys.argv[1]
server_port = int(sys.argv[2])
filename = sys.argv[3]
try:
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((server_host, server_port))
    request = f"GET /{filename} HTTP/1.1\r\nHost: {server_host}:{server_port}\r\nConnection: close\r\n\r\n"
    client_socket.sendall(request.encode('utf-8'))
    response_parts = []
    while True:
        data = client_socket.recv(4096)
        if not data:
            break
        response_parts.append(data)
    response = b''.join(response_parts).decode('utf-8', errors='replace')
    print(response)
except ConnectionRefusedError:
    print(f"error: Couldn't connect to {server_host}:{server_port}.")
except socket.gaierror:
    print(f"error: Invalid host name or IP address '{server_host}'.")
except Exception as e:
    print(f"error: {e}")
finally:
    if 'client_socket' in locals():
        client_socket.close()
