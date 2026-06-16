"""
File transfer over a dedicated TCP connection (port 24801 by default).

Architecture
────────────
  Server side  →  FileSender.send_file(path)   — called when drag crosses edge
  Client side  →  FileReceiver.start()          — always listening

Protocol (binary, little-endian)
─────────────────────────────────
  [4 bytes] filename length (uint32 BE)
  [N bytes] UTF-8 filename
  [8 bytes] file size in bytes (uint64 BE)
  [M bytes] raw file content

Drag-and-drop detection (stub)
───────────────────────────────
  Full OS-level drag detection is complex and platform-specific:
    • Windows : implement IDropTarget via pywin32 / ctypes COM
    • Linux   : implement the XDND protocol via python-xlib

  The DragDropMonitor class below provides the hook points.
  Once a drag is detected crossing the screen boundary, call:

      sender = FileSender(remote_ip)
      sender.send_file(dragged_path, on_progress)

  On the receiving end FileReceiver calls on_file_received(saved_path)
  and you can simulate a "drop" there (e.g. open the file manager folder).
"""

import os
import socket
import struct
import threading
import logging
from pathlib import Path
from typing import Callable

from config import FILE_TRANSFER_PORT, recvall

logger = logging.getLogger(__name__)

CHUNK_SIZE = 128 * 1024  # 128 KB


# ── Receiver (always running on client) ──────────────────────────────────────

class FileReceiver:
    """
    Listens for incoming file transfers and saves them to *save_dir*.

    Usage
    -----
        rcv = FileReceiver(save_dir='/home/alice/Downloads')
        rcv.on_file_received = lambda p: print(f'Saved to {p}')
        rcv.start()
    """

    def __init__(self, port: int = FILE_TRANSFER_PORT,
                 save_dir: str | None = None) -> None:
        self.port = port
        self.save_dir = save_dir or str(Path.home() / 'Downloads')
        self._running = False
        self._server_sock: socket.socket | None = None

        # Optional: fn(saved_path: str) -> None
        self.on_file_received: Callable[[str], None] | None = None

    def start(self) -> None:
        os.makedirs(self.save_dir, exist_ok=True)
        self._running = True
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(('0.0.0.0', self.port))
        self._server_sock.listen(4)
        threading.Thread(target=self._accept_loop, daemon=True,
                         name='file-recv-accept').start()
        logger.info(f"FileReceiver listening on port {self.port}, saving to {self.save_dir}")

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass

    def _accept_loop(self) -> None:
        while self._running:
            try:
                self._server_sock.settimeout(1.0)
                conn, addr = self._server_sock.accept()
                logger.info(f"Incoming file transfer from {addr[0]}")
                threading.Thread(target=self._receive, args=(conn,),
                                 daemon=True, name='file-recv').start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _receive(self, conn: socket.socket) -> None:
        try:
            # Filename
            raw = recvall(conn, 4)
            if raw is None:
                return
            name_len = struct.unpack('>I', raw)[0]
            if name_len > 4096:
                logger.error(f"Filename too long: {name_len}")
                return
            filename = recvall(conn, name_len)
            if filename is None:
                return
            filename = filename.decode('utf-8', errors='replace')

            # File size
            raw = recvall(conn, 8)
            if raw is None:
                return
            file_size = struct.unpack('>Q', raw)[0]
            if file_size > 10 * 1024 ** 3:  # 10 GB hard cap
                logger.error(f"File too large: {file_size} bytes")
                return

            logger.info(f"Receiving '{filename}' ({file_size:,} bytes)")

            # Sanitise: strip directory components
            safe_name = Path(filename).name or 'received_file'
            save_path = self._unique_path(safe_name)

            received = 0
            with open(save_path, 'wb') as fh:
                while received < file_size:
                    chunk = conn.recv(min(CHUNK_SIZE, file_size - received))
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)

            if received < file_size:
                logger.warning(f"Incomplete transfer: {received}/{file_size} bytes")
                os.remove(save_path)
                return

            logger.info(f"Saved to {save_path}")
            if self.on_file_received:
                try:
                    self.on_file_received(str(save_path))
                except Exception as e:
                    logger.debug(f"on_file_received callback error: {e}")

        except Exception as e:
            logger.error(f"File receive error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _unique_path(self, name: str) -> str:
        base = Path(self.save_dir) / name
        if not base.exists():
            return str(base)
        stem, suffix = base.stem, base.suffix
        i = 1
        while True:
            candidate = Path(self.save_dir) / f'{stem}_{i}{suffix}'
            if not candidate.exists():
                return str(candidate)
            i += 1


# ── Sender ────────────────────────────────────────────────────────────────────

class FileSender:
    """
    Sends a file to a remote FileReceiver.

    Usage
    -----
        sender = FileSender('192.168.1.50')
        ok = sender.send_file('/tmp/report.pdf',
                              on_progress=lambda s, t: print(f'{s}/{t}'))
    """

    def __init__(self, remote_ip: str, port: int = FILE_TRANSFER_PORT) -> None:
        self.remote_ip = remote_ip
        self.port = port

    def send_file(
        self,
        filepath: str | Path,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> bool:
        """
        Transfer *filepath* to the remote machine.

        Parameters
        ----------
        filepath    : path to the local file
        on_progress : optional fn(bytes_sent, total_bytes)

        Returns True on success.
        """
        filepath = Path(filepath)
        if not filepath.is_file():
            raise FileNotFoundError(filepath)

        file_size = filepath.stat().st_size
        filename = filepath.name.encode('utf-8')

        logger.info(
            f"Sending '{filepath.name}' ({file_size:,} bytes) "
            f"→ {self.remote_ip}:{self.port}"
        )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(10.0)
            sock.connect((self.remote_ip, self.port))
            sock.settimeout(None)

            # Header
            sock.sendall(struct.pack('>I', len(filename)))
            sock.sendall(filename)
            sock.sendall(struct.pack('>Q', file_size))

            # Data
            sent = 0
            with open(filepath, 'rb') as fh:
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    sent += len(chunk)
                    if on_progress:
                        try:
                            on_progress(sent, file_size)
                        except Exception:
                            pass

        success = sent == file_size
        if success:
            logger.info(f"Transfer complete ({sent:,} bytes)")
        else:
            logger.warning(f"Transfer incomplete: {sent}/{file_size} bytes")
        return success

    def send_file_async(
        self,
        filepath: str | Path,
        on_progress: Callable[[int, int], None] | None = None,
        on_done: Callable[[bool], None] | None = None,
    ) -> threading.Thread:
        """Non-blocking variant — returns the background thread."""
        def _run():
            try:
                ok = self.send_file(filepath, on_progress)
            except Exception as e:
                logger.error(f"send_file_async error: {e}")
                ok = False
            if on_done:
                try:
                    on_done(ok)
                except Exception:
                    pass

        t = threading.Thread(target=_run, daemon=True, name='file-send')
        t.start()
        return t


# ── Drag-and-drop detection stubs ────────────────────────────────────────────

class DragDropMonitor:
    """
    Hook point for OS-level drag-and-drop detection.

    To fully implement cross-boundary drag-and-drop:

    Windows (server)
    ----------------
    1. Register an IDropTarget COM object on the visible window using pywin32:
           win32con / pythoncom / shell.DragAcceptFiles
    2. In IDropTarget.DragOver, check if the cursor is near the KVM edge.
    3. In IDropTarget.Drop, grab the file path from the IDataObject.
    4. Call send_file() and cancel the local drop.

    Linux (server, X11)
    --------------------
    1. Implement the XDND protocol using python-xlib.
    2. Listen for ClientMessage events (XdndEnter, XdndPosition, XdndDrop).
    3. When position crosses the screen edge, grab the URI list from the
       selection, call send_file(), and send XdndFinished.

    Receiving end (simulate drop)
    ------------------------------
    After FileReceiver saves the file, open the destination folder:
        import subprocess, platform
        if platform.system() == 'Windows':
            subprocess.Popen(['explorer', '/select,', saved_path])
        else:
            subprocess.Popen(['xdg-open', os.path.dirname(saved_path)])
    """

    def __init__(self) -> None:
        self.is_dragging = False
        self.dragged_files: list[str] = []

    def start(self) -> None:
        logger.info(
            "DragDropMonitor: OS-level drag detection is not yet implemented. "
            "Use FileSender directly to transfer files."
        )

    def stop(self) -> None:
        pass

    def simulate_drop_notification(self, saved_path: str) -> None:
        """Open the containing folder so the user sees the arrived file."""
        import platform
        import subprocess
        system = platform.system()
        folder = os.path.dirname(saved_path)
        try:
            if system == 'Windows':
                subprocess.Popen(['explorer', f'/select,{saved_path}'])
            elif system == 'Darwin':
                subprocess.Popen(['open', folder])
            else:
                subprocess.Popen(['xdg-open', folder])
        except Exception as e:
            logger.debug(f"simulate_drop_notification error: {e}")


# ── CLI quick-test ────────────────────────────────────────────────────────────

def _cli_send():
    import argparse, sys
    ap = argparse.ArgumentParser(description='Send a file to a KVM client')
    ap.add_argument('remote_ip')
    ap.add_argument('file', help='Path to the file to send')
    ap.add_argument('--port', type=int, default=FILE_TRANSFER_PORT)
    args = ap.parse_args()

    def progress(sent, total):
        pct = sent * 100 // total
        print(f'\r  {pct:3d}%  {sent:,}/{total:,} bytes', end='', flush=True)

    sender = FileSender(args.remote_ip, port=args.port)
    ok = sender.send_file(args.file, on_progress=progress)
    print()
    sys.exit(0 if ok else 1)


def _cli_receive():
    import argparse
    ap = argparse.ArgumentParser(description='Receive files (run on KVM client)')
    ap.add_argument('--port', type=int, default=FILE_TRANSFER_PORT)
    ap.add_argument('--save-dir', default=str(Path.home() / 'Downloads'))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    rcv = FileReceiver(port=args.port, save_dir=args.save_dir)
    rcv.on_file_received = lambda p: print(f'Received: {p}')
    rcv.start()
    print(f'Waiting for files on port {args.port} → {args.save_dir}')
    print('Press Ctrl+C to stop.')
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        rcv.stop()


if __name__ == '__main__':
    import sys
    if '--receive' in sys.argv:
        sys.argv.remove('--receive')
        _cli_receive()
    else:
        _cli_send()
