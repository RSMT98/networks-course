import argparse
import re
import shlex
import socket
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Iterable, Optional

TIMEOUT = 10


@dataclass
class FTPResponse:
    code: int
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class FTPTransferMode(Enum):
    PASSIVE = auto()
    ACTIVE = auto()


class FTPClient:
    def __init__(self, force_active_ftp: bool = False) -> None:
        self.sock: Optional[socket.socket] = None
        self.sock_file = None
        self.force_active_ftp = force_active_ftp

    def connect(self, host: str, port: int) -> FTPResponse:
        self.close()
        self.sock = socket.create_connection((host, port), timeout=TIMEOUT)
        self.sock.settimeout(TIMEOUT)
        self.sock_file = self.sock.makefile("rb")

        resp = self._read_response()
        self._check_expected_response(resp, expected_prefixes=(2,))
        return resp

    def close(self) -> None:
        try:
            if self.sock_file is not None:
                self.sock_file.close()
        finally:
            self.sock_file = None
            if self.sock is not None:
                try:
                    self.sock.close()
                finally:
                    self.sock = None

    def quit(self) -> Optional[FTPResponse]:
        if self.sock is None:
            return None
        try:
            return self.send_command("QUIT", expected_codes={221, 226})
        finally:
            self.close()

    def _readline(self) -> str:
        if self.sock_file is None:
            raise ConnectionError("Control connection is not open")

        raw_line = self.sock_file.readline()
        if not raw_line:
            raise EOFError("Control connection was closed")
        return raw_line.decode("latin-1").rstrip("\r\n")

    def _read_response(self) -> FTPResponse:
        first_line = self._readline()
        lines = [first_line]
        if len(first_line) >= 4 and first_line[:3].isdigit() and first_line[3] == "-":
            code = int(first_line[:3])
            while True:
                line = self._readline()
                lines.append(line)
                if line.startswith(f"{code} "):
                    break
        else:
            if len(first_line) < 3 or not first_line[:3].isdigit():
                raise ValueError(f"Invalid response: {first_line!r}")

            code = int(first_line[:3])

        return FTPResponse(code=code, lines=lines)

    def send_command(
        self,
        cmd: str,
        *,
        expected_codes: Optional[set[int]] = None,
        expected_prefixes: Optional[Iterable[int]] = None,
    ) -> FTPResponse:
        data = cmd + "\r\n"
        if self.sock is None:
            raise ConnectionError("Control connection is not open")

        self.sock.sendall(data.encode("latin-1"))
        resp = self._read_response()
        self._check_expected_response(
            resp, expected_codes=expected_codes, expected_prefixes=expected_prefixes
        )
        return resp

    def _check_expected_response(
        self,
        resp: FTPResponse,
        *,
        expected_codes: Optional[set[int]] = None,
        expected_prefixes: Optional[Iterable[int]] = None,
    ) -> None:
        if expected_codes is not None:
            if resp.code in expected_codes:
                return

            raise RuntimeError(f"Unexpected FTP response {resp.code}: {resp.text}")

        prefixes = tuple(expected_prefixes or (1, 2, 3))
        if resp.code // 100 not in prefixes:
            raise RuntimeError(f"Unexpected FTP response {resp.code}: {resp.text}")

    def login(self, username: str, password: str) -> FTPResponse:
        resp = self.send_command(f"USER {username}", expected_prefixes=(2, 3))
        if resp.code == 331:
            resp = self.send_command(f"PASS {password}", expected_codes={230, 202})
        return resp

    def pwd(self) -> str:
        resp = self.send_command("PWD", expected_codes={257})
        match = re.search(r'"([^"]*)"', resp.text)
        return match.group(1) if match else resp.text

    def cwd(self, path: str) -> FTPResponse:
        return self.send_command(f"CWD {path}", expected_codes={250})

    def mkdir(self, path: str) -> FTPResponse:
        return self.send_command(f"MKD {path}", expected_codes={257, 250})

    def remove_file(self, path: str) -> FTPResponse:
        return self.send_command(f"DELE {path}", expected_codes={250})

    def remove_dir(self, path: str) -> FTPResponse:
        return self.send_command(f"RMD {path}", expected_codes={250})

    def _open_passive_data_socket(self) -> socket.socket:
        resp = self.send_command("PASV", expected_codes={227})
        match = re.compile(r"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)").search(resp.text)
        if not match:
            raise ValueError(f"Could not parse PASV response: {resp.text}")

        h1, h2, h3, h4, p1, p2 = map(int, match.groups())
        host = f"{h1}.{h2}.{h3}.{h4}"
        port = p1 * 256 + p2
        data_sock = socket.create_connection((host, port), timeout=TIMEOUT)
        data_sock.settimeout(TIMEOUT)
        return data_sock

    def _open_active_data_listener(self) -> socket.socket:
        if self.sock is None:
            raise ConnectionError("Control connection is not open")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.settimeout(TIMEOUT)
        host = self.sock.getsockname()[0]
        if ":" in host:
            listener.close()
            raise RuntimeError("Active FTP currently supports only IPv4 addresses")

        if host == "0.0.0.0":
            host = "127.0.0.1"
        listener.bind((host, 0))
        listener.listen(1)
        listen_host, listen_port = listener.getsockname()
        h1, h2, h3, h4 = listen_host.split(".")
        p1, p2 = divmod(listen_port, 256)
        self.send_command(
            f"PORT {h1},{h2},{h3},{h4},{p1},{p2}",
            expected_codes={200},
        )
        return listener

    def _get_transfer_mode_and_socket(self) -> tuple[FTPTransferMode, socket.socket]:
        if self.force_active_ftp:
            return FTPTransferMode.ACTIVE, self._open_active_data_listener()

        try:
            return FTPTransferMode.PASSIVE, self._open_passive_data_socket()
        except Exception as passive_error:
            try:
                return FTPTransferMode.ACTIVE, self._open_active_data_listener()
            except Exception as active_error:
                raise RuntimeError(
                    "Could not open a data connection in either passive or active FTP. "
                    f"Passive error: {passive_error}. Active error: {active_error}"
                ) from active_error

    def _accept_active_data_socket(self, listener: socket.socket) -> socket.socket:
        data_sock, _ = listener.accept()
        data_sock.settimeout(TIMEOUT)
        return data_sock

    def _close_socket(self, sock: Optional[socket.socket]) -> None:
        if sock is None:
            return

        try:
            sock.close()
        except OSError:
            pass

    def _get_ls_result(self, ftp_command: str) -> str:
        self.send_command("TYPE A", expected_codes={200})
        mode, transfer_sock = self._get_transfer_mode_and_socket()
        data_sock: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.PASSIVE else None
        )
        listener: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.ACTIVE else None
        )

        try:
            resp = self.send_command(ftp_command, expected_prefixes=(1, 2))
            if mode == FTPTransferMode.ACTIVE and resp.code // 100 == 1:
                data_sock = self._accept_active_data_socket(listener)

            batches: list[bytes] = []
            if data_sock is not None:
                while True:
                    batch = data_sock.recv(65536)
                    if not batch:
                        break

                    batches.append(batch)

            if resp.code // 100 == 1:
                self._read_operation_successful()

            return b"".join(batches).decode("utf-8", errors="replace").strip()
        finally:
            self._close_socket(data_sock)
            self._close_socket(listener)

    def _read_operation_successful(self) -> FTPResponse:
        resp = self._read_response()
        self._check_expected_response(resp, expected_codes={225, 226, 250})
        return resp

    def list(self, path: str) -> str:
        cmd = "LIST" if not path else f"LIST {path}"
        return self._get_ls_result(cmd)

    def nlst(self, path: str) -> str:
        cmd = "NLST" if not path else f"NLST {path}"
        return self._get_ls_result(cmd)

    def upload_bytes(self, data: bytes, remote_path: str) -> str:
        self.send_command("TYPE I", expected_codes={200})
        mode, transfer_sock = self._get_transfer_mode_and_socket()
        data_sock: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.PASSIVE else None
        )
        listener: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.ACTIVE else None
        )
        try:
            resp = self.send_command(f"STOR {remote_path}", expected_prefixes=(1, 2))
            if mode == FTPTransferMode.ACTIVE and resp.code // 100 == 1:
                data_sock = self._accept_active_data_socket(listener)

            if data:
                assert data_sock is not None
                data_sock.sendall(data)

            if data_sock is not None:
                try:
                    data_sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

            if resp.code // 100 == 1:
                return self._read_operation_successful().text
            return resp.text
        finally:
            self._close_socket(data_sock)
            self._close_socket(listener)

    def download_bytes(self, remote_path: str) -> bytes:
        self.send_command("TYPE I", expected_codes={200})
        mode, transfer_sock = self._get_transfer_mode_and_socket()
        data_sock: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.PASSIVE else None
        )
        listener: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.ACTIVE else None
        )
        try:
            resp = self.send_command(f"RETR {remote_path}", expected_prefixes=(1, 2))
            if mode == FTPTransferMode.ACTIVE and resp.code // 100 == 1:
                data_sock = self._accept_active_data_socket(listener)

            batches: list[bytes] = []
            if data_sock is not None:
                while True:
                    batch = data_sock.recv(65536)
                    if not batch:
                        break

                    batches.append(batch)

            if resp.code // 100 == 1:
                self._read_operation_successful()

            return b"".join(batches)
        finally:
            self._close_socket(data_sock)
            self._close_socket(listener)

    def upload(self, str_local_path: str, remote_path: Optional[str] = None) -> str:
        local_path = Path(str_local_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        self.send_command("TYPE I", expected_codes={200})
        mode, transfer_sock = self._get_transfer_mode_and_socket()
        data_sock: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.PASSIVE else None
        )
        listener: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.ACTIVE else None
        )
        try:
            resp = self.send_command(
                f"STOR {remote_path or local_path.name}", expected_prefixes=(1, 2)
            )
            if mode == FTPTransferMode.ACTIVE and resp.code // 100 == 1:
                data_sock = self._accept_active_data_socket(listener)

            with local_path.open("rb") as src:
                if data_sock is not None:
                    while True:
                        batch = src.read(65536)
                        if not batch:
                            break

                        data_sock.sendall(batch)

            if data_sock is not None:
                try:
                    data_sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

            if resp.code // 100 == 1:
                return self._read_operation_successful().text
            return resp.text
        finally:
            self._close_socket(data_sock)
            self._close_socket(listener)

    def download(self, remote_path: str, str_local_path: Optional[str] = None) -> str:
        local_path = Path(str_local_path or Path(remote_path).name)
        self.send_command("TYPE I", expected_codes={200})
        mode, transfer_sock = self._get_transfer_mode_and_socket()
        data_sock: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.PASSIVE else None
        )
        listener: Optional[socket.socket] = (
            transfer_sock if mode == FTPTransferMode.ACTIVE else None
        )
        try:
            resp = self.send_command(f"RETR {remote_path}", expected_prefixes=(1, 2))
            if mode == FTPTransferMode.ACTIVE and resp.code // 100 == 1:
                data_sock = self._accept_active_data_socket(listener)

            with local_path.open("wb") as dst:
                if data_sock is not None:
                    while True:
                        batch = data_sock.recv(65536)
                        if not batch:
                            break

                        dst.write(batch)

            if resp.code // 100 == 1:
                return self._read_operation_successful().text
            return resp.text
        finally:
            self._close_socket(data_sock)
            self._close_socket(listener)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=21)
    parser.add_argument("--user", default="TestUser")
    parser.add_argument("--password", default="")
    parser.add_argument("--force-active-ftp", action="store_true")
    args = parser.parse_args()

    client = FTPClient(force_active_ftp=args.force_active_ftp)
    try:
        connect_resp = client.connect(args.host, args.port)
        print(connect_resp.text)
        login_resp = client.login(args.user, args.password)
        print(login_resp.text)
        print(
            """
Connected!
Available commands:
- ls [--names-only] [remote_path]
- pwd
- cd <remote_path>
- mkdir <remote_path>
- rm <remote_path>
- rmdir <remote_path>
- upload <local_path> [remote_path]
- download <remote_path> [local_path]
"""
        )
        while True:
            try:
                raw_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw_input:
                continue

            try:
                splitted_input = shlex.split(raw_input)
            except ValueError as e:
                print(f"Input parsing error: {e}")
                continue

            cmd = splitted_input[0].lower()
            cmd_args = splitted_input[1:]
            try:
                if cmd == "ls":
                    names_only = False
                    remote_path = ""
                    if cmd_args and cmd_args[0] == "--names-only":
                        names_only = True
                        cmd_args = cmd_args[1:]

                    if len(cmd_args) > 1:
                        print("Usage: ls [--names-only] [remote_path]")
                        continue

                    if cmd_args:
                        remote_path = cmd_args[0]

                    if names_only:
                        ls_res = client.nlst(remote_path)
                    else:
                        ls_res = client.list(remote_path)

                    print(ls_res if ls_res.strip() else "<empty>")
                elif cmd == "pwd":
                    print(client.pwd())
                elif cmd == "cd":
                    if len(cmd_args) != 1:
                        print("Usage: cd <remote_path>")
                        continue

                    print(client.cwd(cmd_args[0]).text)
                elif cmd == "mkdir":
                    if len(cmd_args) != 1:
                        print("Usage: mkdir <remote_path>")
                        continue

                    print(client.mkdir(cmd_args[0]).text)
                elif cmd == "rm":
                    if len(cmd_args) != 1:
                        print("Usage: rm <remote_path>")
                        continue

                    print(client.remove_file(cmd_args[0]).text)
                elif cmd == "rmdir":
                    if len(cmd_args) != 1:
                        print("Usage: rmdir <remote_path>")
                        continue

                    print(client.remove_dir(cmd_args[0]).text)
                elif cmd == "upload":
                    if len(cmd_args) not in {1, 2}:
                        print("Usage: upload <local_path> [remote_path]")
                        continue

                    print(
                        client.upload(
                            cmd_args[0], cmd_args[1] if len(cmd_args) == 2 else None
                        )
                    )
                elif cmd == "download":
                    if len(cmd_args) not in {1, 2}:
                        print("Usage: download <remote_path> [local_path]")
                        continue

                    print(
                        client.download(
                            cmd_args[0],
                            (
                                cmd_args[1]
                                if len(cmd_args) == 2
                                else Path(cmd_args[0]).name
                            ),
                        )
                    )
                else:
                    print(f"Unknown command: {cmd}")
            except Exception as e:
                print(f"Error: {e}")

        try:
            resp = client.quit()
            if resp is not None:
                print(resp.text)
        except Exception as e:
            print(f"Connection closed with an error: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
