import argparse
import json
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from speed_measurement_common import SpeedEventType, TCPMessageType

BUFFER_SIZE = 65536
HEADER_LIMIT = 8192
POLL_INTERVAL = 100
EPS = 0.000001


class TCPSpeedReceiver(tk.Tk):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()

        self.args = args
        self.events: queue.Queue[dict] = queue.Queue()
        self.server_sock: socket.socket | None = None
        self.stop_event = threading.Event()
        self.closed = False

        self.host_var = tk.StringVar(value=args.host)
        self.port_var = tk.StringVar(value=str(args.port))
        self.speed_var = tk.StringVar(value="-")
        self.received_var = tk.StringVar(value="-")
        self.lost_var = tk.StringVar(value="-")
        self.status_var = tk.StringVar(value="Введите параметры и нажмите Получить.")

        self.title("Получатель TCP")
        self.geometry("680x390")
        self.minsize(560, 320)
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

        ttk.Label(main_frame, text="Скорость передачи").grid(
            row=2, column=0, sticky="w", pady=(0, 14)
        )
        ttk.Entry(main_frame, textvariable=self.speed_var, state="readonly").grid(
            row=2, column=1, sticky="ew", padx=(16, 0), pady=(0, 14)
        )

        ttk.Label(main_frame, text="Число полученных пакетов").grid(
            row=3, column=0, sticky="w", pady=(0, 14)
        )
        ttk.Entry(main_frame, textvariable=self.received_var, state="readonly").grid(
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
        self.speed_var.set("-")
        self.received_var.set("-")
        self.lost_var.set("-")
        self.status_var.set(f"Ожидание TCP подключения на {host}:{port}...")
        threading.Thread(
            target=self.receive_data,
            args=(host, port),
            daemon=True,
        ).start()

    def receive_data(self, host: str, port: int) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
                self.server_sock = server_sock
                server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server_sock.bind((host, port))
                server_sock.listen(1)
                server_sock.settimeout(0.2)

                conn = None
                client_addr = None
                wait_deadline = time.monotonic() + self.args.wait_timeout
                while not self.stop_event.is_set():
                    try:
                        conn, client_addr = server_sock.accept()
                        break
                    except socket.timeout:
                        if time.monotonic() >= wait_deadline:
                            raise TimeoutError(
                                f"Клиент не подключился за {self.args.wait_timeout:.1f} с"
                            )
                        continue

                if conn is None or client_addr is None:
                    return

                self.events.put(
                    {
                        "type": SpeedEventType.STATUS,
                        "text": f"Подключился клиент {client_addr[0]}:{client_addr[1]}.",
                    }
                )
                with conn:
                    conn.settimeout(self.args.timeout)
                    raw_header = b""
                    while b"\n" not in raw_header:
                        if len(raw_header) > HEADER_LIMIT:
                            raise ValueError("Слишком длинный заголовок передачи")
                        data = conn.recv(BUFFER_SIZE)
                        if not data:
                            raise ConnectionError(
                                "Клиент закрыл соединение до заголовка"
                            )
                        raw_header += data

                    header_line, data_buffer = raw_header.split(b"\n", 1)
                    meta_info = json.loads(header_line.decode("utf-8"))
                    try:
                        meta_type = TCPMessageType(meta_info.get("type"))
                    except ValueError as e:
                        raise ValueError(
                            "Первое сообщение не является стартовым"
                        ) from e
                    if meta_type != TCPMessageType.START:
                        raise ValueError("Первое сообщение не является стартовым")

                    packets_count = int(meta_info["packets"])
                    packet_size = int(meta_info["packet_size"])
                    if packets_count <= 0 or packet_size <= 0:
                        raise ValueError("Некорректные параметры передачи")

                    expected_bytes = packets_count * packet_size
                    received_bytes = 0
                    received_started_at = None
                    received_finished_at = time.monotonic()
                    result_note = ""
                    while received_bytes < expected_bytes:
                        if data_buffer:
                            if received_started_at is None:
                                received_started_at = time.monotonic()
                            chunk_size = min(
                                len(data_buffer),
                                expected_bytes - received_bytes,
                            )
                            received_bytes += chunk_size
                            received_finished_at = time.monotonic()
                            data_buffer = data_buffer[chunk_size:]
                            continue

                        try:
                            data_buffer = conn.recv(
                                min(BUFFER_SIZE, expected_bytes - received_bytes)
                            )
                        except socket.timeout:
                            result_note = "Истекло время ожидания данных."
                            break
                        if not data_buffer:
                            break

                        if received_started_at is None:
                            received_started_at = time.monotonic()
                        received_finished_at = time.monotonic()

                    footer_data = data_buffer
                    while b"\n" not in footer_data and len(footer_data) <= HEADER_LIMIT:
                        try:
                            data = conn.recv(BUFFER_SIZE)
                        except socket.timeout:
                            break
                        if not data:
                            break
                        footer_data += data

                    end_received = False
                    client_send_duration = None
                    if b"\n" in footer_data:
                        footer_line, _tail = footer_data.split(b"\n", 1)
                        try:
                            end_info = json.loads(footer_line.decode("utf-8"))
                            try:
                                end_type = TCPMessageType(end_info.get("type"))
                            except ValueError:
                                end_type = None
                            if end_type == TCPMessageType.END:
                                end_received = True
                                raw_duration = float(
                                    end_info.get("client_send_duration", 0)
                                )
                                if raw_duration > 0:
                                    client_send_duration = raw_duration
                            else:
                                result_note = (
                                    "Последнее сообщение не является завершающим."
                                )
                        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                            if result_note:
                                result_note += " "
                            result_note += "Не удалось разобрать завершающее сообщение."
                    else:
                        if result_note:
                            result_note += " "
                        result_note += "Завершающее сообщение не получено."

                    if received_started_at is None:
                        receive_duration = EPS
                    else:
                        receive_duration = max(
                            received_finished_at - received_started_at,
                            EPS,
                        )

                    received_packets = min(packets_count, received_bytes // packet_size)
                    lost_packets = packets_count - received_packets
                    duration = client_send_duration or receive_duration
                    speed_source = (
                        "по времени отправки клиента"
                        if client_send_duration is not None
                        else "по времени приема"
                    )
                    speed = received_bytes / duration
                    speed_text = f"{speed:.2f} B/s"
                    if speed >= 1024 * 1024:
                        speed_text = f"{speed / (1024 * 1024):.2f} MB/s"
                    elif speed >= 1024:
                        speed_text = f"{speed / 1024:.2f} KB/s"

                    response = {
                        "type": TCPMessageType.RESULT.value,
                        "received_packets": received_packets,
                        "packets": packets_count,
                        "lost_packets": lost_packets,
                        "speed": speed_text,
                        "speed_source": speed_source,
                        "end_received": end_received,
                    }
                    try:
                        conn.sendall(
                            json.dumps(response, separators=(",", ":")).encode("utf-8")
                            + b"\n"
                        )
                    except OSError:
                        pass

                    note_text = f" {result_note}" if result_note else ""
                    self.events.put(
                        {
                            "type": SpeedEventType.RESULT,
                            "speed": speed_text,
                            "received": f"{received_packets} из {packets_count}",
                            "lost": str(lost_packets),
                            "text": (
                                f"Получено {received_bytes} байт, "
                                f"потеряно {lost_packets} пакетов; "
                                f"скорость {speed_source}.{note_text}"
                            ),
                        }
                    )
        except (OSError, ValueError, json.JSONDecodeError, ConnectionError) as e:
            self.events.put(
                {"type": SpeedEventType.ERROR, "text": f"Ошибка приема: {e}"}
            )
        finally:
            self.server_sock = None

    def poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            event_type = event.get("type")
            if event_type == SpeedEventType.RESULT:
                self.speed_var.set(str(event.get("speed", "-")))
                self.received_var.set(str(event.get("received", "-")))
                self.lost_var.set(str(event.get("lost", "-")))
                self.status_var.set(str(event.get("text", "")))
                self.receive_button.configure(state="normal")
            else:
                text = str(event.get("text", ""))
                self.status_var.set(text)
                if event_type == SpeedEventType.ERROR:
                    self.receive_button.configure(state="normal")
                    messagebox.showerror("Ошибка TCP", text, parent=self)

        if not self.closed:
            self.after(POLL_INTERVAL, self.poll_events)

    def close_app(self) -> None:
        self.closed = True
        self.stop_event.set()
        if self.server_sock is not None:
            try:
                self.server_sock.close()
            except OSError:
                pass
        self.destroy()


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8080)
parser.add_argument("--timeout", type=float, default=5.0)
parser.add_argument("--wait-timeout", type=float, default=30.0)
args = parser.parse_args()

if args.timeout <= 0:
    parser.error("--timeout must be greater than 0")
if args.wait_timeout <= 0:
    parser.error("--wait-timeout must be greater than 0")

TCPSpeedReceiver(args).mainloop()
