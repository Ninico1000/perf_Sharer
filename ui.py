"""
Configuration UI — tkinter window for setting up the KVM server or client.

Run directly:
    python ui.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import logging
import threading
import socket

from config import DEFAULT_PORT, get_screen_size

logger = logging.getLogger(__name__)


# ── Colour palette ────────────────────────────────────────────────────────────
CLR_SERVER = '#3d7abf'   # blue
CLR_REMOTE = '#e07b39'   # orange
CLR_BG     = '#f4f4f4'
CLR_GREEN  = '#4caf50'
CLR_YELLOW = '#ffc107'
CLR_GREY   = '#9e9e9e'
CLR_RED    = '#f44336'


class KVMApp:
    """Main application window."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title('Software KVM')
        self.root.configure(bg=CLR_BG)
        self.root.resizable(False, False)

        self._kvm = None        # KVMServer | KVMClient
        self._running = False

        self._build_ui()
        self._refresh_local_info()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = self.root
        PAD = dict(padx=10, pady=5)

        # ── Title bar ────────────────────────────────────────────────────────
        title = tk.Label(root, text='⌨  Software KVM', font=('Helvetica', 16, 'bold'),
                         bg=CLR_BG, fg='#333')
        title.grid(row=0, column=0, columnspan=2, pady=(12, 4))

        # ── Mode ─────────────────────────────────────────────────────────────
        mf = ttk.LabelFrame(root, text='Mode')
        mf.grid(row=1, column=0, columnspan=2, sticky='ew', **PAD)

        self._mode = tk.StringVar(value='server')
        ttk.Radiobutton(mf, text='Server  (owns the keyboard & mouse)',
                        variable=self._mode, value='server',
                        command=self._on_mode_change).pack(anchor='w', padx=6, pady=2)
        ttk.Radiobutton(mf, text='Client  (receives control from server)',
                        variable=self._mode, value='client',
                        command=self._on_mode_change).pack(anchor='w', padx=6, pady=2)

        # ── Connection ───────────────────────────────────────────────────────
        cf = ttk.LabelFrame(root, text='Connection')
        cf.grid(row=2, column=0, columnspan=2, sticky='ew', **PAD)
        cf.columnconfigure(1, weight=1)

        ttk.Label(cf, text='Server IP:').grid(row=0, column=0, sticky='w', **PAD)
        self._ip = tk.StringVar(value='')
        self._ip_entry = ttk.Entry(cf, textvariable=self._ip, width=22)
        self._ip_entry.grid(row=0, column=1, sticky='ew', **PAD)
        self._ip_hint = ttk.Label(cf, text='(enter server IP when in Client mode)',
                                  foreground='grey')
        self._ip_hint.grid(row=0, column=2, sticky='w', padx=(0, 10))

        ttk.Label(cf, text='Port:').grid(row=1, column=0, sticky='w', **PAD)
        self._port = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(cf, textvariable=self._port, width=8).grid(row=1, column=1,
                                                              sticky='w', **PAD)

        # ── Screen layout ─────────────────────────────────────────────────────
        lf = ttk.LabelFrame(root, text='Screen Layout  (server perspective)')
        lf.grid(row=3, column=0, columnspan=2, sticky='ew', **PAD)

        self._side = tk.StringVar(value='right')

        btn_row = ttk.Frame(lf)
        btn_row.pack(pady=(4, 0))
        ttk.Radiobutton(btn_row, text='Remote on LEFT', variable=self._side,
                        value='left', command=self._draw_layout).pack(side='left', padx=8)
        ttk.Radiobutton(btn_row, text='Remote on RIGHT', variable=self._side,
                        value='right', command=self._draw_layout).pack(side='left', padx=8)

        self._canvas = tk.Canvas(lf, width=220, height=70, bg=CLR_BG,
                                  highlightthickness=1, highlightbackground='#ccc')
        self._canvas.pack(pady=6)
        self._draw_layout()

        # ── Local info ────────────────────────────────────────────────────────
        info_frame = ttk.LabelFrame(root, text='This Machine')
        info_frame.grid(row=4, column=0, columnspan=2, sticky='ew', **PAD)

        self._local_info = tk.StringVar(value='…')
        ttk.Label(info_frame, textvariable=self._local_info,
                  foreground='#555').pack(padx=8, pady=4, anchor='w')

        # ── Status ────────────────────────────────────────────────────────────
        sf = ttk.LabelFrame(root, text='Status')
        sf.grid(row=5, column=0, columnspan=2, sticky='ew', **PAD)

        status_row = ttk.Frame(sf)
        status_row.pack(fill='x', padx=8, pady=6)

        ttk_bg = ttk.Style().lookup('TFrame', 'background') or CLR_BG
        self._dot = tk.Canvas(status_row, width=14, height=14,
                               highlightthickness=0, bg=ttk_bg)
        self._dot.pack(side='left')
        self._dot_id = self._dot.create_oval(2, 2, 12, 12, fill=CLR_GREY, outline='')

        self._status = tk.StringVar(value='Stopped')
        ttk.Label(status_row, textvariable=self._status,
                  font=('Helvetica', 9, 'bold')).pack(side='left', padx=6)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_frame = ttk.Frame(root)
        btn_frame.grid(row=6, column=0, columnspan=2, pady=10)

        self._toggle_btn = ttk.Button(btn_frame, text='Start', width=12,
                                       command=self._toggle)
        self._toggle_btn.pack(side='left', padx=6)
        ttk.Button(btn_frame, text='Quit', width=8,
                   command=self._quit).pack(side='left', padx=6)

        self._on_mode_change()
        root.protocol('WM_DELETE_WINDOW', self._quit)

    # ── Layout canvas ─────────────────────────────────────────────────────────

    def _draw_layout(self, *_) -> None:
        c = self._canvas
        c.delete('all')
        side = self._side.get()

        W, H = 220, 70
        mid = W // 2
        margin = 10

        def rect(x0, y0, x1, y1, fill, label):
            c.create_rectangle(x0, y0, x1, y1, fill=fill, outline='#555', width=1)
            c.create_text((x0 + x1) / 2, (y0 + y1) / 2,
                          text=label, fill='white',
                          font=('Helvetica', 9, 'bold'))

        if side == 'right':
            rect(margin, 8, mid - 2, H - 8, CLR_SERVER, 'Server\n(you)')
            rect(mid + 2, 8, W - margin, H - 8, CLR_REMOTE, 'Remote')
            c.create_text(mid, H // 2, text='→', font=('Helvetica', 14, 'bold'),
                          fill='#555')
        else:
            rect(margin, 8, mid - 2, H - 8, CLR_REMOTE, 'Remote')
            rect(mid + 2, 8, W - margin, H - 8, CLR_SERVER, 'Server\n(you)')
            c.create_text(mid, H // 2, text='←', font=('Helvetica', 14, 'bold'),
                          fill='#555')

    # ── Mode change ───────────────────────────────────────────────────────────

    def _on_mode_change(self, *_) -> None:
        is_client = self._mode.get() == 'client'
        self._ip_entry.configure(state='normal' if is_client else 'disabled')

    # ── Start / stop ──────────────────────────────────────────────────────────

    def _toggle(self) -> None:
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        try:
            port = int(self._port.get())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror('Invalid port', 'Port must be a number between 1 and 65535.')
            return

        mode = self._mode.get()
        side = self._side.get()

        self._running = True
        self._toggle_btn.configure(text='Stop')
        self._set_dot(CLR_YELLOW)
        self._apply_status(f"{'Server' if mode == 'server' else 'Client'} starting…")

        if mode == 'server':
            from server import KVMServer
            self._kvm = KVMServer(port=port, remote_side=side)
            self._kvm.status_callback = self._on_status
            self._kvm.start()
        else:
            ip = self._ip.get().strip()
            if not ip:
                messagebox.showerror('No IP', 'Enter the server IP address.')
                self._running = False
                self._toggle_btn.configure(text='Start')
                self._set_dot(CLR_GREY)
                return
            from client import KVMClient
            self._kvm = KVMClient(server_ip=ip, port=port)
            self._kvm.status_callback = self._on_status
            self._kvm.start()

    def _stop(self) -> None:
        if self._kvm:
            self._kvm.stop()
            self._kvm = None
        self._running = False
        self._toggle_btn.configure(text='Start')
        self._set_dot(CLR_GREY)
        self._status.set('Stopped')

    # ── Status updates ────────────────────────────────────────────────────────

    def _on_status(self, msg: str) -> None:
        """Called from background threads — schedule into the Tk main loop."""
        self.root.after(0, self._apply_status, msg)

    def _apply_status(self, msg: str) -> None:
        self._status.set(msg)
        low = msg.lower()
        if 'connected' in low:
            self._set_dot(CLR_GREEN)
        elif 'stopped' in low or 'error' in low or 'refused' in low:
            self._set_dot(CLR_GREY)
        elif 'warning' in low:
            self._set_dot(CLR_RED)
        else:
            self._set_dot(CLR_YELLOW)

    def _set_dot(self, colour: str) -> None:
        self._dot.itemconfigure(self._dot_id, fill=colour)

    # ── Local machine info ────────────────────────────────────────────────────

    def _refresh_local_info(self) -> None:
        def fetch():
            try:
                w, h = get_screen_size()
                hostname = socket.gethostname()
                try:
                    local_ip = socket.gethostbyname(hostname)
                except Exception:
                    local_ip = '(unknown)'
                info = f'{hostname}  ·  {local_ip}  ·  {w}×{h}'
            except Exception as e:
                info = f'(could not detect: {e})'
            self.root.after(0, self._local_info.set, info)

        threading.Thread(target=fetch, daemon=True).start()

    # ── Quit ─────────────────────────────────────────────────────────────────

    def _quit(self) -> None:
        self._stop()
        self.root.destroy()

    # ── Run ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s — %(message)s',
    )
    app = KVMApp()
    app.run()


if __name__ == '__main__':
    main()
