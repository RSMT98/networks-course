import argparse
import base64
import re
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from email.header import Header


def read_response(smtp_file) -> tuple[int, list[str]]:
    lines = []
    while True:
        raw_line = smtp_file.readline()
        if not raw_line:
            raise RuntimeError("Connection closed while reading SMTP server response")

        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        lines.append(line)
        if len(line) < 4 or line[3] != "-":
            break

    for line in lines:
        print(f"S: {line}")

    try:
        code = int(lines[-1][:3])
    except ValueError as e:
        raise RuntimeError(f"Invalid SMTP response: {lines[-1]}") from e

    return code, lines


def send_cmd(
    smtp_socket,
    smtp_file,
    cmd: str,
    expected_codes: set[int],
    log_cmd: str | None = None,
) -> tuple[int, list[str]]:
    print(f"C: {log_cmd or cmd}")
    smtp_socket.sendall((cmd + "\r\n").encode("ascii"))
    code, lines = read_response(smtp_file)
    if code not in expected_codes:
        raise RuntimeError(f"Command '{cmd}' failed with SMTP response: {lines[-1]}")

    return code, lines


def send_auth_line(
    smtp_socket, smtp_file, val: str, expected_codes: set[int]
) -> tuple[int, list[str]]:
    encoded_val = base64.b64encode(val.encode("utf-8")).decode("ascii")
    print("C: [secret]")
    smtp_socket.sendall((encoded_val + "\r\n").encode("ascii"))
    code, lines = read_response(smtp_file)
    if code not in expected_codes:
        raise RuntimeError(f"SMTP authentication failed: {lines[-1]}")

    return code, lines


HOST = "smtp.mail.ru"
PORT = 465
SENDER_EMAIL = "<your email here>"
SENDER_PASSWORD = "<your password here>"
EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)
TIMEOUT = 10

parser = argparse.ArgumentParser()
parser.add_argument("--to", required=True)
parser.add_argument("--subject", required=True)
content_group = parser.add_mutually_exclusive_group(required=True)
content_group.add_argument("--body")
content_group.add_argument("--body-file")
args = parser.parse_args()

if not EMAIL_REGEX.fullmatch(SENDER_EMAIL):
    raise ValueError(f"Invalid sender email address: {SENDER_EMAIL}")
if not EMAIL_REGEX.fullmatch(args.to):
    raise ValueError(f"Invalid recipient email address: {args.to}")

body = (
    args.body
    if args.body is not None
    else Path(args.body_file).read_text(encoding="utf-8")
)
context = ssl.create_default_context()
lines = [
    f"From: {SENDER_EMAIL}",
    f"To: {args.to}",
    f"Subject: {Header(args.subject, 'utf-8').encode()}",
    f"Date: {datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}",
    "MIME-Version: 1.0",
    'Content-Type: text/plain; charset="utf-8"',
    "Content-Transfer-Encoding: base64",
    "",
    base64.encodebytes(body.encode("utf-8"))
    .decode("ascii")
    .rstrip("\n")
    .replace("\n", "\r\n"),
    "",
]
msg = "\r\n".join(lines)
dot_stuffed_lines = []
for line in msg.split("\r\n"):
    if line.startswith("."):
        dot_stuffed_lines.append("." + line)
    else:
        dot_stuffed_lines.append(line)
msg_bytes = "\r\n".join(dot_stuffed_lines).encode("ascii")
with socket.create_connection((HOST, PORT), timeout=TIMEOUT) as tcp_socket:
    with context.wrap_socket(tcp_socket, server_hostname=HOST) as smtp_socket:
        smtp_socket.settimeout(TIMEOUT)
        smtp_file = smtp_socket.makefile("rb")
        code, _ = read_response(smtp_file)
        if code != 220:
            raise RuntimeError("SMTP server did not send 220 after connecting")

        _, ehlo_lines = send_cmd(
            smtp_socket,
            smtp_file,
            "EHLO localhost",
            {250},
        )
        auth_methods = set()
        for line in ehlo_lines:
            if len(line) < 4:
                continue

            upper_data = line[4:].strip().upper()
            if not upper_data.startswith("AUTH"):
                continue

            parts = upper_data.split()
            auth_methods.update(parts[1:])

        if "PLAIN" in auth_methods:
            auth_val = f"\0{SENDER_EMAIL}\0{SENDER_PASSWORD}"
            encoded_val = base64.b64encode(auth_val.encode("utf-8")).decode("ascii")
            send_cmd(
                smtp_socket,
                smtp_file,
                f"AUTH PLAIN {encoded_val}",
                {235},
                log_cmd="AUTH PLAIN [secret]",
            )
        elif "LOGIN" in auth_methods:
            send_cmd(smtp_socket, smtp_file, "AUTH LOGIN", {334})
            send_auth_line(smtp_socket, smtp_file, SENDER_EMAIL, {334})
            send_auth_line(smtp_socket, smtp_file, SENDER_PASSWORD, {235})
        else:
            raise RuntimeError("SMTP server does not support PLAIN or LOGIN methods")

        send_cmd(smtp_socket, smtp_file, f"MAIL FROM:<{SENDER_EMAIL}>", {250})
        send_cmd(smtp_socket, smtp_file, f"RCPT TO:<{args.to}>", {250, 251})
        send_cmd(smtp_socket, smtp_file, "DATA", {354})
        print("C: [message body]")
        smtp_socket.sendall(msg_bytes + b"\r\n.\r\n")
        code, lines = read_response(smtp_file)
        if code != 250:
            raise RuntimeError(
                f"Couldn't send the message to the SMTP server: {lines[-1]}"
            )

        send_cmd(smtp_socket, smtp_file, "QUIT", {221})

print("Email sent successfully!")
