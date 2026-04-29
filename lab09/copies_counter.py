import argparse
import json
import select
import socket
import time
import tkinter as tk
import uuid
from dataclasses import dataclass
from enum import Enum
from tkinter import ttk
from typing import Optional

APP_NAME = "copies_counter"
BUFFER_SIZE = 4096


class MessageType(str, Enum):
    STARTED = "started"
    ALIVE = "alive"
    HEARTBEAT = "heartbeat"
    STOPPED = "stopped"


@dataclass
class CopyState:
    addr: tuple[str, int]
    last_seen_at: float
    last_message: MessageType


def set_reuse_options(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass


def format_addr(addr: tuple[str, int]) -> str:
    return f"{addr[0]}:{addr[1]}"


def parse_message(data: bytes) -> Optional[dict]:
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(msg, dict) or msg.get("app") != APP_NAME:
        return None
    if not isinstance(msg.get("id"), str):
        return None
    try:
        msg["type"] = MessageType(msg.get("type"))
    except ValueError:
        return None
    if not isinstance(msg.get("port"), int):
        return None

    return msg


class CopiesCounterGUI(tk.Tk):
    def __init__(
        self,
        args: argparse.Namespace,
        instance_id: str,
        visible_host: str,
    ) -> None:
        super().__init__()

        self.args = args
        self.instance_id = instance_id
        self.visible_host = visible_host
        self.peers: dict[str, CopyState] = {}
        self.copy_timeout = args.broadcast_interval * args.missed_intervals
        self.closed = False

        self.app_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.discovery_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        set_reuse_options(self.app_sock)
        self.app_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.app_sock.bind((self.args.host, self.args.port))
        _, self.local_port = self.app_sock.getsockname()

        set_reuse_options(self.discovery_sock)
        self.discovery_sock.bind(("", self.args.discovery_port))

        self.app_sock.setblocking(False)
        self.discovery_sock.setblocking(False)

        self.local_addr = (self.visible_host, self.local_port)
        self.next_broadcast_at = time.monotonic() + args.broadcast_interval

        self.total_var = tk.StringVar()
        self.local_addr_var = tk.StringVar(value=format_addr(self.local_addr))
        self.instance_var = tk.StringVar(value=self.instance_id[:8])
        self.status_var = tk.StringVar()

        self.title("Copies counter")
        self.geometry("560x420")
        self.minsize(460, 340)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_top_frame()
        self._build_copies_list()
        self._build_log()
        self._build_status_bar()

        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.bind("<Escape>", lambda _: self.close_app())

        self.log(f"Started as {self.instance_id[:8]} on {format_addr(self.local_addr)}")
        self.log(
            f"Searching for other copies via UDP broadcast at {args.broadcast_host}:{args.discovery_port}; heartbeat every {args.broadcast_interval:.1f}s; peer timeout {self.copy_timeout:.1f}s"
        )
        self.send_message(
            (self.args.broadcast_host, self.args.discovery_port), MessageType.STARTED
        )
        self.refresh_copies()
        self.after(self._poll_delay_ms(), self.poll_network)

    def _build_top_frame(self) -> None:
        top_frame = ttk.Frame(self, padding=12)
        top_frame.grid(row=0, column=0, sticky="ew")
        top_frame.columnconfigure(1, weight=1)

        ttk.Label(top_frame, text="Local:").grid(row=0, column=0, sticky="w")
        ttk.Label(top_frame, textvariable=self.local_addr_var).grid(
            row=0, column=1, sticky="w", padx=(6, 18)
        )

        ttk.Label(top_frame, text="ID:").grid(row=0, column=2, sticky="w")
        ttk.Label(top_frame, textvariable=self.instance_var).grid(
            row=0, column=3, sticky="w", padx=(6, 18)
        )

        ttk.Label(top_frame, textvariable=self.total_var).grid(
            row=0, column=4, sticky="e", padx=(0, 12)
        )
        ttk.Button(
            top_frame,
            text="Broadcast",
            command=lambda: self.send_message(
                (self.args.broadcast_host, self.args.discovery_port),
                MessageType.HEARTBEAT,
            ),
        ).grid(row=0, column=5)

    def _build_copies_list(self) -> None:
        list_frame = ttk.Frame(self, padding=(12, 0, 12, 8))
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        columns = ("addr", "message", "last_seen")
        self.copies_tree = ttk.Treeview(
            list_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.copies_tree.heading("addr", text="Address")
        self.copies_tree.heading("message", text="Last message")
        self.copies_tree.heading("last_seen", text="Last seen")
        self.copies_tree.column("addr", width=180, anchor="w")
        self.copies_tree.column("message", width=110, anchor="w")
        self.copies_tree.column("last_seen", width=90, anchor="e")
        self.copies_tree.grid(row=0, column=0, sticky="nsew")

        tree_scroll = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.copies_tree.yview,
        )
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.copies_tree.configure(yscrollcommand=tree_scroll.set)

    def _build_log(self) -> None:
        log_frame = ttk.Frame(self, padding=(12, 0, 12, 8))
        log_frame.grid(row=2, column=0, sticky="ew")
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=7, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="ew")

        log_scroll = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def _build_status_bar(self) -> None:
        status_frame = ttk.Frame(self, padding=(12, 0, 12, 10))
        status_frame.grid(row=3, column=0, sticky="ew")
        status_frame.columnconfigure(0, weight=1)

        ttk.Label(status_frame, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w"
        )

    def _poll_delay_ms(self) -> int:
        return max(50, int(self.args.select_timeout * 1000))

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        print(msg)

    def send_message(self, addr: tuple[str, int], msg_type: MessageType) -> None:
        try:
            self.app_sock.sendto(
                json.dumps(
                    {
                        "app": APP_NAME,
                        "id": self.instance_id,
                        "type": msg_type.value,
                        "port": self.local_port,
                        "time": time.time(),
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                addr,
            )
        except OSError as e:
            self.log(f"Send error to {format_addr(addr)}: {e}")

    def handle_message(self, data: bytes, source_addr: tuple[str, int]) -> bool:
        msg = parse_message(data)
        if msg is None or msg["id"] == self.instance_id:
            return False

        peer_id = msg["id"]
        msg_type = msg["type"]
        peer_addr = (source_addr[0], msg["port"])

        if msg_type == MessageType.STOPPED:
            if peer_id in self.peers:
                self.log(f"Copy stopped: {format_addr(self.peers[peer_id].addr)}")
                del self.peers[peer_id]
                return True

            return False

        is_new = peer_id not in self.peers
        self.peers[peer_id] = CopyState(
            addr=peer_addr,
            last_seen_at=time.monotonic(),
            last_message=msg_type,
        )
        if is_new:
            self.log(f"New copy detected: {format_addr(peer_addr)}")

        if msg_type == MessageType.STARTED:
            self.send_message(peer_addr, MessageType.ALIVE)

        return True

    def remove_expired(self) -> bool:
        now = time.monotonic()
        changed = False
        for peer_id, state in list(self.peers.items()):
            if now - state.last_seen_at < self.copy_timeout:
                continue

            self.log(f"Copy timed out: {format_addr(state.addr)}")
            del self.peers[peer_id]
            changed = True

        return changed

    def refresh_copies(self) -> None:
        now = time.monotonic()
        rows = [
            (
                format_addr(self.local_addr),
                "local",
                "-",
            )
        ]
        for state in sorted(self.peers.values(), key=lambda item: item.addr):
            age = now - state.last_seen_at
            rows.append(
                (
                    format_addr(state.addr),
                    state.last_message.value,
                    f"{age:.1f}s",
                )
            )

        self.copies_tree.delete(*self.copies_tree.get_children())
        for row in rows:
            self.copies_tree.insert("", "end", values=row)

        self.total_var.set(f"Total: {len(rows)}")
        self.status_var.set(
            f"Listening on {format_addr(self.local_addr)}; "
            f"{len(self.peers)} remote copy/copies"
        )

    def poll_network(self) -> None:
        if self.closed:
            return

        state_changed = False
        readable, _, _ = select.select([self.app_sock, self.discovery_sock], [], [], 0)
        for ready_sock in readable:
            while True:
                try:
                    data, source_addr = ready_sock.recvfrom(BUFFER_SIZE)
                except BlockingIOError:
                    break
                except OSError as e:
                    self.log(f"Receive error: {e}")
                    break

                if self.handle_message(data, source_addr):
                    state_changed = True

        if self.remove_expired():
            state_changed = True

        now = time.monotonic()
        if now >= self.next_broadcast_at:
            self.send_message(
                (self.args.broadcast_host, self.args.discovery_port),
                MessageType.HEARTBEAT,
            )
            self.next_broadcast_at = now + self.args.broadcast_interval

        if state_changed or self.copies_tree.get_children():
            self.refresh_copies()

        self.after(self._poll_delay_ms(), self.poll_network)

    def close_app(self) -> None:
        if self.closed:
            return

        self.closed = True
        for state in list(self.peers.values()):
            self.send_message(state.addr, MessageType.STOPPED)
        self.send_message(
            (self.args.broadcast_host, self.args.discovery_port), MessageType.STOPPED
        )
        self.app_sock.close()
        self.discovery_sock.close()
        self.destroy()


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--port", type=int, default=0)
parser.add_argument("--discovery-port", type=int, default=8888)
parser.add_argument("--broadcast-host", default="255.255.255.255")
parser.add_argument("--broadcast-interval", type=float, default=2.0)
parser.add_argument("--missed-intervals", type=int, default=3)
parser.add_argument("--select-timeout", type=float, default=0.2)
args = parser.parse_args()

if args.broadcast_interval <= 0:
    parser.error("--broadcast-interval must be greater than 0")
if args.missed_intervals <= 0:
    parser.error("--missed-intervals must be greater than 0")
if args.select_timeout <= 0:
    parser.error("--select-timeout must be greater than 0")

instance_id = uuid.uuid4().hex
if args.host in {"", "0.0.0.0"}:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        visible_host = sock.getsockname()[0]
    except OSError:
        visible_host = socket.gethostbyname(socket.gethostname())
    finally:
        sock.close()
else:
    visible_host = args.host

app = CopiesCounterGUI(args, instance_id, visible_host)
app.mainloop()
