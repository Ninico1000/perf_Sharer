"""
Shared configuration, protocol helpers, and serialization utilities.
"""

import json
import struct
import socket
import logging

logger = logging.getLogger(__name__)

# ── Network ──────────────────────────────────────────────────────────────────
DEFAULT_PORT = 24800
FILE_TRANSFER_PORT = 24801
RECONNECT_DELAY = 3.0          # seconds between reconnect attempts
SOCKET_TIMEOUT = 5.0           # connect timeout

# ── Input ────────────────────────────────────────────────────────────────────
EDGE_THRESHOLD = 4             # pixels from screen edge that trigger a switch

# ── Message type constants ────────────────────────────────────────────────────
MSG_SCREEN_INFO  = 'screen_info'
MSG_SWITCH_IN    = 'switch_in'   # server tells client it now has control
MSG_SWITCH_OUT   = 'switch_out'  # client tells server it's returning control
MSG_MOUSE_MOVE   = 'move'
MSG_MOUSE_CLICK  = 'click'
MSG_MOUSE_SCROLL = 'scroll'
MSG_KEY          = 'key'
MSG_PING         = 'ping'
MSG_PONG         = 'pong'


# ── Screen resolution ─────────────────────────────────────────────────────────

def get_screen_size() -> tuple[int, int]:
    """Return (width, height) of the primary monitor. Uses tkinter (cross-platform)."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    w, h = root.winfo_screenwidth(), root.winfo_screenheight()
    root.destroy()
    return w, h


# ── Wire protocol: 4-byte length-prefixed JSON ────────────────────────────────

def send_msg(sock: socket.socket, data: dict) -> None:
    """Send a length-prefixed JSON message."""
    payload = json.dumps(data).encode('utf-8')
    sock.sendall(struct.pack('>I', len(payload)) + payload)


def recv_msg(sock: socket.socket) -> dict | None:
    """
    Receive a length-prefixed JSON message.
    Returns None on clean disconnect; raises on protocol error.
    """
    raw = _recvall(sock, 4)
    if raw is None:
        return None
    msglen = struct.unpack('>I', raw)[0]
    if msglen > 64 * 1024 * 1024:
        raise ValueError(f"Message too large: {msglen} bytes")
    body = _recvall(sock, msglen)
    if body is None:
        return None
    return json.loads(body.decode('utf-8'))


def recvall(sock: socket.socket, n: int) -> bytes | None:
    """Public alias used by transfer.py."""
    return _recvall(sock, n)


def _recvall(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ── Key / button serialization ────────────────────────────────────────────────

def serialize_key(key) -> dict:
    """Convert a pynput Key or KeyCode to a JSON-safe dict."""
    from pynput.keyboard import Key, KeyCode
    if isinstance(key, Key):
        return {'key_type': 'special', 'name': key.name}
    if isinstance(key, KeyCode):
        if key.char:
            return {'key_type': 'char', 'char': key.char}
        if key.vk is not None:
            return {'key_type': 'vk', 'vk': key.vk}
    return {'key_type': 'unknown'}


def deserialize_key(data: dict):
    """Reconstruct a pynput key from a serialized dict. Returns None on failure."""
    from pynput.keyboard import Key, KeyCode
    kt = data.get('key_type', 'unknown')
    try:
        if kt == 'special':
            return Key[data['name']]
        if kt == 'char':
            return KeyCode.from_char(data['char'])
        if kt == 'vk':
            return KeyCode.from_vk(data['vk'])
    except (KeyError, ValueError) as e:
        logger.debug(f"deserialize_key failed: {e}")
    return None


def serialize_button(button) -> str:
    """Return the pynput button name ('left', 'right', 'middle', …)."""
    return button.name


def deserialize_button(name: str):
    """Reconstruct a pynput Button from its name. Falls back to left."""
    from pynput.mouse import Button
    return getattr(Button, name, Button.left)
