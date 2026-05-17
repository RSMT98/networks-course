from enum import Enum

CANVAS_WIDTH = 900
CANVAS_HEIGHT = 560
DEFAULT_LINE_COLOR = "#1f2937"
DEFAULT_LINE_WIDTH = 3
MIN_LINE_WIDTH = 1
MAX_LINE_WIDTH = 30
COLOR_PATTERN = r"#[0-9a-fA-F]{6}"


class EventType(str, Enum):
    START = "start"
    DRAW = "draw"
    END = "end"
    CLEAR = "clear"
    STATUS = "status"


class ConnectionResponse(str, Enum):
    ACCEPTED = "accepted"
    BUSY = "busy"
