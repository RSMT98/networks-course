import argparse
import json
import queue
import re
import socket
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

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


class ClientStartupError(Exception):
    def __init__(self, title: str, message: str) -> None:
        super().__init__(message)
        self.title = title
        self.message = message


class RemoteDrawingClient(tk.Tk):
    def __init__(self, args: argparse.Namespace) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(args.timeout)
            sock.connect((args.host, args.port))

            raw_response = b""
            while not raw_response.endswith(b"\n"):
                data = sock.recv(BUFFER_SIZE)
                if not data:
                    raise ConnectionError(
                        "Server closed the connection before accepting the client"
                    )

                raw_response += data

            try:
                response = json.loads(raw_response.decode("utf-8"))
                if not isinstance(response, dict):
                    raise ValueError("response is not a JSON object")
                connection_response = ConnectionResponse(response.get("response"))
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as e:
                raise ConnectionError(
                    f"Invalid server response: {raw_response!r}"
                ) from e
        except ConnectionRefusedError as e:
            sock.close()
            raise ClientStartupError(
                "Connection error",
                f"Could not connect to the drawing server at {args.host}:{args.port}.\n\n"
                "Start remote_drawing_server.py first and check that --host/--port match.",
            ) from e
        except TimeoutError as e:
            sock.close()
            raise ClientStartupError(
                "Connection timeout",
                f"Connection to the drawing server at {args.host}:{args.port} timed out.\n\n"
                "Check that the server is running and reachable.",
            ) from e
        except ConnectionError as e:
            sock.close()
            raise ClientStartupError(
                "Connection error",
                f"Could not start the drawing client.\n\n{e}",
            ) from e
        except OSError as e:
            sock.close()
            raise ClientStartupError(
                "Connection error",
                f"Network error while connecting to the drawing server at {args.host}:{args.port}.\n\n{e}",
            ) from e

        if connection_response == ConnectionResponse.BUSY:
            sock.close()
            raise ClientStartupError(
                "Server busy",
                "The server already has an active client.\n\nClose the other client or try again later.",
            )

        sock.settimeout(None)

        super().__init__()

        self.args = args
        self.connection_events: queue.Queue[str] = queue.Queue()
        self.last_point: tuple[float, float] | None = None
        self.mouse_moved = False
        self.closed = False
        self.withdraw()
        self.sock = sock

        self.status_var = tk.StringVar(value=f"Connected to {args.host}:{args.port}")

        self.title("Remote drawing client")
        self.resizable(False, False)

        top_frame = ttk.Frame(self, padding=(12, 10, 12, 8))
        top_frame.grid(row=0, column=0, sticky="ew")
        top_frame.columnconfigure(0, weight=1)

        ttk.Label(top_frame, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w"
        )
        self.clear_button = ttk.Button(
            top_frame,
            text="Clear",
            command=self.clear_canvas,
        )
        self.clear_button.grid(row=0, column=1, padx=(12, 0))

        self.canvas = tk.Canvas(
            self,
            width=CANVAS_WIDTH,
            height=CANVAS_HEIGHT,
            bg="white",
            highlightthickness=1,
            highlightbackground="#9ca3af",
        )
        self.canvas.grid(row=1, column=0, padx=12, pady=(0, 12))
        self.canvas.bind("<ButtonPress-1>", self.start_line)
        self.canvas.bind("<B1-Motion>", self.draw_line)
        self.canvas.bind("<ButtonRelease-1>", self.finish_line)

        print(f"Connected to remote drawing server {args.host}:{args.port}")
        print(f"Canvas size: {CANVAS_WIDTH}x{CANVAS_HEIGHT}")

        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.deiconify()
        threading.Thread(target=self.watch_server, daemon=True).start()
        self.after(100, self.poll_connection_events)

    def send_event(self, event: dict) -> bool:
        if self.closed:
            return False

        try:
            data = json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.sock.sendall(data + b"\n")
            return True
        except OSError as e:
            self.mark_disconnected(f"Server disconnected: {e}")
            return False

    def watch_server(self) -> None:
        while not self.closed:
            try:
                data = self.sock.recv(1)
            except OSError as e:
                if not self.closed:
                    self.connection_events.put(f"Server disconnected: {e}")
                break

            if not data:
                self.connection_events.put("Server disconnected.")
                break

    def poll_connection_events(self) -> None:
        try:
            msg = self.connection_events.get_nowait()
        except queue.Empty:
            if not self.closed:
                self.after(100, self.poll_connection_events)
            return

        self.mark_disconnected(msg)

    def mark_disconnected(self, msg: str) -> None:
        if self.closed:
            return

        self.status_var.set("Lost connection to the server.")
        print(msg, file=sys.stderr)
        self.closed = True
        self.last_point = None
        try:
            self.sock.close()
        except OSError:
            pass
        messagebox.showerror(
            "Connection lost",
            "Lost connection to the server. The client will be closed.",
            parent=self,
        )
        try:
            self.destroy()
        except tk.TclError:
            pass

    def start_line(self, event: tk.Event) -> None:
        if self.closed:
            return

        if self.send_event(
            {
                "type": EventType.START.value,
                "x": event.x,
                "y": event.y,
                "color": self.args.color,
                "line_width": self.args.line_width,
            }
        ):
            self.last_point = (event.x, event.y)
            self.mouse_moved = False

    def draw_line(self, event: tk.Event) -> None:
        if self.closed:
            return
        if self.last_point is None:
            self.start_line(event)
            return

        if not self.send_event(
            {"type": EventType.DRAW.value, "x": event.x, "y": event.y}
        ):
            self.last_point = None
            return

        self.canvas.create_line(
            self.last_point[0],
            self.last_point[1],
            event.x,
            event.y,
            fill=self.args.color,
            width=self.args.line_width,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
        )
        self.last_point = (event.x, event.y)
        self.mouse_moved = True

    def finish_line(self, _event: tk.Event | None = None) -> None:
        if not self.closed and self.last_point is not None and not self.mouse_moved:
            x, y = self.last_point
            if self.send_event({"type": EventType.DRAW.value, "x": x, "y": y}):
                radius = max(1, self.args.line_width / 2)
                self.canvas.create_oval(
                    x - radius,
                    y - radius,
                    x + radius,
                    y + radius,
                    fill=self.args.color,
                    outline=self.args.color,
                )

        if not self.closed:
            self.send_event({"type": EventType.END.value})
        self.last_point = None
        self.mouse_moved = False

    def clear_canvas(self) -> None:
        if self.closed:
            return

        if not self.send_event({"type": EventType.CLEAR.value}):
            return

        self.canvas.delete("all")
        self.last_point = None

    def close_app(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        self.destroy()


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--timeout", type=float, default=5.0)
parser.add_argument("--color", default=DEFAULT_LINE_COLOR)
parser.add_argument("--line-width", type=int, default=DEFAULT_LINE_WIDTH)
args = parser.parse_args()

if args.timeout <= 0:
    parser.error("--timeout must be greater than 0")
if re.fullmatch(COLOR_PATTERN, args.color) is None:
    parser.error("--color must be in #RRGGBB format")
if not MIN_LINE_WIDTH <= args.line_width <= MAX_LINE_WIDTH:
    parser.error(f"--line-width must be in range {MIN_LINE_WIDTH}..{MAX_LINE_WIDTH}")

try:
    RemoteDrawingClient(args).mainloop()
except ClientStartupError as e:
    print(e.message, file=sys.stderr)
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(e.title, e.message, parent=root)
    root.destroy()
    sys.exit(1)
