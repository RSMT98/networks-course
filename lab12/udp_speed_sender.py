import argparse
import json
import os
import queue
import socket
import struct
import threading
import time
import tkinter as tk
import uuid
from tkinter import messagebox, ttk

from speed_measurement_common import SpeedEventType, UDPMessageType

PACKET_SIZE = 1024
END_REPEATS = 3
PACKET_HEADER_FORMAT = "!HQ"
POLL_INTERVAL = 100
EPS = 0.000001


class UDPSpeedSender(tk.Tk):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()

        self.args = args
        self.events: queue.Queue[dict] = queue.Queue()
        self.sock: socket.socket | None = None
        self.closed = False

        self.host_var = tk.StringVar(value=args.host)
        self.port_var = tk.StringVar(value=str(args.port))
        self.packets_var = tk.StringVar(value=str(args.packets))
        self.status_var = tk.StringVar(value="Введите параметры и нажмите Отправить.")

        self.title("Отправитель UDP")
        self.geometry("730x330")
        self.minsize(600, 270)
        self.columnconfigure(0, weight=1)

        main_frame = ttk.Frame(self, padding=32)
        main_frame.grid(row=0, column=0, sticky="nsew")
        main_frame.columnconfigure(1, weight=1)

        ttk.Label(main_frame, text="Введите IP получателя").grid(
            row=0, column=0, sticky="w", pady=(0, 14)
        )
        ttk.Entry(main_frame, textvariable=self.host_var).grid(
            row=0, column=1, sticky="ew", padx=(16, 0), pady=(0, 14)
        )

        ttk.Label(main_frame, text="Введите порт получателя").grid(
            row=1, column=0, sticky="w", pady=(0, 14)
        )
        ttk.Entry(main_frame, textvariable=self.port_var).grid(
            row=1, column=1, sticky="ew", padx=(16, 0), pady=(0, 14)
        )

        ttk.Label(main_frame, text="Введите число пакетов для отправки").grid(
            row=2, column=0, sticky="w", pady=(0, 20)
        )
        ttk.Entry(main_frame, textvariable=self.packets_var).grid(
            row=2, column=1, sticky="ew", padx=(16, 0), pady=(0, 20)
        )

        self.send_button = ttk.Button(
            main_frame,
            text="Отправить",
            command=self.send_data,
        )
        self.send_button.grid(row=3, column=0, columnspan=2)

        status_message = tk.Message(
            main_frame,
            textvariable=self.status_var,
            width=660,
            anchor="w",
            justify="left",
        )
        status_message.grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(22, 0),
        )

        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.after(POLL_INTERVAL, self.poll_events)

    def send_data(self) -> None:
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get())
            packets_count = int(self.packets_var.get())
            if not host:
                raise ValueError("IP адрес получателя не может быть пустым")
            if not 1 <= port <= 65535:
                raise ValueError("Порт должен быть в диапазоне 1..65535")
            if packets_count <= 0:
                raise ValueError("Количество пакетов должно быть больше 0")
        except ValueError as e:
            messagebox.showerror("Ошибка ввода", str(e), parent=self)
            return

        self.send_button.configure(state="disabled")
        self.status_var.set(f"Отправка UDP пакетов на {host}:{port}...")
        threading.Thread(
            target=self.send_packets,
            args=(host, port, packets_count),
            daemon=True,
        ).start()

    def send_packets(self, host: str, port: int, packets_count: int) -> None:
        transfer_id = uuid.uuid4().hex
        packets = []
        for packet_num in range(packets_count):
            meta_info = {
                "type": UDPMessageType.DATA.value,
                "transfer_id": transfer_id,
                "seq": packet_num,
                "packets": packets_count,
                "packet_size": PACKET_SIZE,
            }
            meta = json.dumps(
                meta_info,
                separators=(",", ":"),
            ).encode("utf-8")
            packets.append((meta, os.urandom(PACKET_SIZE)))

        sent_bytes = 0
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                self.sock = sock
                server_addr = (host, port)
                started_at = time.monotonic()
                for packet_num, (meta, payload) in enumerate(packets, start=1):
                    client_elapsed_ns = max(
                        1,
                        int((time.monotonic() - started_at) * 1_000_000_000),
                    )
                    packet = (
                        struct.pack(
                            PACKET_HEADER_FORMAT,
                            len(meta),
                            client_elapsed_ns,
                        )
                        + meta
                        + payload
                    )
                    sock.sendto(packet, server_addr)
                    sent_bytes += PACKET_SIZE
                    if self.args.delay > 0:
                        time.sleep(self.args.delay)
                    if packet_num == packets_count or packet_num % 100 == 0:
                        self.events.put(
                            {
                                "type": SpeedEventType.STATUS,
                                "text": f"Отправлено {packet_num} из {packets_count} пакетов.",
                            }
                        )

                duration = max(time.monotonic() - started_at, EPS)
                end_info = {
                    "type": UDPMessageType.END.value,
                    "transfer_id": transfer_id,
                    "packets": packets_count,
                    "duration": duration,
                    "bytes": sent_bytes,
                    "finished_at": time.time(),
                }
                end_meta = json.dumps(
                    end_info,
                    separators=(",", ":"),
                ).encode("utf-8")
                end_packet = struct.pack("!H", len(end_meta)) + end_meta
                for _ in range(END_REPEATS):
                    sock.sendto(end_packet, server_addr)

                self.events.put(
                    {
                        "type": SpeedEventType.DONE,
                        "text": (
                            f"Готово: отправлено {packets_count} пакетов, {sent_bytes} байт за {duration:.4f} с."
                        ),
                    }
                )
        except OSError as e:
            self.events.put(
                {"type": SpeedEventType.ERROR, "text": f"Ошибка отправки: {e}"}
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
            text = str(event.get("text", ""))
            self.status_var.set(text)
            if event_type in {SpeedEventType.DONE, SpeedEventType.ERROR}:
                self.send_button.configure(state="normal")
                if event_type == SpeedEventType.ERROR:
                    messagebox.showerror("Ошибка UDP", text, parent=self)

        if not self.closed:
            self.after(POLL_INTERVAL, self.poll_events)

    def close_app(self) -> None:
        self.closed = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.destroy()


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8888)
parser.add_argument("--packets", type=int, default=20000)
parser.add_argument("--delay", type=float, default=0.0)
args = parser.parse_args()

if args.delay < 0:
    parser.error("--delay must be greater than or equal to 0")

UDPSpeedSender(args).mainloop()
