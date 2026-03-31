import argparse
import socket
import threading
import time
from pathlib import Path
from typing import Optional

TIMEOUT = 10


class FTPClientSession:
    def __init__(
        self,
        sock: socket.socket,
        addr: tuple[str, int],
        *,
        root_dir: Path,
        username: str,
        password: str,
    ) -> None:
        self.sock = sock
        self.addr = addr
        self.root_dir = root_dir.resolve()
        self.username = username
        self.password = password
        self.sock.settimeout(None)
        self.sock_file = self.sock.makefile("rb")
        self.cur_dir = self.root_dir
        self.requested_username: Optional[str] = None
        self.is_authorized = False
        self.data_addr: Optional[tuple[str, int]] = None

    def run(self) -> None:
        try:
            self._send_response(220, "RSMT98's FTP server is ready.")
            while True:
                line = self._readline()
                if line is None:
                    break

                self._log(f"-> {line}")
                should_close = self._handle_command(line)
                if should_close:
                    break
        except (ConnectionError, EOFError, OSError) as e:
            self._log(f"Connection closed: {e}")
        finally:
            self.close()

    def close(self) -> None:
        try:
            self.sock_file.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _log(self, msg: str) -> None:
        print(f"[{self.addr[0]}:{self.addr[1]}] {msg}")

    def _readline(self) -> Optional[str]:
        raw_line = self.sock_file.readline()
        if not raw_line:
            return None
        return raw_line.decode("latin-1").rstrip("\r\n")

    def _send_response(self, code: int, msg: str) -> None:
        resp = f"{code} {msg}\r\n"
        self.sock.sendall(resp.encode("latin-1"))
        self._log(f"<- {code} {msg}")

    def _handle_command(self, line: str) -> bool:
        raw_cmd, _, raw_arg = line.partition(" ")
        cmd = raw_cmd.upper()
        arg = raw_arg.strip()

        if cmd == "USER":
            self.requested_username = arg
            self.is_authorized = False
            self._send_response(331, "Please, specify the password.")
            return False

        if cmd == "PASS":
            if self.requested_username is None:
                self._send_response(503, "Send USER before PASS.")
                return False

            if self.requested_username == self.username and arg == self.password:
                self.is_authorized = True
                self._send_response(230, "Login successful.")
            else:
                self.requested_username = None
                self._send_response(530, "Authentication failed.")
            return False

        if cmd == "QUIT":
            self._send_response(221, "Goodbye.")
            return True

        if cmd == "NOOP":
            self._send_response(200, "NOOP command successful.")
            return False

        if cmd == "SYST":
            self._send_response(215, "UNIX Type: L8")
            return False

        if cmd == "TYPE":
            if arg.upper() in {"A", "I"}:
                self._send_response(200, f"Type set to {arg.upper()}.")
            else:
                self._send_response(504, "Only TYPE A and TYPE I are supported.")
            return False

        if cmd == "FEAT":
            self._send_response(211, "No extra features.")
            return False

        if not self.is_authorized:
            self._send_response(530, "Please log in first.")
            return False

        if cmd == "PWD":
            self._send_response(
                257,
                f'"{self._to_server_path(self.cur_dir)}" is the current directory.',
            )
            return False

        if cmd == "CWD":
            self._handle_cwd(arg)
            return False

        if cmd == "PORT":
            self._handle_port(arg)
            return False

        if cmd == "MKD":
            self._handle_mkd(arg)
            return False

        if cmd == "RMD":
            self._handle_rmd(arg)
            return False

        if cmd == "DELE":
            self._handle_dele(arg)
            return False

        if cmd == "NLST":
            self._handle_nlst(arg)
            return False

        if cmd == "LIST":
            self._handle_list(arg)
            return False

        if cmd == "RETR":
            self._handle_retr(arg)
            return False

        if cmd == "STOR":
            self._handle_stor(arg)
            return False

        self._send_response(502, f"Command {cmd} is not implemented.")
        return False

    def _handle_cwd(self, arg: str) -> None:
        if not arg:
            self._send_response(501, "CWD requires a path.")
            return

        try:
            new_dir = self._resolve_path(arg)
        except PermissionError:
            self._send_response(550, "Access denied.")
            return

        if not new_dir.exists() or not new_dir.is_dir():
            self._send_response(550, "Directory does not exist.")
            return

        self.cur_dir = new_dir
        self._send_response(
            250, f'Current directory changed to "{self._to_server_path(new_dir)}".'
        )

    def _handle_port(self, arg: str) -> None:
        parts = [part.strip() for part in arg.split(",")]
        if len(parts) != 6:
            self._send_response(501, "PORT requires six comma-separated numbers.")
            return

        try:
            numbers = [int(part) for part in parts]
        except ValueError:
            self._send_response(501, "PORT arguments must be integers.")
            return

        if any(number < 0 or number > 255 for number in numbers):
            self._send_response(501, "PORT arguments must be in range 0..255.")
            return

        host = ".".join(str(number) for number in numbers[:4])
        port = numbers[4] * 256 + numbers[5]
        self.data_addr = (host, port)
        self._send_response(200, f"PORT command successful ({host}:{port}).")

    def _handle_mkd(self, arg: str) -> None:
        if not arg:
            self._send_response(501, "MKD requires a directory path.")
            return

        try:
            dir_path = self._resolve_path(arg)
        except PermissionError:
            self._send_response(550, "Access denied.")
            return

        if not dir_path.parent.exists() or not dir_path.parent.is_dir():
            self._send_response(550, "Parent directory does not exist.")
            return

        try:
            dir_path.mkdir()
            self._send_response(
                257, f'"{self._to_server_path(dir_path)}" directory created.'
            )
        except FileExistsError:
            self._send_response(550, "Directory already exists.")
        except OSError as e:
            self._send_response(550, f"Cannot create directory: {e}")

    def _handle_rmd(self, arg: str) -> None:
        if not arg:
            self._send_response(501, "RMD requires a directory path.")
            return

        try:
            dir_path = self._resolve_path(arg)
        except PermissionError:
            self._send_response(550, "Access denied.")
            return

        if dir_path == self.root_dir:
            self._send_response(550, "Root directory cannot be removed.")
            return

        if not dir_path.exists() or not dir_path.is_dir():
            self._send_response(550, "Directory does not exist.")
            return

        try:
            dir_path.rmdir()
            self._send_response(250, "Directory removed.")
        except OSError as e:
            self._send_response(550, f"Cannot remove directory: {e}")

    def _handle_dele(self, arg: str) -> None:
        if not arg:
            self._send_response(501, "DELE requires a file path.")
            return

        try:
            file_path = self._resolve_path(arg)
        except PermissionError:
            self._send_response(550, "Access denied.")
            return

        if not file_path.exists() or not file_path.is_file():
            self._send_response(550, "File does not exist.")
            return

        try:
            file_path.unlink()
            self._send_response(250, "File deleted.")
        except OSError as e:
            self._send_response(550, f"Cannot delete file: {e}")

    def _handle_nlst(self, arg: str) -> None:
        try:
            target = self._resolve_path(arg) if arg else self.cur_dir
        except PermissionError:
            self._send_response(550, "Access denied.")
            return

        if not target.exists():
            self._send_response(550, "Path does not exist.")
            return

        names: list[str]
        if target.is_dir():
            names = [
                path.name
                for path in sorted(
                    target.iterdir(),
                    key=lambda item: (not item.is_dir(), item.name.lower()),
                )
            ]
        else:
            names = [target.name]

        self._send_bytes_over_data_connection(
            "\r\n".join(names).encode("utf-8"), "Directory send OK."
        )

    def _handle_list(self, arg: str) -> None:
        try:
            target = self._resolve_path(arg) if arg else self.cur_dir
        except PermissionError:
            self._send_response(550, "Access denied.")
            return

        if not target.exists():
            self._send_response(550, "Path does not exist.")
            return

        lines: list[str]
        if target.is_dir():
            lines = [
                self._format_list_line(path)
                for path in sorted(
                    target.iterdir(),
                    key=lambda item: (not item.is_dir(), item.name.lower()),
                )
            ]
        else:
            lines = [self._format_list_line(target)]

        self._send_bytes_over_data_connection(
            "\r\n".join(lines).encode("utf-8"), "Directory send OK."
        )

    def _handle_retr(self, arg: str) -> None:
        if not arg:
            self._send_response(501, "RETR requires a file path.")
            return

        try:
            file_path = self._resolve_path(arg)
        except PermissionError:
            self._send_response(550, "Access denied.")
            return

        if not file_path.exists() or not file_path.is_file():
            self._send_response(550, "File does not exist.")
            return

        try:
            with file_path.open("rb") as src:
                self._send_file_over_data_connection(src, "Transfer complete.")
        except OSError as e:
            self._send_response(550, f"Cannot open file: {e}")

    def _handle_stor(self, arg: str) -> None:
        if not arg:
            self._send_response(501, "STOR requires a file path.")
            return

        try:
            file_path = self._resolve_path(arg)
        except PermissionError:
            self._send_response(550, "Access denied.")
            return

        if not file_path.parent.exists() or not file_path.parent.is_dir():
            self._send_response(550, "Target directory does not exist.")
            return

        data_sock = self._open_data_connection()
        if data_sock is None:
            return

        try:
            self._send_response(
                150, f"Opening binary mode data connection for {file_path.name}."
            )
            with file_path.open("wb") as dst:
                while True:
                    batch = data_sock.recv(65536)
                    if not batch:
                        break

                    dst.write(batch)

            self._send_response(226, "Transfer complete.")
        except OSError as e:
            self._send_response(550, f"Transfer failed: {e}")
        finally:
            self._close_socket(data_sock)

    def _send_bytes_over_data_connection(self, data: bytes, success_msg: str) -> None:
        data_sock = self._open_data_connection()
        if data_sock is None:
            return

        try:
            self._send_response(150, "Opening data connection.")
            if data:
                data_sock.sendall(data)
            self._send_response(226, success_msg)
        except OSError as e:
            self._send_response(550, f"Transfer failed: {e}")
        finally:
            self._close_socket(data_sock)

    def _send_file_over_data_connection(self, src, success_msg: str) -> None:
        data_sock = self._open_data_connection()
        if data_sock is None:
            return

        try:
            self._send_response(150, "Opening binary mode data connection.")
            while True:
                batch = src.read(65536)
                if not batch:
                    break

                data_sock.sendall(batch)
            self._send_response(226, success_msg)
        except OSError as e:
            self._send_response(550, f"Transfer failed: {e}")
        finally:
            self._close_socket(data_sock)

    def _open_data_connection(self) -> Optional[socket.socket]:
        if self.data_addr is None:
            self._send_response(425, "Use PORT first.")
            return None

        try:
            data_sock = socket.create_connection(self.data_addr, timeout=TIMEOUT)
            data_sock.settimeout(TIMEOUT)
            return data_sock
        except OSError as e:
            self._send_response(425, f"Cannot open data connection: {e}")
            return None
        finally:
            self.data_addr = None

    def _resolve_path(self, raw_path: str) -> Path:
        if raw_path.startswith("/"):
            target = self.root_dir / raw_path.lstrip("/")
        else:
            target = self.cur_dir / raw_path

        resolved_path = target.resolve(strict=False)
        try:
            resolved_path.relative_to(self.root_dir)
        except ValueError as e:
            raise PermissionError from e

        return resolved_path

    def _to_server_path(self, path: Path) -> str:
        if path == self.root_dir:
            return "/"
        return "/" + path.relative_to(self.root_dir).as_posix()

    def _format_list_line(self, path: Path) -> str:
        stat = path.stat()
        permissions = "drwxr-xr-x" if path.is_dir() else "-rw-r--r--"
        time_part = time.strftime("%b %d %H:%M", time.localtime(stat.st_mtime))
        size = 0 if path.is_dir() else stat.st_size
        return f"{permissions} 1 owner group {size:>8} {time_part} {path.name}"

    def _close_socket(self, sock: socket.socket) -> None:
        try:
            sock.close()
        except OSError:
            pass


class FTPServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password

        self.root_dir = Path("server-folder").resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind((self.host, self.port))
            server_sock.listen()
            print(f"FTP server is running on {self.host}:{self.port}")
            while True:
                sock, addr = server_sock.accept()
                print(f"The client is connected: {addr}")
                worker = threading.Thread(
                    target=self._serve_client,
                    args=(sock, addr),
                    daemon=True,
                )
                worker.start()

    def _serve_client(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        session = FTPClientSession(
            sock,
            addr,
            root_dir=self.root_dir,
            username=self.username,
            password=self.password,
        )
        session.run()


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=21)
parser.add_argument("--user", default="TestUser")
parser.add_argument("--password", default="")
args = parser.parse_args()

try:
    FTPServer(
        host=args.host, port=args.port, username=args.user, password=args.password
    ).run()
except KeyboardInterrupt:
    pass
