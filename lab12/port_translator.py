import argparse
import ipaddress
import json
import queue
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

BUFFER_SIZE = 65536
POLL_INTERVAL = 100


class TranslatorEvent(str, Enum):
    LOG = "log"
    RULES = "rules"
    CONNECTION_STARTED = "connection_started"
    CONNECTION_FINISHED = "connection_finished"


@dataclass(frozen=True)
class TranslationRule:
    listen_port: int
    target_ip: str
    target_port: int


@dataclass
class PortListener:
    rule: TranslationRule
    sock: socket.socket
    stop_event: threading.Event

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.sock.close()
        except OSError:
            pass


class PortTranslatorApp(tk.Tk):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()

        self.args = args
        self.events: queue.Queue[dict] = queue.Queue()
        self.listeners: dict[int, PortListener] = {}
        self.connections_count: dict[int, int] = {}
        self.active_sockets: set[socket.socket] = set()
        self.active_sockets_lock = threading.Lock()
        self.watcher_stop_event: threading.Event | None = None
        self.rules_lock = threading.Lock()
        self.run_id = 0
        self.closed = False
        self.running = False
        self.listen_host = args.host
        self.config_path = args.config
        self.last_config_mtime: int | None = None

        self.host_var = tk.StringVar(value=args.host)
        self.config_var = tk.StringVar(value=str(args.config) if args.config else "")
        self.status_var = tk.StringVar(
            value="Выберите файл правил, затем нажмите Старт."
        )

        self.title("Транслятор портов")
        self.geometry("920x640")
        self.minsize(760, 520)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        settings_frame = ttk.LabelFrame(self, text="Настройки", padding=12)
        settings_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        settings_frame.columnconfigure(1, weight=1)
        settings_frame.columnconfigure(3, weight=3)

        ttk.Label(settings_frame, text="IP для прослушивания").grid(
            row=0, column=0, sticky="w"
        )
        self.host_entry = ttk.Entry(settings_frame, textvariable=self.host_var)
        self.host_entry.grid(row=0, column=1, sticky="ew", padx=(8, 16))

        ttk.Label(settings_frame, text="Файл правил").grid(row=0, column=2, sticky="w")
        self.config_entry = ttk.Entry(settings_frame, textvariable=self.config_var)
        self.config_entry.grid(row=0, column=3, sticky="ew", padx=(8, 8))

        self.browse_button = ttk.Button(
            settings_frame,
            text="Обзор",
            command=self.choose_config,
        )
        self.browse_button.grid(row=0, column=4)

        buttons_frame = ttk.Frame(self, padding=(12, 0, 12, 8))
        buttons_frame.grid(row=1, column=0, sticky="ew")

        self.start_button = ttk.Button(
            buttons_frame,
            text="Старт",
            command=self.start_translator,
        )
        self.start_button.grid(row=0, column=0, padx=(0, 8))

        self.reload_button = ttk.Button(
            buttons_frame,
            text="Перезагрузить правила",
            command=self.reload_rules,
            state="disabled",
        )
        self.reload_button.grid(row=0, column=1, padx=(0, 8))

        self.stop_button = ttk.Button(
            buttons_frame,
            text="Стоп",
            command=self.stop_translator,
            state="disabled",
        )
        self.stop_button.grid(row=0, column=2)

        main_pane = ttk.Panedwindow(self, orient=tk.VERTICAL)
        main_pane.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 8))

        rules_frame = ttk.LabelFrame(main_pane, text="Правила трансляции", padding=8)
        rules_frame.columnconfigure(0, weight=1)
        rules_frame.rowconfigure(0, weight=1)

        self.rules_tree = ttk.Treeview(
            rules_frame,
            columns=("listen", "target", "status", "connections"),
            show="headings",
            selectmode="browse",
            height=8,
        )
        self.rules_tree.heading("listen", text="Порт")
        self.rules_tree.heading("target", text="Назначение")
        self.rules_tree.heading("status", text="Состояние")
        self.rules_tree.heading("connections", text="Соединения")
        self.rules_tree.column("listen", width=90, anchor="center", stretch=False)
        self.rules_tree.column("target", width=300, stretch=True)
        self.rules_tree.column("status", width=150, anchor="center", stretch=False)
        self.rules_tree.column("connections", width=110, anchor="center", stretch=False)
        self.rules_tree.grid(row=0, column=0, sticky="nsew")

        rules_scroll = ttk.Scrollbar(
            rules_frame,
            orient="vertical",
            command=self.rules_tree.yview,
        )
        rules_scroll.grid(row=0, column=1, sticky="ns")
        self.rules_tree.configure(yscrollcommand=rules_scroll.set)

        log_frame = ttk.LabelFrame(main_pane, text="Журнал", padding=8)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        log_scroll = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        main_pane.add(rules_frame, weight=2)
        main_pane.add(log_frame, weight=1)

        ttk.Label(self, textvariable=self.status_var).grid(
            row=3, column=0, sticky="ew", padx=12, pady=(0, 12)
        )

        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.after(POLL_INTERVAL, self.poll_events)

    def choose_config(self) -> None:
        raw_config_path = self.config_var.get().strip()
        initial_dir = Path.cwd()
        if raw_config_path:
            initial_dir = Path(raw_config_path).expanduser().parent

        path = filedialog.askopenfilename(
            parent=self,
            title="Выберите файл правил",
            initialdir=str(initial_dir),
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if path:
            self.config_var.set(path)

    def start_translator(self) -> None:
        host = self.host_var.get().strip()
        raw_config_path = self.config_var.get().strip()
        config_path = Path(raw_config_path).expanduser()
        try:
            if not host:
                raise ValueError("IP для прослушивания не может быть пустым")
            if not raw_config_path:
                raise ValueError("Выберите файл правил")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_sock:
                test_sock.bind((host, 0))

            rules = self.load_rules(config_path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            messagebox.showerror("Ошибка запуска", str(e), parent=self)
            return

        self.running = True
        self.run_id += 1
        run_id = self.run_id
        if self.watcher_stop_event is not None:
            self.watcher_stop_event.set()
        self.watcher_stop_event = threading.Event()
        self.listen_host = host
        self.config_path = config_path
        self.last_config_mtime = config_path.stat().st_mtime_ns
        self.events.put(
            {"type": TranslatorEvent.LOG, "text": f"Запуск транслятора на {host}."}
        )
        failed_ports = self.apply_rules(host, rules, run_id=run_id)
        if failed_ports:
            self.running = False
            if self.watcher_stop_event is not None:
                self.watcher_stop_event.set()
            with self.rules_lock:
                for listener in self.listeners.values():
                    listener.close()
                self.listeners.clear()
            self.connections_count.clear()
            self.status_var.set("Транслятор не запущен.")
            self.events.put({"type": TranslatorEvent.RULES})

            error_text = "; ".join(
                f"{listen_port}: {error}" for listen_port, error in failed_ports
            )
            messagebox.showerror(
                "Ошибка запуска",
                f"Не удалось открыть все порты из конфигурации:\n{error_text}",
                parent=self,
            )
            return

        self.host_entry.configure(state="disabled")
        self.config_entry.configure(state="disabled")
        self.browse_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.reload_button.configure(state="normal")
        self.stop_button.configure(state="normal")
        self.status_var.set("Транслятор запущен.")

        threading.Thread(
            target=self.watch_config,
            args=(self.watcher_stop_event, run_id),
            daemon=True,
        ).start()

    def reload_rules(self) -> None:
        if not self.running:
            return

        config_path = self.config_path
        run_id = self.run_id
        try:
            rules = self.load_rules(config_path)
            self.last_config_mtime = config_path.stat().st_mtime_ns
        except (OSError, ValueError, json.JSONDecodeError) as e:
            messagebox.showerror("Ошибка правил", str(e), parent=self)
            return

        self.events.put(
            {"type": TranslatorEvent.LOG, "text": "Ручная перезагрузка правил."}
        )
        failed_ports = self.apply_rules(self.listen_host, rules, run_id=run_id)
        if failed_ports:
            failed_text = ", ".join(str(listen_port) for listen_port, _ in failed_ports)
            self.status_var.set(
                f"Правила применены частично. Не открыты порты: {failed_text}."
            )
        else:
            self.status_var.set("Правила перезагружены.")

    def load_rules(self, config_path: Path) -> list[TranslationRule]:
        if not config_path.exists():
            raise FileNotFoundError(f"Файл правил не найден: {config_path}")

        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_rules = (
            raw_config.get("rules") if isinstance(raw_config, dict) else raw_config
        )
        if not isinstance(raw_rules, list):
            raise ValueError("Файл правил должен содержать список rules")

        rules = []
        used_ports = set()
        for index, raw_rule in enumerate(raw_rules, start=1):
            if not isinstance(raw_rule, dict):
                raise ValueError(f"Правило #{index} должно быть JSON-объектом")

            listen_port = raw_rule.get(
                "listen_port",
                raw_rule.get("source_port", raw_rule.get("from_port")),
            )
            target_ip = raw_rule.get("target_ip")
            if target_ip is None:
                target_ip = raw_rule.get("target_host")
            if target_ip is None:
                raise ValueError(
                    f"В правиле #{index} target_host/target_ip не может быть пустым"
                )
            target_port = raw_rule.get("target_port")
            try:
                listen_port = int(listen_port)
                target_port = int(target_port)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"В правиле #{index} порты должны быть целыми числами"
                ) from e

            target_ip = str(target_ip).strip()
            if not 1 <= listen_port <= 65535:
                raise ValueError(
                    f"В правиле #{index} listen_port должен быть в диапазоне 1..65535"
                )
            if not 1 <= target_port <= 65535:
                raise ValueError(
                    f"В правиле #{index} target_port должен быть в диапазоне 1..65535"
                )
            if not target_ip:
                raise ValueError(
                    f"В правиле #{index} target_host/target_ip не может быть пустым"
                )
            try:
                target_ip = str(ipaddress.ip_address(target_ip))
            except ValueError as e:
                raise ValueError(
                    f"В правиле #{index} target_host/target_ip должен быть IP-адресом"
                ) from e
            if listen_port in used_ports:
                raise ValueError(f"Порт {listen_port} указан в правилах несколько раз")

            used_ports.add(listen_port)
            rules.append(
                TranslationRule(
                    listen_port=listen_port,
                    target_ip=target_ip,
                    target_port=target_port,
                )
            )

        return rules

    def apply_rules(
        self,
        host: str,
        rules: list[TranslationRule],
        *,
        run_id: int | None = None,
    ) -> list[tuple[int, str]]:
        new_rules = {rule.listen_port: rule for rule in rules}
        failed_ports: list[tuple[int, str]] = []
        with self.rules_lock:
            if run_id is not None and (not self.running or run_id != self.run_id):
                return failed_ports

            for listen_port, listener in list(self.listeners.items()):
                new_rule = new_rules.get(listen_port)
                if new_rule is None:
                    listener.close()
                    self.listeners.pop(listen_port, None)
                    self.events.put(
                        {
                            "type": TranslatorEvent.LOG,
                            "text": f"Остановлено прослушивание порта {listen_port}.",
                        }
                    )
                elif new_rule != listener.rule:
                    old_rule = listener.rule
                    listener.rule = new_rule
                    self.events.put(
                        {
                            "type": TranslatorEvent.LOG,
                            "text": (
                                f"Правило порта {listen_port} обновлено: "
                                f"{old_rule.target_ip}:{old_rule.target_port} -> {new_rule.target_ip}:{new_rule.target_port}."
                            ),
                        }
                    )

            for listen_port, rule in sorted(new_rules.items()):
                if listen_port in self.listeners:
                    continue

                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((host, listen_port))
                    sock.listen()
                    sock.settimeout(0.5)
                except OSError as e:
                    failed_ports.append((listen_port, str(e)))
                    self.events.put(
                        {
                            "type": TranslatorEvent.LOG,
                            "text": f"Не удалось открыть порт {listen_port}: {e}",
                        }
                    )
                    continue

                listener = PortListener(
                    rule=rule,
                    sock=sock,
                    stop_event=threading.Event(),
                )
                self.listeners[listen_port] = listener
                self.connections_count.setdefault(listen_port, 0)
                threading.Thread(
                    target=self.accept_clients,
                    args=(listener,),
                    daemon=True,
                ).start()
                self.events.put(
                    {
                        "type": TranslatorEvent.LOG,
                        "text": (
                            f"Порт {listen_port} -> {rule.target_ip}:{rule.target_port} активен."
                        ),
                    }
                )

        self.events.put({"type": TranslatorEvent.RULES})
        return failed_ports

    def watch_config(self, stop_event: threading.Event, run_id: int) -> None:
        while not stop_event.wait(self.args.reload_interval):
            if not self.running or run_id != self.run_id:
                continue

            config_path = self.config_path
            try:
                mtime = config_path.stat().st_mtime_ns
            except OSError as e:
                self.events.put(
                    {
                        "type": TranslatorEvent.LOG,
                        "text": f"Не удалось проверить файл правил: {e}",
                    }
                )
                continue

            if self.last_config_mtime is not None and mtime == self.last_config_mtime:
                continue

            try:
                rules = self.load_rules(config_path)
            except (OSError, ValueError, json.JSONDecodeError) as e:
                if stop_event.is_set() or not self.running or run_id != self.run_id:
                    break
                self.events.put(
                    {
                        "type": TranslatorEvent.LOG,
                        "text": f"Новые правила не применены: {e}",
                    }
                )
                continue

            if stop_event.is_set() or not self.running or run_id != self.run_id:
                break

            self.last_config_mtime = mtime
            self.events.put(
                {"type": TranslatorEvent.LOG, "text": "Файл правил изменился."}
            )
            self.apply_rules(self.listen_host, rules, run_id=run_id)

    def accept_clients(self, listener: PortListener) -> None:
        while not listener.stop_event.is_set():
            try:
                client_sock, client_addr = listener.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with self.rules_lock:
                rule = listener.rule

            self.events.put(
                {
                    "type": TranslatorEvent.LOG,
                    "text": (
                        f"Клиент {client_addr[0]}:{client_addr[1]} подключился к порту {rule.listen_port}."
                    ),
                }
            )
            threading.Thread(
                target=self.handle_client,
                args=(client_sock, client_addr, rule),
                daemon=True,
            ).start()

    def handle_client(
        self,
        client_sock: socket.socket,
        client_addr: tuple[str, int],
        rule: TranslationRule,
    ) -> None:
        try:
            target_sock = socket.create_connection(
                (rule.target_ip, rule.target_port),
                timeout=self.args.connect_timeout,
            )
            target_sock.settimeout(None)
        except OSError as e:
            client_sock.close()
            self.events.put(
                {
                    "type": TranslatorEvent.LOG,
                    "text": (
                        f"Не удалось подключить {client_addr[0]}:{client_addr[1]} к {rule.target_ip}:{rule.target_port}: {e}"
                    ),
                }
            )
            return

        with self.active_sockets_lock:
            if not self.running:
                try:
                    client_sock.close()
                except OSError:
                    pass
                try:
                    target_sock.close()
                except OSError:
                    pass
                return

            self.active_sockets.add(client_sock)
            self.active_sockets.add(target_sock)

        self.events.put(
            {"type": TranslatorEvent.CONNECTION_STARTED, "port": rule.listen_port}
        )
        self.events.put(
            {
                "type": TranslatorEvent.LOG,
                "text": (
                    f"Трансляция {client_addr[0]}:{client_addr[1]} -> {rule.target_ip}:{rule.target_port}."
                ),
            }
        )

        try:
            first_pipe = threading.Thread(
                target=self.forward_data,
                args=(client_sock, target_sock),
                daemon=True,
            )
            second_pipe = threading.Thread(
                target=self.forward_data,
                args=(target_sock, client_sock),
                daemon=True,
            )
            first_pipe.start()
            second_pipe.start()
            first_pipe.join()
            second_pipe.join()
        finally:
            with self.active_sockets_lock:
                self.active_sockets.discard(client_sock)
                self.active_sockets.discard(target_sock)

            try:
                client_sock.close()
            except OSError:
                pass
            try:
                target_sock.close()
            except OSError:
                pass

            self.events.put(
                {"type": TranslatorEvent.CONNECTION_FINISHED, "port": rule.listen_port}
            )
            self.events.put(
                {
                    "type": TranslatorEvent.LOG,
                    "text": (
                        f"Соединение {client_addr[0]}:{client_addr[1]} через порт {rule.listen_port} закрыто."
                    ),
                }
            )

    def forward_data(
        self,
        src: socket.socket,
        dst: socket.socket,
    ) -> None:
        try:
            while True:
                data = src.recv(BUFFER_SIZE)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            event_type = event.get("type")
            if event_type == TranslatorEvent.LOG:
                self.add_log(str(event.get("text", "")))
            elif event_type == TranslatorEvent.RULES:
                self.refresh_rules_table()
            elif event_type == TranslatorEvent.CONNECTION_STARTED:
                port = int(event["port"])
                self.connections_count[port] = self.connections_count.get(port, 0) + 1
                self.refresh_rules_table()
            elif event_type == TranslatorEvent.CONNECTION_FINISHED:
                port = int(event["port"])
                self.connections_count[port] = max(
                    self.connections_count.get(port, 1) - 1,
                    0,
                )
                self.refresh_rules_table()

        if not self.closed:
            self.after(POLL_INTERVAL, self.poll_events)

    def add_log(self, text: str) -> None:
        created_at = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{created_at}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def refresh_rules_table(self) -> None:
        self.rules_tree.delete(*self.rules_tree.get_children())
        with self.rules_lock:
            rows = sorted(self.listeners.items())

        for listen_port, listener in rows:
            rule = listener.rule
            self.rules_tree.insert(
                "",
                "end",
                values=(
                    listen_port,
                    f"{rule.target_ip}:{rule.target_port}",
                    "активно",
                    self.connections_count.get(listen_port, 0),
                ),
            )

    def stop_translator(self) -> None:
        self.running = False
        if self.watcher_stop_event is not None:
            self.watcher_stop_event.set()

        with self.rules_lock:
            for listener in self.listeners.values():
                listener.close()
            self.listeners.clear()

        with self.active_sockets_lock:
            active_sockets = list(self.active_sockets)
        for sock in active_sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        self.host_entry.configure(state="normal")
        self.config_entry.configure(state="normal")
        self.browse_button.configure(state="normal")
        self.start_button.configure(state="normal")
        self.reload_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.status_var.set("Транслятор остановлен.")
        self.events.put({"type": TranslatorEvent.RULES})
        self.events.put(
            {
                "type": TranslatorEvent.LOG,
                "text": "Транслятор остановлен. Активные соединения закрываются.",
            }
        )

    def close_app(self) -> None:
        self.closed = True
        self.stop_translator()
        self.destroy()


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--config", type=Path)
parser.add_argument("--reload-interval", type=float, default=1.0)
parser.add_argument("--connect-timeout", type=float, default=5.0)
args = parser.parse_args()

if args.reload_interval <= 0:
    parser.error("--reload-interval must be greater than 0")
if args.connect_timeout <= 0:
    parser.error("--connect-timeout must be greater than 0")

PortTranslatorApp(args).mainloop()
