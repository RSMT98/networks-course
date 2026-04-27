import json
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO, Optional

from checksum import internet_checksum, is_checksum_valid

MAGIC = b"SAW1"
HEADER_FORMAT = "!4sBBIH"
HEADER_WITHOUT_CHECKSUM_FORMAT = "!4sBBI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
BUFFER_SIZE = 65535


class FrameType(IntEnum):
    START = 1
    DATA = 2
    END = 3
    ACK = 4
    DONE = 5


@dataclass
class Frame:
    frame_type: FrameType
    seq: int
    payload: bytes = b""

    def to_bytes(self) -> bytes:
        header_without_checksum = struct.pack(
            HEADER_WITHOUT_CHECKSUM_FORMAT,
            MAGIC,
            int(self.frame_type),
            self.seq,
            len(self.payload),
        )
        checksum = internet_checksum(header_without_checksum + self.payload)
        header = struct.pack(
            HEADER_FORMAT,
            MAGIC,
            int(self.frame_type),
            self.seq,
            len(self.payload),
            checksum,
        )
        return header + self.payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "Frame":
        if len(data) < HEADER_SIZE:
            raise ValueError("Frame is shorter than the protocol header")

        magic, raw_frame_type, seq, payload_size, checksum = struct.unpack(
            HEADER_FORMAT, data[:HEADER_SIZE]
        )
        if magic != MAGIC:
            raise ValueError("Unknown protocol magic")
        if seq not in {0, 1}:
            raise ValueError(f"Invalid seq number: {seq}")
        try:
            frame_type = FrameType(raw_frame_type)
        except ValueError as e:
            raise ValueError(f"Invalid frame type: {raw_frame_type}") from e

        payload = data[HEADER_SIZE:]
        if len(payload) != payload_size:
            raise ValueError(
                f"Invalid payload size: expected {payload_size}, got {len(payload)}"
            )

        header_without_checksum = struct.pack(
            HEADER_WITHOUT_CHECKSUM_FORMAT,
            magic,
            raw_frame_type,
            seq,
            payload_size,
        )
        if not is_checksum_valid(header_without_checksum + payload, checksum):
            raise ValueError(f"Invalid checksum in {frame_type.name} frame, seq={seq}")

        return cls(frame_type=frame_type, seq=seq, payload=payload)

    @property
    def checksum(self) -> int:
        header_without_checksum = struct.pack(
            HEADER_WITHOUT_CHECKSUM_FORMAT,
            MAGIC,
            int(self.frame_type),
            self.seq,
            len(self.payload),
        )
        return internet_checksum(header_without_checksum + self.payload)


@dataclass
class TransferResult:
    path: Path
    bytes_count: int
    frames_count: int


@dataclass
class IncomingTransferState:
    output_dir: Path
    expected_seq: int = 0
    dst: Optional[BinaryIO] = None
    output_path: Optional[Path] = None
    expected_size: Optional[int] = None
    peer_timeout: float = 1.0
    peer_max_retries: int = 30
    drain_seconds: float = 0.0
    drain_end_time: Optional[float] = None
    received_bytes: int = 0
    frames_count: int = 0
    result: Optional[TransferResult] = None
    error: Optional[Exception] = None


def validate_common_args(loss_rate: float, corrupt_rate: float, timeout: float) -> None:
    if not 0.0 <= loss_rate <= 1.0:
        raise ValueError("--loss-rate must be in range 0..1")
    if not 0.0 <= corrupt_rate <= 1.0:
        raise ValueError("--corrupt-rate must be in range 0..1")
    if timeout <= 0:
        raise ValueError("--timeout must be greater than 0")


def format_addr(addr: tuple[str, int]) -> str:
    return f"{addr[0]}:{addr[1]}"


class StopAndWaitProtocol:
    def __init__(
        self,
        sock: socket.socket,
        *,
        timeout: float,
        loss_rate: float = 0.3,
        corrupt_rate: float = 0.0,
        max_retries: int = 30,
    ) -> None:
        validate_common_args(loss_rate, corrupt_rate, timeout)
        if max_retries <= 0:
            raise ValueError("--max-retries must be greater than 0")

        self.sock = sock
        self.timeout = timeout
        self.loss_rate = loss_rate
        self.corrupt_rate = corrupt_rate
        self.max_retries = max_retries
        self.sock.settimeout(timeout)

    def send_file(
        self,
        local_path: Path,
        addr: tuple[str, int],
        *,
        chunk_size: int,
    ) -> None:
        if chunk_size <= 0 or chunk_size > 60000:
            raise ValueError("--chunk-size must be in range 1..60000")
        if not local_path.is_file():
            raise FileNotFoundError(f"File not found: {local_path}")

        stat = local_path.stat()
        meta_info = {
            "filename": local_path.name,
            "size": stat.st_size,
            "chunk_size": chunk_size,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }
        seq = 0
        frames_count = 0

        start_payload = json.dumps(meta_info, ensure_ascii=False).encode("utf-8")
        seq = self._send_with_ack(
            addr,
            frame_type=FrameType.START,
            seq=seq,
            payload=start_payload,
        )
        frames_count += 1

        sent_bytes = 0
        with local_path.open("rb") as src:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break

                seq = self._send_with_ack(
                    addr,
                    frame_type=FrameType.DATA,
                    seq=seq,
                    payload=chunk,
                )
                sent_bytes += len(chunk)
                frames_count += 1

        self._send_with_ack(addr, frame_type=FrameType.END, seq=seq, payload=b"")
        self._send_done(addr, seq)
        frames_count += 1
        print(
            f"Transfer finished: file={local_path}, bytes={sent_bytes}, frames={frames_count}"
        )

    def receive_file(
        self,
        output_dir: Path,
        *,
        expected_addr: Optional[tuple[str, int]] = None,
    ) -> TransferResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        expected_seq = 0
        dst = None
        output_path: Optional[Path] = None
        expected_size: Optional[int] = None
        drain_seconds = self._calc_drain_seconds(self.timeout, self.max_retries)
        received_bytes = 0
        frames_count = 0

        print("Waiting for a file transfer...")
        try:
            while True:
                try:
                    frame, addr = self._recv_valid_frame()
                except socket.timeout:
                    continue

                if expected_addr is not None and addr != expected_addr:
                    print(f"Ignored frame from unexpected address {format_addr(addr)}")
                    continue

                if frame.frame_type == FrameType.ACK:
                    print(
                        f"Ignored ACK seq={frame.seq} from {format_addr(addr)} while receiving file"
                    )
                    continue

                if frame.seq != expected_seq:
                    print(
                        f"Duplicate or out-of-order {frame.frame_type.name} seq={frame.seq}, expected={expected_seq}. Sending ACK again."
                    )
                    self._send_ack(addr, frame.seq)
                    continue

                if frame.frame_type == FrameType.START:
                    try:
                        meta_info = self._parse_start_payload(frame.payload)
                    except ValueError as e:
                        print(f"Invalid START frame: {e}")
                        continue

                    filename = Path(meta_info["filename"]).name
                    output_path = output_dir / filename
                    dst = output_path.open("wb")
                    expected_size = meta_info["size"]
                    peer_timeout, peer_max_retries = self._parse_peer_timing(meta_info)
                    drain_seconds = self._calc_drain_seconds(
                        peer_timeout,
                        peer_max_retries,
                    )
                    received_bytes = 0
                    frames_count = 1
                    print(
                        f"Started receiving file={filename}, "
                        f"expected_size={meta_info['size']}, "
                        f"chunk_size={meta_info['chunk_size']}, "
                        f"peer_timeout={peer_timeout:.3f}s, "
                        f"peer_max_retries={peer_max_retries}"
                    )
                    self._send_ack(addr, frame.seq)
                    expected_seq = 1 - expected_seq
                    continue

                if frame.frame_type == FrameType.DATA:
                    if dst is None or output_path is None:
                        print("Received DATA before START. Frame ignored.")
                        continue

                    dst.write(frame.payload)
                    received_bytes += len(frame.payload)
                    frames_count += 1
                    print(
                        f"Received DATA seq={frame.seq}, bytes={len(frame.payload)}, total={received_bytes}"
                    )
                    self._send_ack(addr, frame.seq)
                    expected_seq = 1 - expected_seq
                    continue

                if frame.frame_type == FrameType.END:
                    if dst is None or output_path is None:
                        print("Received END before START. Frame ignored.")
                        continue

                    dst.close()
                    dst = None
                    frames_count += 1
                    self._send_ack(addr, frame.seq)
                    if expected_size is not None and received_bytes != expected_size:
                        raise ValueError(
                            f"Received file size mismatch: expected {expected_size}, got {received_bytes}"
                        )

                    print(
                        f"File received: path={output_path}, bytes={received_bytes}, frames={frames_count}"
                    )
                    self._drain_for_duplicate_end(addr, frame.seq, drain_seconds)
                    return TransferResult(
                        path=output_path,
                        bytes_count=received_bytes,
                        frames_count=frames_count,
                    )
        finally:
            if dst is not None:
                dst.close()

    def _send_with_ack(
        self,
        addr: tuple[str, int],
        *,
        frame_type: FrameType,
        seq: int,
        payload: bytes,
    ) -> int:
        frame = Frame(frame_type=frame_type, seq=seq, payload=payload)
        for attempt in range(1, self.max_retries + 1):
            self._send_frame(addr, frame)
            print(
                f"Sent {frame_type.name} seq={seq}, bytes={len(payload)}, checksum=0x{frame.checksum:04x}, attempt={attempt}"
            )

            try:
                while True:
                    ack_frame, ack_addr = self._recv_valid_frame()
                    if ack_addr != addr:
                        print(
                            f"Ignored ACK from unexpected address {format_addr(ack_addr)}"
                        )
                        continue
                    if ack_frame.frame_type == FrameType.ACK and ack_frame.seq == seq:
                        print(f"Received ACK seq={seq} from {format_addr(ack_addr)}")
                        return 1 - seq

                    print(
                        f"Ignored {ack_frame.frame_type.name} seq={ack_frame.seq} while waiting for ACK seq={seq}"
                    )
            except socket.timeout:
                print(f"Timeout waiting for ACK seq={seq}. Resending.")

        raise TimeoutError(
            f"No ACK seq={seq} from {format_addr(addr)} after {self.max_retries} attempts"
        )

    def _send_ack(self, addr: tuple[str, int], seq: int) -> None:
        ack = Frame(frame_type=FrameType.ACK, seq=seq)
        self._send_frame(addr, ack)
        print(
            f"Sent ACK seq={seq} to {format_addr(addr)}, checksum=0x{ack.checksum:04x}"
        )

    def _send_done(self, addr: tuple[str, int], seq: int) -> None:
        done = Frame(frame_type=FrameType.DONE, seq=seq)
        for i in range(1, 4):
            self._send_frame(addr, done)
            print(
                f"Sent DONE seq={seq} to {format_addr(addr)}, checksum=0x{done.checksum:04x}, copy={i}"
            )

    def _send_frame(self, addr: tuple[str, int], frame: Frame) -> None:
        if random.random() < self.loss_rate:
            print(
                f"Simulated packet loss: {frame.frame_type.name} seq={frame.seq} to {format_addr(addr)}"
            )
            return

        raw_frame = frame.to_bytes()
        if self.corrupt_rate > 0.0 and random.random() < self.corrupt_rate:
            raw_frame = self._corrupt_bytes(raw_frame)
            print(
                f"Simulated bit error: {frame.frame_type.name} seq={frame.seq} to {format_addr(addr)}"
            )

        self.sock.sendto(raw_frame, addr)

    def _recv_valid_frame(self) -> tuple[Frame, tuple[str, int]]:
        while True:
            data, addr = self.sock.recvfrom(BUFFER_SIZE)
            try:
                frame = Frame.from_bytes(data)
            except ValueError as e:
                print(f"Invalid frame from {format_addr(addr)}: {e}")
                continue

            print(
                f"Received {frame.frame_type.name} seq={frame.seq} from {format_addr(addr)}, bytes={len(frame.payload)}, checksum=0x{frame.checksum:04x}"
            )
            return frame, addr

    def _drain_for_duplicate_end(
        self,
        addr: tuple[str, int],
        seq: int,
        drain_seconds: float,
    ) -> None:
        old_timeout = self.sock.gettimeout()
        end_time = time.monotonic() + drain_seconds
        try:
            while True:
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    break

                self.sock.settimeout(remaining)
                try:
                    frame, frame_addr = self._recv_valid_frame()
                except socket.timeout:
                    break

                if (
                    frame_addr == addr
                    and frame.frame_type == FrameType.DONE
                    and frame.seq == seq
                ):
                    print("Received DONE from sender.")
                    break

                if (
                    frame_addr == addr
                    and frame.frame_type == FrameType.END
                    and frame.seq == seq
                ):
                    print("Duplicate END received after transfer. Sending ACK again.")
                    self._send_ack(addr, seq)
                    end_time = time.monotonic() + drain_seconds
        finally:
            self.sock.settimeout(old_timeout)

    def _parse_start_payload(self, payload: bytes) -> dict:
        try:
            meta_info = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"Invalid START payload: {e}") from e

        filename = meta_info.get("filename")
        size = meta_info.get("size")
        chunk_size = meta_info.get("chunk_size")
        if not isinstance(filename, str) or not filename:
            raise ValueError("START payload does not contain a valid filename")
        if not isinstance(size, int) or size < 0:
            raise ValueError("START payload does not contain a valid size")
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            raise ValueError("START payload does not contain a valid chunk size")
        return meta_info

    def _parse_peer_timing(self, meta_info: dict) -> tuple[float, int]:
        raw_timeout = meta_info.get("timeout", self.timeout)
        raw_max_retries = meta_info.get("max_retries", self.max_retries)
        if not isinstance(raw_timeout, (int, float)) or raw_timeout <= 0:
            raise ValueError("START payload contains invalid peer timeout")
        if not isinstance(raw_max_retries, int) or raw_max_retries <= 0:
            raise ValueError("START payload contains invalid peer max_retries")
        return float(raw_timeout), raw_max_retries

    def _calc_drain_seconds(
        self,
        peer_timeout: float,
        peer_max_retries: int,
    ) -> float:
        return peer_timeout * peer_max_retries + max(peer_timeout, self.timeout)

    def _corrupt_bytes(self, data: bytes) -> bytes:
        if not data:
            return data

        broken_data = bytearray(data)
        broken_data[random.randrange(len(broken_data))] ^= 0b00000001
        return bytes(broken_data)


class DuplexStopAndWaitProtocol(StopAndWaitProtocol):
    def __init__(
        self,
        sock: socket.socket,
        peer_addr: tuple[str, int],
        *,
        timeout: float,
        loss_rate: float = 0.3,
        corrupt_rate: float = 0.0,
        max_retries: int = 30,
    ) -> None:
        super().__init__(
            sock,
            timeout=timeout,
            loss_rate=loss_rate,
            corrupt_rate=corrupt_rate,
            max_retries=max_retries,
        )
        self.peer_addr = peer_addr
        self._ack_events = {
            0: threading.Event(),
            1: threading.Event(),
        }
        self._stop_event = threading.Event()
        self._file_received_event = threading.Event()
        self._incoming_drain_done_event = threading.Event()
        self._outgoing_done_event = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None
        self._incoming_state: Optional[IncomingTransferState] = None

    def start_receiving(self, output_dir: Path) -> None:
        if self._recv_thread is not None:
            raise RuntimeError("Receive thread is already running")

        output_dir.mkdir(parents=True, exist_ok=True)
        self._incoming_state = IncomingTransferState(output_dir=output_dir)
        self._stop_event.clear()
        self._file_received_event.clear()
        self._incoming_drain_done_event.clear()
        self._outgoing_done_event.clear()
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def stop_receiving(self) -> None:
        self._stop_event.set()
        if self._recv_thread is not None:
            self._recv_thread.join(timeout=self.timeout + 0.5)
            self._recv_thread = None

        state = self._incoming_state
        if state is not None and state.dst is not None:
            try:
                state.dst.close()
            except OSError:
                pass
            finally:
                state.dst = None

    def wait_for_received_file(self) -> TransferResult:
        if self._incoming_state is None:
            raise RuntimeError("Receive thread was not started")

        self._file_received_event.wait()
        if self._incoming_state.error is not None:
            raise self._incoming_state.error
        if self._incoming_state.result is None:
            raise RuntimeError("Receive thread stopped without a transfer result")
        return self._incoming_state.result

    def wait_for_drain(self) -> None:
        if self._incoming_state is None:
            raise RuntimeError("Receive thread was not started")

        self._incoming_drain_done_event.wait()
        if self._incoming_state.error is not None:
            raise self._incoming_state.error

    def send_file_to_peer(self, local_path: Path, *, chunk_size: int) -> None:
        if chunk_size <= 0 or chunk_size > 60000:
            raise ValueError("--chunk-size must be in range 1..60000")
        if not local_path.is_file():
            raise FileNotFoundError(f"File not found: {local_path}")

        stat = local_path.stat()
        meta_info = {
            "filename": local_path.name,
            "size": stat.st_size,
            "chunk_size": chunk_size,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }
        seq = 0
        frames_count = 0

        start_payload = json.dumps(meta_info, ensure_ascii=False).encode("utf-8")
        seq = self._send_with_receiver_ack(
            frame_type=FrameType.START,
            seq=seq,
            payload=start_payload,
        )
        frames_count += 1

        sent_bytes = 0
        with local_path.open("rb") as src:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break

                seq = self._send_with_receiver_ack(
                    frame_type=FrameType.DATA,
                    seq=seq,
                    payload=chunk,
                )
                sent_bytes += len(chunk)
                frames_count += 1

        self._send_with_receiver_ack(frame_type=FrameType.END, seq=seq, payload=b"")
        self._send_done(self.peer_addr, seq)
        self._outgoing_done_event.set()
        frames_count += 1
        print(
            f"Outgoing transfer finished: file={local_path}, bytes={sent_bytes}, frames={frames_count}"
        )

    def _send_with_receiver_ack(
        self,
        *,
        frame_type: FrameType,
        seq: int,
        payload: bytes,
    ) -> int:
        frame = Frame(frame_type=frame_type, seq=seq, payload=payload)
        ack_event = self._ack_events[seq]
        for attempt in range(1, self.max_retries + 1):
            ack_event.clear()
            self._send_frame(self.peer_addr, frame)
            print(
                f"Sent {frame_type.name} seq={seq}, bytes={len(payload)}, checksum=0x{frame.checksum:04x}, attempt={attempt}"
            )
            if ack_event.wait(self.timeout):
                print(f"ACK seq={seq} accepted from {format_addr(self.peer_addr)}")
                ack_event.clear()
                return 1 - seq

            print(f"Timeout waiting for ACK seq={seq}. Resending.")

        raise TimeoutError(
            f"No ACK seq={seq} from {format_addr(self.peer_addr)} after {self.max_retries} attempts"
        )

    def _recv_loop(self) -> None:
        assert self._incoming_state is not None
        while not self._stop_event.is_set():
            try:
                frame, addr = self._recv_valid_frame()
            except socket.timeout:
                self._finish_drain_if_ready()
                if self._can_stop_recv_loop():
                    return
                continue
            except ConnectionRefusedError as e:
                print(f"Peer is not reachable yet: {e}")
                continue
            except OSError as e:
                if not self._stop_event.is_set():
                    self._incoming_state.error = e
                    self._file_received_event.set()
                    self._incoming_drain_done_event.set()
                return

            if addr != self.peer_addr:
                print(f"Ignored frame from unexpected address {format_addr(addr)}")
                continue

            if frame.frame_type == FrameType.ACK:
                self._ack_events[frame.seq].set()
                continue

            try:
                self._handle_incoming_file_frame(frame)
            except Exception as e:
                self._incoming_state.error = e
                self._file_received_event.set()
                self._incoming_drain_done_event.set()
                return

            self._finish_drain_if_ready()
            if self._can_stop_recv_loop():
                return

    def _finish_drain_if_ready(self) -> None:
        state = self._incoming_state
        if (
            state is None
            or state.result is None
            or state.drain_end_time is None
            or self._incoming_drain_done_event.is_set()
            or time.monotonic() < state.drain_end_time
        ):
            return

        self._incoming_drain_done_event.set()

    def _can_stop_recv_loop(self) -> bool:
        return (
            self._incoming_drain_done_event.is_set()
            and self._outgoing_done_event.is_set()
        )

    def _handle_incoming_file_frame(self, frame: Frame) -> None:
        state = self._incoming_state
        if state is None:
            return

        if state.result is not None:
            if frame.frame_type == FrameType.DONE:
                print("Received DONE from peer.")
                self._incoming_drain_done_event.set()
                return
            if frame.frame_type == FrameType.END:
                print("Duplicate END received after transfer. Sending ACK again.")
                self._send_ack(self.peer_addr, frame.seq)
                if not self._incoming_drain_done_event.is_set():
                    state.drain_end_time = time.monotonic() + state.drain_seconds
            else:
                print(
                    f"Ignored {frame.frame_type.name} seq={frame.seq} after incoming transfer was completed"
                )
            return

        if frame.seq != state.expected_seq:
            print(
                f"Duplicate or out-of-order {frame.frame_type.name} seq={frame.seq}, expected={state.expected_seq}. Sending ACK again."
            )
            self._send_ack(self.peer_addr, frame.seq)
            return

        if frame.frame_type == FrameType.START:
            meta_info = self._parse_start_payload(frame.payload)
            peer_timeout, peer_max_retries = self._parse_peer_timing(meta_info)
            filename = Path(meta_info["filename"]).name
            state.output_path = state.output_dir / filename
            state.dst = state.output_path.open("wb")
            state.expected_size = meta_info["size"]
            state.peer_timeout = peer_timeout
            state.peer_max_retries = peer_max_retries
            state.drain_seconds = self._calc_drain_seconds(
                state.peer_timeout,
                state.peer_max_retries,
            )
            state.received_bytes = 0
            state.frames_count = 1
            print(
                f"Started receiving file={filename}, "
                f"expected_size={state.expected_size}, "
                f"chunk_size={meta_info['chunk_size']}, "
                f"peer_timeout={state.peer_timeout:.3f}s, "
                f"peer_max_retries={state.peer_max_retries}"
            )
            self._send_ack(self.peer_addr, frame.seq)
            state.expected_seq = 1 - state.expected_seq
            return

        if frame.frame_type == FrameType.DATA:
            if state.dst is None or state.output_path is None:
                print("Received DATA before START. Frame ignored.")
                return

            state.dst.write(frame.payload)
            state.received_bytes += len(frame.payload)
            state.frames_count += 1
            print(
                f"Received DATA seq={frame.seq}, bytes={len(frame.payload)}, total={state.received_bytes}"
            )
            self._send_ack(self.peer_addr, frame.seq)
            state.expected_seq = 1 - state.expected_seq
            return

        if frame.frame_type == FrameType.END:
            if state.dst is None or state.output_path is None:
                print("Received END before START. Frame ignored.")
                return

            state.dst.close()
            state.dst = None
            state.frames_count += 1
            self._send_ack(self.peer_addr, frame.seq)
            if (
                state.expected_size is not None
                and state.received_bytes != state.expected_size
            ):
                raise ValueError(
                    f"Received file size mismatch: expected {state.expected_size}, got {state.received_bytes}"
                )

            print(
                f"File received: path={state.output_path}, bytes={state.received_bytes}, frames={state.frames_count}"
            )
            state.result = TransferResult(
                path=state.output_path,
                bytes_count=state.received_bytes,
                frames_count=state.frames_count,
            )
            state.drain_end_time = time.monotonic() + state.drain_seconds
            self._file_received_event.set()
