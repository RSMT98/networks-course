import argparse
import json
import queue
import re
import socket
import threading
import tkinter as tk
from tkinter import ttk

from remote_drawing_common import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    COLOR_PATTERN,
    DEFAULT_LINE_COLOR,
    DEFAULT_LINE_WIDTH,
    MAX_LINE_WIDTH,
    MIN_LINE_WIDTH,
    ConnectionResponse,
    EventType,
)

BUFFER_SIZE = 4096


class RemoteDrawingServer(tk.Tk):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()

        self.args = args
        self.events: queue.Queue[dict] = queue.Queue()
        self.stop_event = threading.Event()
        self.active_client: str | None = None
        self.active_client_lock = threading.Lock()
        self.last_point: tuple[float, float] | None = None
        self.line_color = DEFAULT_LINE_COLOR
        self.line_width = DEFAULT_LINE_WIDTH

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((args.host, args.port))
        self.sock.listen()
        self.sock.settimeout(0.5)

        self.status_var = tk.StringVar(
            value=f"Server is running on {args.host}:{args.port}"
        )

        self.title("Remote drawing server")
        self.resizable(False, False)

        top_frame = ttk.Frame(self, padding=(12, 10, 12, 8))
        top_frame.grid(row=0, column=0, sticky="ew")
        top_frame.columnconfigure(0, weight=1)

        ttk.Label(top_frame, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w"
        )

        self.canvas = tk.Canvas(
            self,
            width=CANVAS_WIDTH,
            height=CANVAS_HEIGHT,
            bg="white",
            highlightthickness=1,
            highlightbackground="#9ca3af",
        )
        self.canvas.grid(row=1, column=0, padx=12, pady=(0, 12))

        print(f"Remote drawing server is running on {args.host}:{args.port}")
        print(f"Canvas size: {CANVAS_WIDTH}x{CANVAS_HEIGHT}")

        self.protocol("WM_DELETE_WINDOW", self.close_app)
        threading.Thread(target=self.accept_clients, daemon=True).start()
        self.after(args.poll_interval, self.process_events)

    def accept_clients(self) -> None:
        while not self.stop_event.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            client = f"{addr[0]}:{addr[1]}"
            with self.active_client_lock:
                if self.active_client is not None:
                    response = {"response": ConnectionResponse.BUSY.value}
                    try:
                        conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
                    except OSError as e:
                        print(f"Could not reject client {client}: {e}")
                    conn.close()
                    print(
                        f"Rejected client {client}: active client is {self.active_client}"
                    )
                    continue

                self.active_client = client

            response = {"response": ConnectionResponse.ACCEPTED.value}
            try:
                conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
            except OSError as e:
                print(f"Could not accept client {client}: {e}")
                conn.close()
                with self.active_client_lock:
                    if self.active_client == client:
                        self.active_client = None
                continue

            threading.Thread(
                target=self.handle_client,
                args=(conn, client),
                daemon=True,
            ).start()

    def handle_client(self, conn: socket.socket, client: str) -> None:
        try:
            self.events.put(
                {"type": EventType.STATUS, "text": f"Client connected: {client}"}
            )
            print(f"Client connected: {client}")

            with conn:
                conn.settimeout(0.5)
                raw_data = b""
                while not self.stop_event.is_set():
                    try:
                        data = conn.recv(BUFFER_SIZE)
                    except socket.timeout:
                        continue
                    except OSError:
                        break

                    if not data:
                        break

                    raw_data += data
                    while b"\n" in raw_data:
                        line, raw_data = raw_data.split(b"\n", 1)
                        if not line:
                            continue

                        try:
                            event = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as e:
                            print(f"Invalid event from {client}: {e}")
                            continue

                        if isinstance(event, dict):
                            event["client"] = client
                            self.events.put(event)

            self.events.put(
                {"type": EventType.STATUS, "text": f"Client disconnected: {client}"}
            )
            self.events.put({"type": EventType.CLEAR})
            print(f"Client disconnected: {client}")
        finally:
            with self.active_client_lock:
                if self.active_client == client:
                    self.active_client = None

    def process_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            try:
                event_type = EventType(event.get("type"))
            except (TypeError, ValueError):
                print(f"Unknown event type: {event.get('type')!r}")
                continue

            if event_type == EventType.STATUS:
                self.status_var.set(str(event.get("text", "")))
                continue
            if event_type == EventType.CLEAR:
                self.clear_canvas()
                continue
            if event_type == EventType.END:
                self.last_point = None
                continue

            x = event.get("x")
            y = event.get("y")
            if type(x) not in {int, float} or type(y) not in {int, float}:
                continue

            if event_type == EventType.START:
                color = event.get("color")
                line_width = event.get("line_width")
                self.last_point = (x, y)
                if isinstance(color, str) and re.fullmatch(COLOR_PATTERN, color):
                    self.line_color = color
                else:
                    self.line_color = DEFAULT_LINE_COLOR
                    if color is not None:
                        print(f"Invalid line color from client: {color!r}")

                if (
                    type(line_width) is int
                    and MIN_LINE_WIDTH <= line_width <= MAX_LINE_WIDTH
                ):
                    self.line_width = line_width
                else:
                    self.line_width = DEFAULT_LINE_WIDTH
                    if line_width is not None:
                        print(f"Invalid line width from client: {line_width!r}")
            elif event_type == EventType.DRAW and self.last_point is not None:
                if self.last_point == (x, y):
                    radius = max(1, self.line_width / 2)
                    self.canvas.create_oval(
                        x - radius,
                        y - radius,
                        x + radius,
                        y + radius,
                        fill=self.line_color,
                        outline=self.line_color,
                    )
                else:
                    self.canvas.create_line(
                        self.last_point[0],
                        self.last_point[1],
                        x,
                        y,
                        fill=self.line_color,
                        width=self.line_width,
                        capstyle=tk.ROUND,
                        joinstyle=tk.ROUND,
                    )
                self.last_point = (x, y)

        if not self.stop_event.is_set():
            self.after(self.args.poll_interval, self.process_events)

    def clear_canvas(self) -> None:
        self.canvas.delete("all")
        self.last_point = None

    def close_app(self) -> None:
        self.stop_event.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.destroy()


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--poll-interval", type=int, default=30)
args = parser.parse_args()

if args.poll_interval <= 0:
    parser.error("--poll-interval must be greater than 0")

RemoteDrawingServer(args).mainloop()
