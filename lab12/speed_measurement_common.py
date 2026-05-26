from enum import Enum


class SpeedEventType(Enum):
    STATUS = "status"
    DONE = "done"
    ERROR = "error"
    RESULT = "result"


class TCPMessageType(Enum):
    START = "start"
    END = "end"
    RESULT = "result"


class UDPMessageType(Enum):
    DATA = "data"
    END = "end"
