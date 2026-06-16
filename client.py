"""
KVM Client — runs on the machine that receives control from the server.

Responsibilities:
  • Connects to the server and automatically reconnects on drop
  • Injects mouse and keyboard events via pynput Controllers
  • Tracks virtual cursor position; sends switch_out when cursor hits the
    boundary edge, returning control to the server

Linux note: injecting input via pynput may require the user to be in the
'input' group or to run as root:
    sudo usermod -a -G input $USER   (then log out and back in)
"""

import socket
import threading
import logging
import time

from pynput.mouse import Controller as MouseCtrl
from pynput.keyboard import Controller as KeyCtrl

from config import (
    DEFAULT_PORT, RECONNECT_DELAY, SOCKET_TIMEOUT,
    get_screen_size, send_msg, recv_msg,
    deserialize_key, deserialize_button,
    MSG_SCREEN_INFO, MSG_SWITCH_IN, MSG_SWITCH_OUT,
    MSG_MOUSE_MOVE, MSG_MOUSE_CLICK, MSG_MOUSE_SCROLL, MSG_KEY,
    MSG_PING, MSG_PONG,
)

logger = logging.getLogger(__name__)


class KVMClient:
    """
    Connects to a KVMServer and injects forwarded input events.

    Parameters
    ----------
    server_ip : IP address of the machine running server.py
    port      : must match the server's port
    """

    def __init__(self, server_ip: str, port: int = DEFAULT_PORT):
        self.server_ip = server_ip
        self.port = port

        self.screen_w, self.screen_h = get_screen_size()
        self._mouse_ctrl = MouseCtrl()
        self._key_ctrl = KeyCtrl()

        # Control state
        self._active = False          # True when we currently have control
        self._entry_side = 'left'     # which edge the cursor entered from
        self._virtual_x = self.screen_w // 2
        self._virtual_y = self.screen_h // 2

        self._server_screen = {'w': 1920, 'h': 1080}  # updated on connect

        self._sock: socket.socket | None = None
        self._running = False

        # Optional UI callback: fn(str) -> None
        self.status_callback = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._connect_loop, daemon=True,
                         name='kvm-connect').start()

    def stop(self) -> None:
        self._running = False
        self._active = False
        sock = self._sock
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        self._set_status("Stopped")

    # ── Status helper ─────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        logger.info(msg)
        if self.status_callback:
            try:
                self.status_callback(msg)
            except Exception:
                pass

    # ── Connection loop ───────────────────────────────────────────────────────

    def _connect_loop(self) -> None:
        while self._running:
            try:
                self._set_status(f"Connecting to {self.server_ip}:{self.port}…")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(SOCKET_TIMEOUT)
                sock.connect((self.server_ip, self.port))
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = sock
                self._set_status(f"Connected to {self.server_ip}")
                self._session(sock)
            except ConnectionRefusedError:
                self._set_status(
                    f"Connection refused — retrying in {RECONNECT_DELAY:.0f}s…"
                )
            except socket.timeout:
                self._set_status("Connection timed out — retrying…")
            except OSError as e:
                self._set_status(f"Network error: {e} — retrying…")
            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
                self._set_status(f"Error: {e} — retrying…")
            finally:
                self._active = False
                self._sock = None

            if self._running:
                time.sleep(RECONNECT_DELAY)

    def _session(self, sock: socket.socket) -> None:
        """Handle one connected session until the socket closes."""
        # Announce our screen size
        send_msg(sock, {
            'type': MSG_SCREEN_INFO,
            'w': self.screen_w,
            'h': self.screen_h,
        })

        # Learn the server's screen size
        msg = recv_msg(sock)
        if msg and msg.get('type') == MSG_SCREEN_INFO:
            self._server_screen = {'w': msg['w'], 'h': msg['h']}
            logger.info(f"Server screen: {msg['w']}×{msg['h']}")

        while self._running:
            msg = recv_msg(sock)
            if msg is None:
                break
            self._dispatch(sock, msg)

    # ── Event dispatch ────────────────────────────────────────────────────────

    def _dispatch(self, sock: socket.socket, msg: dict) -> None:
        t = msg.get('type')

        if t == MSG_SWITCH_IN:
            self._on_switch_in(msg)

        elif t == MSG_SWITCH_OUT:
            self._active = False
            logger.debug("← control returned to server")

        elif t == MSG_MOUSE_MOVE:
            self._on_move(sock, msg)

        elif t == MSG_MOUSE_CLICK:
            self._on_click(msg)

        elif t == MSG_MOUSE_SCROLL:
            self._on_scroll(msg)

        elif t == MSG_KEY:
            self._on_key(msg)

        elif t == MSG_PING:
            try:
                send_msg(sock, {'type': MSG_PONG})
            except OSError:
                pass

    # ── Input injection ───────────────────────────────────────────────────────

    def _on_switch_in(self, msg: dict) -> None:
        self._entry_side = msg.get('entry_side', 'left')
        raw_y = msg.get('y', self.screen_h // 2)
        self._virtual_y = max(0, min(self.screen_h - 1, raw_y))

        if self._entry_side == 'left':
            self._virtual_x = 1
        else:
            self._virtual_x = self.screen_w - 2

        try:
            self._mouse_ctrl.position = (self._virtual_x, self._virtual_y)
        except Exception as e:
            logger.debug(f"position set error: {e}")

        self._active = True
        logger.debug(
            f"→ control received (entry_side={self._entry_side}, "
            f"y={self._virtual_y})"
        )

    def _on_move(self, sock: socket.socket, msg: dict) -> None:
        if not self._active:
            return

        dx = msg.get('dx', 0)
        dy = msg.get('dy', 0)
        new_x = self._virtual_x + dx
        new_y = max(0, min(self.screen_h - 1, self._virtual_y + dy))

        # Check if cursor has crossed back to the server's side
        if self._entry_side == 'left' and new_x < 0:
            self._return_control(sock)
            return
        if self._entry_side == 'right' and new_x >= self.screen_w:
            self._return_control(sock)
            return

        self._virtual_x = max(0, min(self.screen_w - 1, new_x))
        self._virtual_y = new_y

        try:
            self._mouse_ctrl.position = (self._virtual_x, self._virtual_y)
        except Exception as e:
            logger.debug(f"mouse move error: {e}")

    def _on_click(self, msg: dict) -> None:
        button = deserialize_button(msg.get('button', 'left'))
        try:
            if msg.get('pressed'):
                self._mouse_ctrl.press(button)
            else:
                self._mouse_ctrl.release(button)
        except Exception as e:
            logger.debug(f"click inject error: {e}")

    def _on_scroll(self, msg: dict) -> None:
        try:
            self._mouse_ctrl.scroll(msg.get('dx', 0), msg.get('dy', 0))
        except Exception as e:
            logger.debug(f"scroll inject error: {e}")

    def _on_key(self, msg: dict) -> None:
        key = deserialize_key(msg)
        if key is None:
            return
        try:
            if msg.get('action') == 'press':
                self._key_ctrl.press(key)
            else:
                self._key_ctrl.release(key)
        except Exception as e:
            logger.debug(f"key inject error: {e}")

    # ── Return control ────────────────────────────────────────────────────────

    def _return_control(self, sock: socket.socket) -> None:
        """Send switch_out to the server and deactivate."""
        self._active = False
        y_mapped = int(self._virtual_y * self._server_screen['h'] / self.screen_h)
        try:
            send_msg(sock, {'type': MSG_SWITCH_OUT, 'y': y_mapped})
        except OSError as e:
            logger.debug(f"switch_out send error: {e}")
        logger.debug("← returned control to server")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s — %(message)s',
    )

    ap = argparse.ArgumentParser(description='Software KVM client')
    ap.add_argument('server_ip', help='IP address of the KVM server')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    client = KVMClient(server_ip=args.server_ip, port=args.port)
    client.start()

    print(f"\nKVM Client running")
    print(f"  Screen  : {client.screen_w}×{client.screen_h}")
    print(f"  Server  : {args.server_ip}:{args.port}")
    print("\nWaiting for server to transfer control…")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping…")
        client.stop()


if __name__ == '__main__':
    main()
