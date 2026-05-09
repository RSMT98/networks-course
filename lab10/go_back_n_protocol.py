import random
import socket
import struct
import time
from dataclasses import dataclass
from enum import Enum, IntEnum

MAGIC = b"GBN1"
HEADER_FORMAT = "!4sBIIH"
HEADER_WITHOUT_CHECKSUM_FORMAT = "!4sBII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
BUFFER_SIZE = 65535


class FrameType(IntEnum):
    START = 1
    DATA = 2
    END = 3
    ACK = 4
    DONE = 5
    ERROR = 6


class ProtocolSide(str, Enum):
    CLIENT = "client"
    SERVER = "server"


@dataclass
class Frame:
    frame_type: FrameType
    seq: int
    payload: bytes = b""

    def to_bytes(self) -> bytes:
        header = struct.pack(
            HEADER_FORMAT,
            MAGIC,
            int(self.frame_type),
            self.seq,
            len(self.payload),
            internet_checksum(
                struct.pack(
                    HEADER_WITHOUT_CHECKSUM_FORMAT,
                    MAGIC,
                    int(self.frame_type),
                    self.seq,
                    len(self.payload),
                )
                + self.payload
            ),
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

        try:
            frame_type = FrameType(raw_frame_type)
        except ValueError as e:
            raise ValueError(f"Invalid frame type: {raw_frame_type}") from e

        payload = data[HEADER_SIZE:]
        if len(payload) != payload_size:
            raise ValueError(
                f"Invalid payload size: expected {payload_size}, got {len(payload)}"
            )

        if not is_checksum_valid(
            struct.pack(
                HEADER_WITHOUT_CHECKSUM_FORMAT, magic, raw_frame_type, seq, payload_size
            )
            + payload,
            checksum,
        ):
            raise ValueError(f"Invalid checksum in {frame_type.name} frame, seq={seq}")

        return cls(frame_type=frame_type, seq=seq, payload=payload)

    @property
    def checksum(self) -> int:
        return internet_checksum(
            struct.pack(
                HEADER_WITHOUT_CHECKSUM_FORMAT,
                MAGIC,
                int(self.frame_type),
                self.seq,
                len(self.payload),
            )
            + self.payload
        )


def _fold_sum(value: int) -> int:
    while value >> 16:
        value = (value & 0xFFFF) + (value >> 16)
    return value


def _sum_words(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"

    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
        total = _fold_sum(total)
    return total


def internet_checksum(data: bytes) -> int:
    return ~_sum_words(data) & 0xFFFF


def is_checksum_valid(data: bytes, checksum: int) -> bool:
    if not 0 <= checksum <= 0xFFFF:
        return False

    return _fold_sum(_sum_words(data) + checksum) == 0xFFFF


def format_addr(addr: tuple[str, int]) -> str:
    return f"{addr[0]}:{addr[1]}"


def corrupt_bytes(data: bytes) -> bytes:
    if not data:
        return data

    broken_data = bytearray(data)
    broken_data[random.randrange(len(broken_data))] ^= 0b00000001
    return bytes(broken_data)


def send_frame(
    sock: socket.socket,
    addr: tuple[str, int],
    frame: Frame,
    *,
    loss_rate: float,
    corrupt_rate: float,
    side: ProtocolSide,
) -> None:
    if not isinstance(side, ProtocolSide):
        raise TypeError("side must be a ProtocolSide value")

    if random.random() < loss_rate:
        print(
            f"[{side.value}] Simulated packet loss: {frame.frame_type.name} seq={frame.seq} to {format_addr(addr)}"
        )
        return

    raw_frame = frame.to_bytes()
    if corrupt_rate > 0.0 and random.random() < corrupt_rate:
        raw_frame = corrupt_bytes(raw_frame)
        print(
            f"[{side.value}] Simulated bit error: {frame.frame_type.name} seq={frame.seq} to {format_addr(addr)}"
        )

    sock.sendto(raw_frame, addr)


def recv_frame(
    sock: socket.socket,
    *,
    deadline: float | None = None,
    return_invalid: bool = False,
) -> tuple[Frame | None, tuple[str, int]]:
    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()

            sock.settimeout(remaining)

        data, addr = sock.recvfrom(BUFFER_SIZE)
        try:
            frame = Frame.from_bytes(data)
        except ValueError as e:
            print(f"Invalid frame from {format_addr(addr)}: {e}")
            if return_invalid:
                return None, addr
            continue

        print(
            f"Received {frame.frame_type.name} seq={frame.seq} from {format_addr(addr)}, bytes={len(frame.payload)}, checksum=0x{frame.checksum:04x}"
        )
        return frame, addr


def validate_common_args(loss_rate: float, corrupt_rate: float, timeout: float) -> None:
    if not 0.0 <= loss_rate <= 1.0:
        raise ValueError("--loss-rate must be in range 0..1")
    if not 0.0 <= corrupt_rate <= 1.0:
        raise ValueError("--corrupt-rate must be in range 0..1")
    if timeout <= 0:
        raise ValueError("--timeout must be greater than 0")
