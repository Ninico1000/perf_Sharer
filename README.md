# perf_Sharer — Software KVM

Control two laptops (Windows + Linux) with one keyboard and mouse over LAN.

## How it works

```
┌─────────────────────────┐     TCP :24800      ┌──────────────────────────┐
│  SERVER (has KB+mouse)  │◄───────────────────►│  CLIENT (remote machine) │
│                         │                     │                          │
│  pynput captures input  │  mouse/key events   │  pynput injects input    │
│  cursor → right edge    │──────────────────►  │  cursor starts at left   │
│  cursor ← right edge    │◄──────────────────  │  edge → moves freely     │
└─────────────────────────┘    switch_out        └──────────────────────────┘
```

Moving the mouse past the configured screen edge transfers control to the
remote machine. Moving it back returns control.

---

## Quick start

### Install dependencies

```bash
pip install pynput
```

### On the server (machine with physical keyboard + mouse)

```bash
python server.py --side right          # remote screen is to the right
# or
python ui.py                           # graphical launcher
```

### On the client (other laptop)

```bash
python client.py 192.168.1.X           # replace with server's IP
# or
python ui.py                           # graphical launcher → Client mode
```

---

## Files

| File | Purpose |
|---|---|
| `server.py` | Captures input, detects edge, forwards events |
| `client.py` | Receives events, injects them via pynput |
| `config.py` | Shared constants, protocol helpers, serialization |
| `ui.py` | tkinter configuration & status window |
| `transfer.py` | File transfer over a second TCP connection (port 24801) |

---

## Configuration

### server.py CLI

```
python server.py [--port PORT] [--side {left,right}] [--threshold N]
```

| Flag | Default | Description |
|---|---|---|
| `--port` | 24800 | TCP port |
| `--side` | right | Which side the remote screen is on |
| `--threshold` | 4 | Pixels from edge that trigger a switch |

### client.py CLI

```
python client.py SERVER_IP [--port PORT]
```

---

## Linux permissions

pynput needs access to `/dev/input/*` to suppress events.  
Without this the server captures input but **cannot suppress** it —
keys and clicks will fire on both machines simultaneously.

```bash
# Add yourself to the input group (log out and back in after)
sudo usermod -a -G input $USER

# Alternatively run as root (not recommended)
sudo python server.py
```

On **Wayland** pynput suppression is not supported.  Use X11 (`DISPLAY=:0`).

---

## File transfer

Files can be sent manually:

```bash
# On the receiving machine (always listening)
python transfer.py --receive --save-dir ~/Downloads

# On the sending machine
python transfer.py 192.168.1.X /path/to/file.pdf
```

Drag-and-drop detection across the screen boundary requires OS-level hooks
(IDropTarget on Windows, XDND on Linux) — see the `DragDropMonitor` stub in
`transfer.py` for implementation notes.

---

## Topology

```
[Server screen]  ←edge→  [Remote screen]

      ←  side='left'   means remote is left of server
      →  side='right'  means remote is right of server (default)
```

The `--side` flag is always from the **server's** point of view.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Keys/clicks fire on server in remote mode | Add user to `input` group (Linux) |
| Client can't connect | Check firewall allows TCP 24800 on server |
| Cursor jitter at edge | Increase `--threshold` slightly |
| Cursor jumps to wrong Y position | Screen heights differ; y is mapped proportionally |
| Wayland: suppression not working | Switch to X11 session |
