import argparse
import json
import queue
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from speed_measurement_common import SpeedEventType, UDPMessageType

BUFFER_SIZE = 65535
PACKET_HEADER_FORMAT = "!HQ"
PACKET_HEADER_SIZE = struct.calcsize(PACKET_HEADER_FORMAT)
POLL_INTERVAL = 100
EPS = 0.000001


class UDPSpeedReceiver(tk.Tk):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()

        self.args = args
        self.events: queue.Queue[dict] = queue.Queue()
        self.sock: socket.socket | None = None
        self.stop_event = threading.Event()
        self.closed = False

        self.host_var = tk.StringVar(value=args.host)
        self.port_var = tk.StringVar(value=str(args.port))
        self.received_var = tk.StringVar(value="-")
        self.speed_var = tk.StringVar(value="-")
        self.lost_var = tk.StringVar(value="-")
        self.status_var = tk.StringVar(value="Введите параметры и нажмите Получить.")

        self.title("Получатель UDP")
        self.geometry("730x400")
        self.minsize(600, 330)
        self.columnconfigure(0, weight=1)

        main_frame = ttk.Frame(self, padding=32)
        main_frame.grid(row=0, column=0, sticky="nsew")
        main_frame.columnconfigure(1, weight=1)

        ttk.Label(main_frame, text="Введите локальный IP").grid(
            row=0, column=0, sticky="w", pady=(0, 14)
        )
        ttk.Entry(main_frame, textvariable=self.host_var).grid(
            row=0, column=1, sticky="ew", padx=(16, 0), pady=(0, 14)
        )

        ttk.Label(main_frame, text="Введите локальный порт").grid(
            row=1, column=0, sticky="w", pady=(0, 14)
        )
        ttk.Entry(main_frame, textvariable=self.port_var).grid(
            row=1, column=1, sticky="ew", padx=(16, 0), pady=(0, 14)
        )

        ttk.Label(main_frame, text="Число полученных пакетов").grid(
            row=2, column=0, sticky="w", pady=(0, 14)
        )
        ttk.Entry(main_frame, textvariable=self.received_var, state="readonly").grid(
            row=2, column=1, sticky="ew", padx=(16, 0), pady=(0, 14)
        )

        ttk.Label(main_frame, text="Скорость соединения").grid(
            row=3, column=0, sticky="w", pady=(0, 14)
        )
        ttk.Entry(main_frame, textvariable=self.speed_var, state="readonly").grid(
            row=3, column=1, sticky="ew", padx=(16, 0), pady=(0, 14)
        )

        ttk.Label(main_frame, text="Потеряно пакетов").grid(
            row=4, column=0, sticky="w", pady=(0, 22)
        )
        ttk.Entry(main_frame, textvariable=self.lost_var, state="readonly").grid(
            row=4, column=1, sticky="ew", padx=(16, 0), pady=(0, 22)
        )

        self.receive_button = ttk.Button(
            main_frame,
            text="Получить",
            command=self.start_receiving,
        )
        self.receive_button.grid(row=5, column=0, columnspan=2)

        ttk.Label(main_frame, textvariable=self.status_var).grid(
            row=6,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(22, 0),
        )

        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.after(POLL_INTERVAL, self.poll_events)

    def start_receiving(self) -> None:
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get())
            if not host:
                raise ValueError("IP адрес не может быть пустым")
            if not 1 <= port <= 65535:
                raise ValueError("Порт должен быть в диапазоне 1..65535")
        except ValueError as e:
            messagebox.showerror("Ошибка ввода", str(e), parent=self)
            return

        self.stop_event.clear()
        self.receive_button.configure(state="disabled")
        self.received_var.set("-")
        self.speed_var.set("-")
        self.lost_var.set("-")
        self.status_var.set(f"Ожидание UDP пакетов на {host}:{port}...")
        threading.Thread(
            target=self.receive_data,
            args=(host, port),
            daemon=True,
        ).start()

    def receive_data(self, host: str, port: int) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                self.sock = sock
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
                sock.settimeout(0.2)

                transfer_id = None
                expected_packets = 0
                received_packets: set[int] = set()
                received_bytes = 0
                first_packet_at = None
                last_packet_at = None
                end_received_at = None
                duration = None
                last_client_elapsed = None
                client_addr = None

                wait_deadline = time.monotonic() + self.args.wait_timeout
                while not self.stop_event.is_set():
                    try:
                        data, addr = sock.recvfrom(BUFFER_SIZE)
                    except socket.timeout:
                        now = time.monotonic()
                        if transfer_id is None and now >= wait_deadline:
                            raise TimeoutError(
                                f"UDP-пакеты не получены за {self.args.wait_timeout:.1f} с"
                            )
                        if end_received_at is not None:
                            if now - end_received_at >= self.args.done_timeout:
                                break
                        elif last_packet_at is not None:
                            if now - last_packet_at >= self.args.idle_timeout:
                                break
                        continue

                    if len(data) < 2:
                        continue

                    client_elapsed = None
                    meta_size = struct.unpack("!H", data[:2])[0]
                    meta_start = 2
                    if len(data) >= PACKET_HEADER_SIZE:
                        header_meta_size, raw_client_elapsed = struct.unpack(
                            PACKET_HEADER_FORMAT,
                            data[:PACKET_HEADER_SIZE],
                        )
                        if (
                            header_meta_size == meta_size
                            and raw_client_elapsed > 0
                            and len(data) >= PACKET_HEADER_SIZE + header_meta_size
                            and data[PACKET_HEADER_SIZE : PACKET_HEADER_SIZE + 1]
                            == b"{"
                        ):
                            client_elapsed = raw_client_elapsed / 1_000_000_000
                            meta_start = PACKET_HEADER_SIZE

                    if len(data) < meta_start + meta_size:
                        continue

                    try:
                        meta_info = json.loads(
                            data[meta_start : meta_start + meta_size].decode("utf-8")
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue

                    packet_transfer_id = meta_info.get("transfer_id")
                    if not isinstance(packet_transfer_id, str):
                        continue

                    try:
                        packet_type = UDPMessageType(meta_info.get("type"))
                    except ValueError:
                        continue
                    if transfer_id is None:
                        if packet_type not in {UDPMessageType.DATA, UDPMessageType.END}:
                            continue

                        transfer_id = packet_transfer_id
                        expected_packets = 0
                        received_packets.clear()
                        received_bytes = 0
                        first_packet_at = None
                        last_packet_at = None
                        end_received_at = None
                        duration = None
                        last_client_elapsed = None
                        client_addr = addr
                        self.events.put(
                            {
                                "type": SpeedEventType.STATUS,
                                "text": f"Прием UDP передачи от {addr[0]}:{addr[1]}.",
                            }
                        )
                    elif packet_transfer_id != transfer_id:
                        continue

                    if client_addr is not None and addr != client_addr:
                        continue

                    if packet_type == UDPMessageType.DATA:
                        try:
                            seq = int(meta_info["seq"])
                            packets_count = int(meta_info["packets"])
                        except (KeyError, TypeError, ValueError):
                            continue

                        if packets_count <= 0 or seq < 0 or seq >= packets_count:
                            continue

                        expected_packets = packets_count
                        now = time.monotonic()
                        if first_packet_at is None:
                            first_packet_at = now
                        last_packet_at = now

                        if seq not in received_packets:
                            received_packets.add(seq)
                            received_bytes += len(data) - meta_start - meta_size
                            if client_elapsed is not None:
                                last_client_elapsed = max(
                                    last_client_elapsed or 0.0,
                                    client_elapsed,
                                )

                        if (
                            len(received_packets) == expected_packets
                            and end_received_at is not None
                        ):
                            break
                    elif packet_type == UDPMessageType.END:
                        if expected_packets == 0:
                            try:
                                expected_packets = int(meta_info["packets"])
                            except (KeyError, TypeError, ValueError):
                                expected_packets = 0

                        try:
                            raw_duration = float(meta_info["duration"])
                            if raw_duration > 0:
                                duration = raw_duration
                        except (KeyError, TypeError, ValueError):
                            duration = None
                        end_received_at = time.monotonic()
                        if (
                            expected_packets
                            and len(received_packets) == expected_packets
                        ):
                            break

                if transfer_id is None:
                    raise TimeoutError("Передача не была получена")

                if duration is None or duration <= 0:
                    if last_client_elapsed is not None:
                        duration = max(last_client_elapsed, EPS)
                    elif first_packet_at is not None and last_packet_at is not None:
                        duration = max(last_packet_at - first_packet_at, EPS)
                    else:
                        duration = EPS
                else:
                    duration = max(duration, EPS)

                lost_packets = max(0, expected_packets - len(received_packets))
                speed = received_bytes / duration
                speed_text = f"{speed:.2f} B/s"
                if speed >= 1024 * 1024:
                    speed_text = f"{speed / (1024 * 1024):.2f} MB/s"
                elif speed >= 1024:
                    speed_text = f"{speed / 1024:.2f} KB/s"

                self.events.put(
                    {
                        "type": SpeedEventType.RESULT,
                        "received": f"{len(received_packets)} из {expected_packets}",
                        "speed": speed_text,
                        "lost": str(lost_packets),
                        "text": (
                            f"Получено {received_bytes} байт, "
                            f"потеряно {lost_packets} пакетов."
                        ),
                    }
                )
        except (OSError, TimeoutError) as e:
            self.events.put(
                {"type": SpeedEventType.ERROR, "text": f"Ошибка приема: {e}"}
            )
        finally:
            self.sock = None

    def poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            event_type = event.get("type")
            if event_type == SpeedEventType.RESULT:
                self.received_var.set(str(event.get("received", "-")))
                self.speed_var.set(str(event.get("speed", "-")))
                self.lost_var.set(str(event.get("lost", "-")))
                self.status_var.set(str(event.get("text", "")))
                self.receive_button.configure(state="normal")
            else:
                text = str(event.get("text", ""))
                self.status_var.set(text)
                if event_type == SpeedEventType.ERROR:
                    self.receive_button.configure(state="normal")
                    messagebox.showerror("Ошибка UDP", text, parent=self)

        if not self.closed:
            self.after(POLL_INTERVAL, self.poll_events)

    def close_app(self) -> None:
        self.closed = True
        self.stop_event.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.destroy()


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--idle-timeout", type=float, default=2.0)
parser.add_argument("--done-timeout", type=float, default=0.3)
parser.add_argument("--wait-timeout", type=float, default=30.0)
args = parser.parse_args()

if args.idle_timeout <= 0:
    parser.error("--idle-timeout must be greater than 0")
if args.done_timeout <= 0:
    parser.error("--done-timeout must be greater than 0")
if args.wait_timeout <= 0:
    parser.error("--wait-timeout must be greater than 0")

UDPSpeedReceiver(args).mainloop()
