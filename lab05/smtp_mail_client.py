import argparse
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

HOST = "smtp.mail.ru"
PORT = 465
SENDER_EMAIL = "<your email here>"
SENDER_PASSWORD = "<your password here>"
EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)


parser = argparse.ArgumentParser()
parser.add_argument("--to", required=True)
parser.add_argument("--subject", required=True)
parser.add_argument("--format", required=True, choices=["txt", "html"])
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

msg = EmailMessage()
msg["From"] = SENDER_EMAIL
msg["To"] = args.to
msg["Subject"] = args.subject
if args.format == "txt":
    msg.set_content(body)
else:
    msg.set_content(body, subtype="html")

with smtplib.SMTP_SSL(HOST, PORT, timeout=10) as server:
    server.login(SENDER_EMAIL, SENDER_PASSWORD)
    server.send_message(msg)

print("Email sent successfully!")
