import argparse
import json
import os
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from speed_measurement_common import SpeedEventType, TCPMessageType

PACKET_SIZE = 1024
POLL_INTERVAL = 100
EPS = 0.000001


class TCPSpeedSender(tk.Tk):
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

        self.title("Отправитель TCP")
        self.geometry("680x320")
        self.minsize(560, 260)
        self.columnconfigure(0, weight=1)

        main_frame = ttk.Frame(self, padding=32)
        main_frame.grid(row=0, column=0, sticky="nsew")
        main_frame.columnconfigure(1, weight=1)

        ttk.Label(main_frame, text="Введите IP адрес получателя").grid(
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

        ttk.Label(main_frame, text="Введите количество пакетов для отправки").grid(
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
            width=610,
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
        self.status_var.set(f"Подключение к {host}:{port}...")
        threading.Thread(
            target=self.send_packets,
            args=(host, port, packets_count),
            daemon=True,
        ).start()

    def send_packets(self, host: str, port: int, packets_count: int) -> None:
        packets = [os.urandom(PACKET_SIZE) for _ in range(packets_count)]
        sent_bytes = 0
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                self.sock = sock
                sock.settimeout(self.args.timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.connect((host, port))

                meta_info = {
                    "type": TCPMessageType.START.value,
                    "packets": packets_count,
                    "packet_size": PACKET_SIZE,
                }
                sock.sendall(
                    json.dumps(meta_info, separators=(",", ":")).encode("utf-8") + b"\n"
                )

                started_at = time.monotonic()
                for packet_num, packet in enumerate(packets, start=1):
                    sock.sendall(packet)
                    sent_bytes += PACKET_SIZE
                    if packet_num == packets_count or packet_num % 100 == 0:
                        self.events.put(
                            {
                                "type": SpeedEventType.STATUS,
                                "text": f"Отправлено {packet_num} из {packets_count} пакетов.",
                            }
                        )

                send_duration = max(time.monotonic() - started_at, EPS)
                end_info = {
                    "type": TCPMessageType.END.value,
                    "client_send_duration": send_duration,
                    "bytes": sent_bytes,
                    "finished_at": time.time(),
                }
                sock.sendall(
                    json.dumps(end_info, separators=(",", ":")).encode("utf-8") + b"\n"
                )

                raw_response = b""
                while not raw_response.endswith(b"\n"):
                    data = sock.recv(4096)
                    if not data:
                        raise ConnectionError("Получатель не подтвердил прием данных")
                    raw_response += data

                result = json.loads(raw_response.decode("utf-8"))
                try:
                    result_type = TCPMessageType(result.get("type"))
                except ValueError as e:
                    raise ValueError(
                        f"Некорректный ответ получателя: {result!r}"
                    ) from e
                if result_type != TCPMessageType.RESULT:
                    raise ValueError(f"Некорректный ответ получателя: {result!r}")

                self.events.put(
                    {
                        "type": SpeedEventType.DONE,
                        "text": (
                            f"Получатель подтвердил {result.get('received_packets')} из {result.get('packets')} пакетов; "
                            f"потеряно {result.get('lost_packets')}; скорость {result.get('speed')} ({result.get('speed_source')})."
                        ),
                    }
                )
        except (OSError, ValueError, json.JSONDecodeError, ConnectionError) as e:
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
                    messagebox.showerror("Ошибка TCP", text, parent=self)

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
parser.add_argument("--port", type=int, default=8080)
parser.add_argument("--packets", type=int, default=20000)
parser.add_argument("--timeout", type=float, default=5.0)
args = parser.parse_args()

if args.timeout <= 0:
    parser.error("--timeout must be greater than 0")

TCPSpeedSender(args).mainloop()
