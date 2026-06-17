"""
KVM Server — runs on the machine that owns the physical keyboard and mouse.

Responsibilities:
  • Captures all mouse/keyboard input via pynput (suppress=True)
  • In local mode  : re-injects events so the server machine behaves normally
  • In remote mode : forwards events over TCP; cursor stays pinned at the screen edge
  • Detects screen-edge crossings and sends switch_in / switch_out signals

Linux note: suppress=True on the keyboard listener requires either root or
membership of the 'input' group:
    sudo usermod -a -G input $USER   (then log out and back in)
Mouse suppress has the same requirement.
"""

import socket
import threading
import logging
import time
import sys

from pynput import mouse, keyboard
from pynput.mouse import Controller as MouseCtrl
from pynput.keyboard import Controller as KeyCtrl

from config import (
    DEFAULT_PORT, EDGE_THRESHOLD,
    get_screen_size, send_msg, recv_msg,
    serialize_key, serialize_button,
    MSG_SCREEN_INFO, MSG_SWITCH_IN, MSG_SWITCH_OUT,
    MSG_MOUSE_MOVE, MSG_MOUSE_CLICK, MSG_MOUSE_SCROLL, MSG_KEY,
)

logger = logging.getLogger(__name__)


class KVMServer:
    """
    Starts a TCP server and manages mouse/keyboard capture + forwarding.

    Parameters
    ----------
    port        : TCP port to listen on
    remote_side : 'left' or 'right' — which side of the server screen the
                  remote machine is on
    """

    def __init__(self, port: int = DEFAULT_PORT, remote_side: str = 'right',
                 edge_threshold: int = EDGE_THRESHOLD):
        self.port = port
        self.remote_side = remote_side
        self.edge_threshold = edge_threshold

        self.screen_w, self.screen_h = get_screen_size()
        self._mouse_ctrl = MouseCtrl()
        self._key_ctrl = KeyCtrl()

        # State
        self._remote_mode = False
        self._client_sock: socket.socket | None = None
        self._client_lock = threading.Lock()
        self._client_screen = {'w': 1920, 'h': 1080}

        # Remote-mode delta tracking (accumulate raw positions, send deltas)
        self._last_raw_x = 0
        self._last_raw_y = 0

        # Suppress fallback tracking
        self._mouse_suppress = True
        self._key_suppress = True

        # Diagnostics: have we seen the first event from each listener?
        self._move_seen = False
        self._key_seen = False

        # Cooldown: right after leaving remote mode, the freshly (re)started
        # local listener can deliver a stale on_move callback for the
        # not-yet-warped cursor position (still sitting at the edge),
        # which would immediately re-trigger remote mode and cause a
        # rapid enter/exit oscillation. Edge detection is suppressed
        # until this deadline.
        self._edge_cooldown_until = 0.0

        self._running = False
        self._server_sock: socket.socket | None = None
        self._mouse_listener = None
        self._key_listener = None

        # Optional callback for UI status updates: fn(str) -> None
        self.status_callback = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._start_tcp_server()
        self._start_mouse_listener()
        self._start_key_listener()
        logger.info(
            f"Server started — screen {self.screen_w}×{self.screen_h}, "
            f"port {self.port}, remote on {self.remote_side} "
            f"(mouse_suppress={self._mouse_suppress}, key_suppress={self._key_suppress})"
        )
        self._set_status("Waiting for client…")

    def stop(self) -> None:
        self._running = False
        self._remote_mode = False

        for thing in (self._server_sock, self._client_sock,
                      self._mouse_listener, self._key_listener):
            if thing:
                try:
                    thing.stop() if hasattr(thing, 'stop') else thing.close()
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

    # ── TCP server ────────────────────────────────────────────────────────────

    def _start_tcp_server(self) -> None:
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(('0.0.0.0', self.port))
        self._server_sock.listen(1)
        threading.Thread(target=self._accept_loop, daemon=True, name='kvm-accept').start()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                self._server_sock.settimeout(1.0)
                conn, addr = self._server_sock.accept()
                logger.info(f"Client connected from {addr[0]}:{addr[1]}")
                self._set_status(f"Connected: {addr[0]}")
                threading.Thread(
                    target=self._handle_client, args=(conn, addr),
                    daemon=True, name='kvm-client'
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, conn: socket.socket, addr) -> None:
        with self._client_lock:
            # Drop any previous connection
            old = self._client_sock
            self._client_sock = conn
        if old:
            try:
                old.close()
            except Exception:
                pass

        try:
            conn.settimeout(10.0)

            # Exchange screen dimensions
            msg = recv_msg(conn)
            if msg and msg.get('type') == MSG_SCREEN_INFO:
                self._client_screen = {'w': msg['w'], 'h': msg['h']}
                logger.info(f"Client screen: {msg['w']}×{msg['h']}")

            send_msg(conn, {
                'type': MSG_SCREEN_INFO,
                'w': self.screen_w,
                'h': self.screen_h,
            })

            conn.settimeout(None)

            # Listen for upstream messages (switch_out, pong, …)
            while self._running:
                msg = recv_msg(conn)
                if not msg:
                    break
                self._on_client_msg(conn, msg)

        except Exception as e:
            logger.error(f"Client session error: {e}")
        finally:
            with self._client_lock:
                if self._client_sock is conn:
                    self._client_sock = None
            if self._remote_mode:
                self._exit_remote()
            try:
                conn.close()
            except Exception:
                pass
            self._set_status("Client disconnected — waiting…")

    def _on_client_msg(self, conn: socket.socket, msg: dict) -> None:
        t = msg.get('type')
        if t == MSG_SWITCH_OUT:
            # Client cursor reached the boundary edge and returned control
            raw_y = msg.get('y', self.screen_h // 2)
            self._exit_remote(client_y=raw_y)

    # ── Listeners ─────────────────────────────────────────────────────────────
    #
    # IMPORTANT: suppress=True installs a global OS-level hook that blocks
    # EVERY keystroke/click before it reaches any app — including the
    # synthetic ones we re-inject via Controller (SendInput-generated events
    # pass through the same global hook chain and get swallowed again). So we
    # must only suppress while actually in remote mode; in local mode the
    # listener runs with suppress=False (pure pass-through, no re-injection
    # needed) and we just observe events for edge detection.

    def _start_mouse_listener(self) -> None:
        self._mouse_listener = self._make_mouse_listener(suppress=False)

    def _start_key_listener(self) -> None:
        self._key_listener = self._make_key_listener(suppress=False)

    def _make_mouse_listener(self, suppress: bool):
        """Create and start a mouse listener; fall back to the opposite
        suppress value with a warning if construction fails."""
        for s in (suppress, not suppress):
            try:
                ml = mouse.Listener(
                    on_move=self._on_move,
                    on_click=self._on_click,
                    on_scroll=self._on_scroll,
                    suppress=s,
                )
                ml.start()
                self._mouse_suppress = s
                if s != suppress:
                    logger.warning(
                        f"Mouse listener suppress={suppress} failed — running "
                        f"with suppress={s} instead."
                    )
                return ml
            except Exception as e:
                logger.warning(f"Mouse listener suppress={s} failed: {e}")
        return None

    def _make_key_listener(self, suppress: bool):
        """Create and start a keyboard listener; fall back to the opposite
        suppress value with a warning if construction fails."""
        for s in (suppress, not suppress):
            try:
                kl = keyboard.Listener(
                    on_press=self._on_key_press,
                    on_release=self._on_key_release,
                    suppress=s,
                )
                kl.start()
                self._key_suppress = s
                if s != suppress:
                    logger.warning(
                        f"Keyboard listener suppress={suppress} failed — running "
                        f"with suppress={s} instead."
                    )
                return kl
            except Exception as e:
                logger.warning(f"Keyboard listener suppress={s} failed: {e}")
        return None

    def _set_suppress_mode(self, suppress: bool) -> None:
        """Swap the mouse/keyboard listeners to the given suppress mode.

        Called from inside a listener callback's own thread (entering remote
        mode) or from the client-handler thread (exiting remote mode); both
        are safe since Listener.stop() only signals the hook thread to unwind
        after the current callback returns.
        """
        old_mouse, old_key = self._mouse_listener, self._key_listener
        self._mouse_listener = self._make_mouse_listener(suppress)
        self._key_listener = self._make_key_listener(suppress)
        for old in (old_mouse, old_key):
            if old:
                try:
                    old.stop()
                except Exception:
                    pass

    # ── Mouse callbacks ───────────────────────────────────────────────────────

    def _on_move(self, x: int, y: int) -> None:
        if self._remote_mode:
            dx = x - self._last_raw_x
            dy = y - self._last_raw_y
            self._last_raw_x = x
            self._last_raw_y = y
            if dx or dy:
                self._send({'type': MSG_MOUSE_MOVE, 'dx': dx, 'dy': dy})
            # No re-injection → cursor stays pinned at edge on server screen
        else:
            # suppress=False in local mode: the OS already moved the cursor
            # normally, we're just observing for edge detection.

            if not self._move_seen:
                self._move_seen = True
                logger.debug(f"on_move alive — first event x={x} y={y}")
            near_edge = (self.remote_side == 'right' and x >= self.screen_w - 60) or \
                        (self.remote_side == 'left' and x <= 60)
            if near_edge:
                logger.debug(f"near edge: x={x} y={y} screen_w={self.screen_w}")

            # Edge detection — only switch if a client is connected
            with self._client_lock:
                has_client = self._client_sock is not None
            if not has_client:
                return

            if time.monotonic() < self._edge_cooldown_until:
                return

            if self.remote_side == 'right' and x >= self.screen_w - self.edge_threshold:
                self._enter_remote(x, y)
            elif self.remote_side == 'left' and x <= self.edge_threshold:
                self._enter_remote(x, y)

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        if self._remote_mode:
            self._send({
                'type': MSG_MOUSE_CLICK,
                'button': serialize_button(button),
                'pressed': pressed,
            })

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        if self._remote_mode:
            self._send({'type': MSG_MOUSE_SCROLL, 'dx': dx, 'dy': dy})

    # ── Keyboard callbacks ────────────────────────────────────────────────────

    def _on_key_press(self, key) -> None:
        if not self._key_seen:
            self._key_seen = True
            logger.debug(f"on_key_press alive — first event key={key!r}")
        if self._remote_mode:
            self._send({'type': MSG_KEY, 'action': 'press', **serialize_key(key)})

    def _on_key_release(self, key) -> None:
        if self._remote_mode:
            self._send({'type': MSG_KEY, 'action': 'release', **serialize_key(key)})

    # ── Mode switching ────────────────────────────────────────────────────────

    def _enter_remote(self, local_x: int, local_y: int) -> None:
        """Switch to remote mode: pin cursor, notify client."""
        self._remote_mode = True
        self._set_suppress_mode(True)

        # Anchor the raw-position tracker at the actual position the cursor
        # crossed the edge at — NOT an artificial edge coordinate. Using a
        # fake anchor here would make the first move event's delta wrong
        # (real_x - fake_anchor), producing a bogus jump that could even
        # immediately push the client back across its own boundary.
        self._last_raw_x = local_x
        self._last_raw_y = local_y

        # Map y proportionally to client screen height
        client_y = int(local_y * self._client_screen['h'] / self.screen_h)

        # The client cursor enters from the side facing the server
        entry_side = 'left' if self.remote_side == 'right' else 'right'

        self._send({
            'type': MSG_SWITCH_IN,
            'y': client_y,
            'entry_side': entry_side,
        })
        logger.debug(f"→ remote mode (client entry_side={entry_side}, y={client_y})")

    def _exit_remote(self, client_y: int | None = None) -> None:
        """Return to local mode; warp cursor back to the boundary edge."""
        self._remote_mode = False
        # Block edge re-triggering until the cursor has been warped away
        # from the edge and any stale/in-flight move events have drained.
        self._edge_cooldown_until = time.monotonic() + 0.4
        self._set_suppress_mode(False)

        if client_y is not None:
            local_y = int(client_y * self.screen_h / self._client_screen['h'])
            local_y = max(0, min(self.screen_h - 1, local_y))
            # Cursor reappears on the same side it left from, but pulled back
            # far enough that it doesn't immediately fall inside the
            # edge-trigger zone again (which would bounce straight back into
            # remote mode and oscillate).
            margin = self.edge_threshold + 10
            if self.remote_side == 'right':
                local_x = max(0, self.screen_w - margin)
            else:
                local_x = min(self.screen_w - 1, margin)
            try:
                self._mouse_ctrl.position = (local_x, local_y)
            except Exception:
                pass

        logger.debug("← local mode")

    # ── Network send ──────────────────────────────────────────────────────────

    def _send(self, data: dict) -> None:
        with self._client_lock:
            sock = self._client_sock
        if sock is None:
            return
        try:
            send_msg(sock, data)
        except OSError as e:
            logger.warning(f"Send failed: {e}")
            with self._client_lock:
                if self._client_sock is sock:
                    self._client_sock = None
            if self._remote_mode:
                self._exit_remote()


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s — %(message)s',
    )

    ap = argparse.ArgumentParser(description='Software KVM server')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--side', choices=['left', 'right'], default='right',
                    help='Which side of THIS screen the remote machine is on')
    ap.add_argument('--threshold', type=int, default=EDGE_THRESHOLD,
                    help='Pixels from edge that trigger a switch')
    args = ap.parse_args()

    server = KVMServer(port=args.port, remote_side=args.side,
                       edge_threshold=args.threshold)
    server.start()

    sw, sh = server.screen_w, server.screen_h
    print(f"\nKVM Server running")
    print(f"  Screen : {sw}×{sh}")
    print(f"  Port   : {args.port}")
    print(f"  Remote : {args.side} side")
    print(f"\nMove the mouse to the {args.side} edge to transfer control.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping…")
        server.stop()


if __name__ == '__main__':
    main()
