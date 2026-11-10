"""Serial Monitor - Tkinter GUI.

Features
--------
* Serial port settings: port, baud rate, data bits, parity, stop bits,
  flow control, DTR / RTS lines.
* Monitor received (and optionally sent) data as ASCII, HEX or HEX+ASCII,
  with optional timestamps, auto-scroll, log saving.
* Send commands as ASCII (with escape sequences like ``\\r\\n``, ``\\x1B``)
  or HEX (``01 03 00 00 00 0A``), with selectable line ending, command
  history, periodic (repeat) sending and user-defined quick-command macros.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover - handled at runtime
    serial = None

from . import codec

APP_TITLE = "Serial Monitor"
CONFIG_PATH = Path.home() / ".serial_monitor.json"

BAUD_RATES = [
    "300", "1200", "2400", "4800", "9600", "14400", "19200", "28800",
    "38400", "57600", "76800", "115200", "230400", "250000", "460800",
    "500000", "921600", "1000000", "2000000",
]
DATA_BITS = ["5", "6", "7", "8"]
PARITIES = ["None", "Even", "Odd", "Mark", "Space"]
STOP_BITS = ["1", "1.5", "2"]
FLOW_CONTROLS = ["None", "RTS/CTS", "XON/XOFF", "DSR/DTR"]
DISPLAY_MODES = ["ASCII", "HEX", "HEX + ASCII"]
NUM_MACROS = 8

DEFAULT_CONFIG = {
    "port": "",
    "baud": "9600",
    "data_bits": "8",
    "parity": "None",
    "stop_bits": "1",
    "flow": "None",
    "dtr": True,
    "rts": True,
    "display_mode": "ASCII",
    "timestamps": False,
    "show_tx": True,
    "autoscroll": True,
    "show_control": False,
    "send_mode": "ASCII",
    "line_ending": "CR+LF (\\r\\n)",
    "escapes": True,
    "repeat_ms": "1000",
    "history": [],
    "macros": [
        {"name": f"M{i + 1}", "data": "", "mode": "ASCII"} for i in range(NUM_MACROS)
    ],
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in cfg})
    except (OSError, ValueError):
        pass
    macros = list(cfg.get("macros") or [])[:NUM_MACROS]
    while len(macros) < NUM_MACROS:
        macros.append({"name": f"M{len(macros) + 1}", "data": "", "mode": "ASCII"})
    cfg["macros"] = macros
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


class SerialWorker:
    """Owns the serial port and a background reader thread."""

    def __init__(self, rx_queue: "queue.Queue"):
        self.rx_queue = rx_queue
        self.ser = None
        self._thread = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def open(self, port, baud, data_bits, parity, stop_bits, flow, dtr, rts):
        parity_map = {
            "None": serial.PARITY_NONE,
            "Even": serial.PARITY_EVEN,
            "Odd": serial.PARITY_ODD,
            "Mark": serial.PARITY_MARK,
            "Space": serial.PARITY_SPACE,
        }
        stop_map = {
            "1": serial.STOPBITS_ONE,
            "1.5": serial.STOPBITS_ONE_POINT_FIVE,
            "2": serial.STOPBITS_TWO,
        }
        bytesize_map = {
            "5": serial.FIVEBITS,
            "6": serial.SIXBITS,
            "7": serial.SEVENBITS,
            "8": serial.EIGHTBITS,
        }
        ser = serial.serial_for_url(port, do_not_open=True)
        ser.baudrate = int(baud)
        ser.bytesize = bytesize_map[data_bits]
        ser.parity = parity_map[parity]
        ser.stopbits = stop_map[stop_bits]
        ser.timeout = 0.05
        ser.write_timeout = 2
        ser.rtscts = flow == "RTS/CTS"
        ser.xonxoff = flow == "XON/XOFF"
        ser.dsrdtr = flow == "DSR/DTR"
        # Set the modem lines before opening so devices that reset on
        # DTR (e.g. Arduino) behave as the user configured.
        if flow != "DSR/DTR":
            ser.dtr = dtr
        if flow != "RTS/CTS":
            ser.rts = rts
        ser.open()
        self.ser = ser
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise IOError("Port is not open")
        with self._write_lock:
            n = self.ser.write(data)
            self.ser.flush()
            return n or 0

    def set_dtr(self, value: bool):
        if self.is_open:
            self.ser.dtr = value

    def set_rts(self, value: bool):
        if self.is_open:
            self.ser.rts = value

    def modem_status(self):
        """Return (CTS, DSR, RI, CD) or None if unavailable."""
        if not self.is_open:
            return None
        try:
            s = self.ser
            return s.cts, s.dsr, s.ri, s.cd
        except Exception:
            return None

    def _reader(self):
        while not self._stop.is_set():
            try:
                waiting = self.ser.in_waiting
                data = self.ser.read(waiting or 1)
            except Exception as exc:  # device unplugged etc.
                self.rx_queue.put(("error", str(exc)))
                return
            if data:
                self.rx_queue.put(("rx", data))


class SerialMonitorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.rx_queue: "queue.Queue" = queue.Queue()
        self.worker = SerialWorker(self.rx_queue)

        self.rx_count = 0
        self.tx_count = 0
        self.history = list(self.cfg.get("history", []))
        self.history_index = len(self.history)
        self.repeat_job = None
        # Display state
        self._at_line_start = True
        self._last_dir = None
        self._last_rx_time = 0.0
        self._hex_col = 0
        self._dump_partial = b""  # unfinished HEX+ASCII row
        self._last_was_cr = False

        root.title(APP_TITLE)
        root.geometry("1000x700")
        root.minsize(800, 520)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_vars()
        self._build_ui()
        self.refresh_ports()
        self._update_connection_state()
        self.root.after(20, self._poll_queue)
        self.root.after(250, self._poll_modem_lines)

    # ------------------------------------------------------------------ UI
    def _build_vars(self):
        c = self.cfg
        self.v_port = tk.StringVar(value=c["port"])
        self.v_baud = tk.StringVar(value=c["baud"])
        self.v_data = tk.StringVar(value=c["data_bits"])
        self.v_parity = tk.StringVar(value=c["parity"])
        self.v_stop = tk.StringVar(value=c["stop_bits"])
        self.v_flow = tk.StringVar(value=c["flow"])
        self.v_dtr = tk.BooleanVar(value=c["dtr"])
        self.v_rts = tk.BooleanVar(value=c["rts"])

        self.v_display = tk.StringVar(value=c["display_mode"])
        self.v_timestamps = tk.BooleanVar(value=c["timestamps"])
        self.v_show_tx = tk.BooleanVar(value=c["show_tx"])
        self.v_autoscroll = tk.BooleanVar(value=c["autoscroll"])
        self.v_show_control = tk.BooleanVar(value=c["show_control"])

        self.v_send_mode = tk.StringVar(value=c["send_mode"])
        self.v_line_ending = tk.StringVar(value=c["line_ending"])
        self.v_escapes = tk.BooleanVar(value=c["escapes"])
        self.v_repeat = tk.BooleanVar(value=False)
        self.v_repeat_ms = tk.StringVar(value=c["repeat_ms"])
        self.v_send_text = tk.StringVar()

        self.v_status = tk.StringVar(value="Disconnected")
        self.v_counts = tk.StringVar(value="RX: 0  TX: 0")
        self.v_lines = tk.StringVar(value="")

    def _build_ui(self):
        pad = {"padx": 4, "pady": 2}

        # ---- Port settings -------------------------------------------
        settings = ttk.LabelFrame(self.root, text="Serial Port Settings")
        settings.pack(fill="x", padx=6, pady=(6, 3))

        ttk.Label(settings, text="Port").grid(row=0, column=0, sticky="w", **pad)
        self.settings_widgets = []  # (widget, state when disconnected)
        self.cb_port = ttk.Combobox(settings, textvariable=self.v_port, width=22)
        self.settings_widgets.append((self.cb_port, "normal"))
        self.cb_port.grid(row=0, column=1, sticky="w", **pad)
        btn_refresh = ttk.Button(settings, text="Refresh", command=self.refresh_ports,
                                 width=8)
        btn_refresh.grid(row=0, column=2, **pad)
        self.settings_widgets.append((btn_refresh, "normal"))

        ttk.Label(settings, text="Baud").grid(row=0, column=3, sticky="w", **pad)
        self._combo(settings, self.v_baud, BAUD_RATES, 9, "normal", 0, 4)

        ttk.Label(settings, text="Data bits").grid(row=0, column=5, sticky="w", **pad)
        self._combo(settings, self.v_data, DATA_BITS, 3, "readonly", 0, 6)

        ttk.Label(settings, text="Parity").grid(row=0, column=7, sticky="w", **pad)
        self._combo(settings, self.v_parity, PARITIES, 6, "readonly", 0, 8)

        ttk.Label(settings, text="Stop bits").grid(row=1, column=5, sticky="w", **pad)
        self._combo(settings, self.v_stop, STOP_BITS, 3, "readonly", 1, 6)

        ttk.Label(settings, text="Flow").grid(row=1, column=7, sticky="w", **pad)
        self._combo(settings, self.v_flow, FLOW_CONTROLS, 9, "readonly", 1, 8)

        lines = ttk.Frame(settings)
        lines.grid(row=1, column=0, columnspan=5, sticky="w", **pad)
        ttk.Checkbutton(lines, text="DTR", variable=self.v_dtr,
                        command=lambda: self.worker.set_dtr(self.v_dtr.get())
                        ).pack(side="left")
        ttk.Checkbutton(lines, text="RTS", variable=self.v_rts,
                        command=lambda: self.worker.set_rts(self.v_rts.get())
                        ).pack(side="left", padx=(6, 12))
        ttk.Label(lines, textvariable=self.v_lines, foreground="#555").pack(side="left")

        self.btn_connect = ttk.Button(settings, text="Connect", width=12,
                                      command=self.toggle_connection)
        self.btn_connect.grid(row=0, column=9, rowspan=2, sticky="ns", padx=8, pady=4)
        settings.columnconfigure(10, weight=1)

        # ---- Monitor -------------------------------------------------
        monitor = ttk.LabelFrame(self.root, text="Monitor")
        monitor.pack(fill="both", expand=True, padx=6, pady=3)

        opts = ttk.Frame(monitor)
        opts.pack(fill="x", padx=2, pady=2)
        ttk.Label(opts, text="Display").pack(side="left", padx=(2, 2))
        cb_disp = ttk.Combobox(opts, textvariable=self.v_display, values=DISPLAY_MODES,
                               width=11, state="readonly")
        cb_disp.pack(side="left", padx=(0, 8))
        cb_disp.bind("<<ComboboxSelected>>", lambda e: self._new_line())
        ttk.Checkbutton(opts, text="Timestamps", variable=self.v_timestamps
                        ).pack(side="left", padx=4)
        ttk.Checkbutton(opts, text="Show sent", variable=self.v_show_tx
                        ).pack(side="left", padx=4)
        ttk.Checkbutton(opts, text="Show control chars",
                        variable=self.v_show_control).pack(side="left", padx=4)
        ttk.Checkbutton(opts, text="Auto-scroll", variable=self.v_autoscroll
                        ).pack(side="left", padx=4)
        ttk.Button(opts, text="Save log...", command=self.save_log
                   ).pack(side="right", padx=2)
        ttk.Button(opts, text="Clear", command=self.clear_monitor
                   ).pack(side="right", padx=2)

        text_frame = ttk.Frame(monitor)
        text_frame.pack(fill="both", expand=True, padx=2, pady=2)
        self.txt = tk.Text(text_frame, wrap="char", height=10, font=("Courier New", 10),
                           bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
                           undo=False)
        yscroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=yscroll.set)
        self.txt.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.txt.tag_configure("rx", foreground="#9cdcfe")
        self.txt.tag_configure("tx", foreground="#ce9178")
        self.txt.tag_configure("ts", foreground="#6a9955")
        self.txt.tag_configure("info", foreground="#c586c0")
        self.txt.tag_configure("error", foreground="#f44747")
        # Read-only but still selectable/copyable.
        self.txt.bind("<Key>", self._readonly_key)

        # ---- Send ----------------------------------------------------
        send = ttk.LabelFrame(self.root, text="Send")
        send.pack(fill="x", padx=6, pady=3)

        row1 = ttk.Frame(send)
        row1.pack(fill="x", padx=2, pady=2)
        ttk.Radiobutton(row1, text="ASCII", value="ASCII", variable=self.v_send_mode
                        ).pack(side="left")
        ttk.Radiobutton(row1, text="HEX", value="HEX", variable=self.v_send_mode
                        ).pack(side="left", padx=(4, 8))
        self.ent_send = ttk.Entry(row1, textvariable=self.v_send_text,
                                  font=("Courier New", 10))
        self.ent_send.pack(side="left", fill="x", expand=True, padx=4)
        self.ent_send.bind("<Return>", lambda e: self.send_current())
        self.ent_send.bind("<Up>", self._history_up)
        self.ent_send.bind("<Down>", self._history_down)
        self.btn_send = ttk.Button(row1, text="Send", command=self.send_current, width=10)
        self.btn_send.pack(side="left", padx=2)

        row2 = ttk.Frame(send)
        row2.pack(fill="x", padx=2, pady=2)
        ttk.Label(row2, text="Line ending").pack(side="left")
        ttk.Combobox(row2, textvariable=self.v_line_ending,
                     values=list(codec.LINE_ENDINGS), width=14, state="readonly"
                     ).pack(side="left", padx=(4, 10))
        ttk.Checkbutton(row2, text="Interpret escapes (\\r \\n \\t \\xHH) in ASCII",
                        variable=self.v_escapes).pack(side="left", padx=4)
        ttk.Checkbutton(row2, text="Repeat every", variable=self.v_repeat,
                        command=self._toggle_repeat).pack(side="left", padx=(12, 2))
        ttk.Spinbox(row2, from_=10, to=3600000, increment=100, width=8,
                    textvariable=self.v_repeat_ms).pack(side="left")
        ttk.Label(row2, text="ms").pack(side="left", padx=2)

        # ---- Macros --------------------------------------------------
        macros = ttk.LabelFrame(self.root, text="Quick commands (right-click to edit)")
        macros.pack(fill="x", padx=6, pady=3)
        self.macro_buttons = []
        for i in range(NUM_MACROS):
            b = ttk.Button(macros, command=lambda i=i: self.send_macro(i))
            b.grid(row=0, column=i, sticky="ew", padx=2, pady=3)
            b.bind("<Button-3>", lambda e, i=i: self.edit_macro(i))
            b.bind("<Button-2>", lambda e, i=i: self.edit_macro(i))  # macOS
            macros.columnconfigure(i, weight=1)
            self.macro_buttons.append(b)
        self._refresh_macro_buttons()

        # ---- Status bar ---------------------------------------------
        status = ttk.Frame(self.root)
        status.pack(fill="x", side="bottom", padx=6, pady=(0, 4))
        ttk.Label(status, textvariable=self.v_status).pack(side="left")
        ttk.Button(status, text="Reset counters", command=self.reset_counters
                   ).pack(side="right")
        ttk.Label(status, textvariable=self.v_counts).pack(side="right", padx=8)


    def _combo(self, parent, var, values, width, idle_state, row, col):
        cb = ttk.Combobox(parent, textvariable=var, values=values, width=width,
                          state=idle_state)
        cb.grid(row=row, column=col, sticky="w", padx=4, pady=2)
        self.settings_widgets.append((cb, idle_state))
        return cb

    # ------------------------------------------------------------ ports
    def refresh_ports(self):
        if serial is None:
            self.cb_port["values"] = []
            return
        ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
        devices = [p.device for p in ports]
        self.cb_port["values"] = devices
        self._port_descriptions = {p.device: p.description for p in ports}
        if not self.v_port.get() and devices:
            self.v_port.set(devices[0])
        self._set_status_idle(len(devices))

    def _set_status_idle(self, n_ports=None):
        if self.worker.is_open:
            return
        if n_ports is None:
            n_ports = len(self.cb_port["values"])
        self.v_status.set(f"Disconnected - {n_ports} port(s) found")

    def toggle_connection(self):
        if self.worker.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if serial is None:
            messagebox.showerror(APP_TITLE, "pyserial is not installed.\n\n"
                                 "Install it with:  pip install pyserial")
            return
        port = self.v_port.get().strip()
        if not port:
            messagebox.showwarning(APP_TITLE, "Please select a serial port.")
            return
        try:
            baud = int(self.v_baud.get())
            if baud <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_TITLE, f"Invalid baud rate: {self.v_baud.get()!r}")
            return
        try:
            self.worker.open(port, baud, self.v_data.get(), self.v_parity.get(),
                             self.v_stop.get(), self.v_flow.get(),
                             self.v_dtr.get(), self.v_rts.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not open {port}:\n{exc}")
            return
        desc = f"{port} @ {baud} {self.v_data.get()}{self.v_parity.get()[0]}" \
               f"{self.v_stop.get()}  flow: {self.v_flow.get()}"
        self._info(f"Connected to {desc}")
        self.v_status.set(f"Connected: {desc}")
        self._update_connection_state()

    def disconnect(self, reason: str | None = None):
        self._stop_repeat()
        self.worker.close()
        self._info(f"Disconnected{': ' + reason if reason else ''}")
        self._update_connection_state()
        self._set_status_idle()

    def _update_connection_state(self):
        connected = self.worker.is_open
        self.btn_connect.configure(text="Disconnect" if connected else "Connect")
        for w, idle_state in self.settings_widgets:
            w.configure(state="disabled" if connected else idle_state)
        if not connected:
            self.v_lines.set("")

    def _poll_modem_lines(self):
        st = self.worker.modem_status()
        if st is not None:
            cts, dsr, ri, cd = st
            fmt = lambda name, v: f"{name}:{'●' if v else '○'}"
            self.v_lines.set("  ".join([fmt("CTS", cts), fmt("DSR", dsr),
                                        fmt("RI", ri), fmt("CD", cd)]))
        self.root.after(250, self._poll_modem_lines)

    # -------------------------------------------------------- receiving
    def _poll_queue(self):
        rx = bytearray()
        try:
            for _ in range(500):
                kind, payload = self.rx_queue.get_nowait()
                if kind == "rx":
                    rx += payload
                elif kind == "error":
                    self._flush_rx(rx)
                    rx = bytearray()
                    self._error(f"Port error: {payload}")
                    self.disconnect("port error")
        except queue.Empty:
            pass
        self._flush_rx(rx)
        self._update_counts()
        self.root.after(20, self._poll_queue)

    def _flush_rx(self, rx: bytearray):
        if rx:
            self.rx_count += len(rx)
            self._display(bytes(rx), "rx")

    def _display(self, data: bytes, direction: str):
        mode = self.v_display.get()
        now = time.monotonic()
        # Start a fresh line whenever the direction changes, or (for HEX
        # views) when there was a pause in the incoming stream.
        if direction != self._last_dir or \
                (mode != "ASCII" and now - self._last_rx_time > 0.1):
            self._new_line()
        self._last_dir = direction
        self._last_rx_time = now

        if mode == "ASCII":
            show_ctl = self.v_show_control.get()
            if not show_ctl and self._last_was_cr and data.startswith(b"\n"):
                data = data[1:]  # second half of a CR LF split across reads
            self._last_was_cr = data.endswith(b"\r")
            text = codec.to_ascii(data, show_control=show_ctl)
            if not show_ctl:
                text = text.replace("\r\n", "\n").replace("\r", "\n")
            for piece in text.splitlines(keepends=True):
                self._write_with_prefix(piece, direction)
                if piece.endswith("\n"):
                    self._at_line_start = True
        elif mode == "HEX":
            for b in data:
                if self._hex_col >= 16:
                    self._insert("\n", direction)
                    self._at_line_start = True
                    self._hex_col = 0
                self._write_with_prefix(f"{b:02X} ", direction)
                self._hex_col += 1
        else:  # HEX + ASCII dump, 16 bytes per row
            if self._dump_partial:
                # Continue the unfinished row: remove it and redraw it.
                self.txt.delete("dump_row", "end-1c")
                data = self._dump_partial + data
                self._at_line_start = True
            self._dump_partial = b""
            for off in range(0, len(data), 16):
                row = data[off:off + 16]
                self.txt.mark_set("dump_row", "end-1c")
                self.txt.mark_gravity("dump_row", "left")
                self._write_with_prefix(codec.hex_dump(row) + "\n", direction)
                self._at_line_start = True
                if len(row) < 16:
                    self._dump_partial = row
        self._trim()
        if self.v_autoscroll.get():
            self.txt.see("end")

    def _write_with_prefix(self, text: str, direction: str):
        if self._at_line_start:
            if self.v_timestamps.get():
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                self._insert(f"[{ts}] ", "ts")
            if self.v_show_tx.get():
                self._insert("<< " if direction == "rx" else ">> ", direction)
            self._at_line_start = False
        self._insert(text, direction)

    def _new_line(self):
        if not self._at_line_start:
            self._insert("\n", "rx")
        self._at_line_start = True
        self._hex_col = 0
        self._dump_partial = b""
        self._last_was_cr = False

    def _insert(self, text: str, tag: str):
        self.txt.insert("end", text, tag)

    def _trim(self, max_lines: int = 20000):
        lines = int(self.txt.index("end-1c").split(".")[0])
        if lines > max_lines:
            self.txt.delete("1.0", f"{lines - max_lines}.0")

    def _info(self, msg: str, tag: str = "info"):
        self._new_line()
        ts = datetime.now().strftime("%H:%M:%S")
        self._insert(f"--- [{ts}] {msg} ---\n", tag)
        self._last_dir = None
        if self.v_autoscroll.get():
            self.txt.see("end")

    def _error(self, msg: str):
        self._info(msg, "error")

    def _readonly_key(self, event):
        # Allow copy / select-all / navigation, block editing.
        if event.state & 0x4 and event.keysym.lower() in ("c", "a"):
            if event.keysym.lower() == "a":
                self.txt.tag_add("sel", "1.0", "end")
                return "break"
            return None
        if event.keysym in ("Left", "Right", "Up", "Down", "Prior", "Next",
                            "Home", "End"):
            return None
        return "break"

    # ---------------------------------------------------------- sending
    def _payload(self, text: str, mode: str) -> bytes | None:
        try:
            return codec.build_payload(text, mode, self.v_line_ending.get(),
                                       escapes=self.v_escapes.get())
        except codec.ParseError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return None

    def send_bytes(self, data: bytes) -> bool:
        if not self.worker.is_open:
            messagebox.showwarning(APP_TITLE, "Not connected.")
            return False
        if not data:
            return True
        try:
            n = self.worker.write(data)
        except Exception as exc:
            self._error(f"Write failed: {exc}")
            return False
        self.tx_count += n
        if self.v_show_tx.get():
            self._display(data, "tx")
        self._update_counts()
        return True

    def send_current(self):
        text = self.v_send_text.get()
        data = self._payload(text, self.v_send_mode.get())
        if data is None:
            self._stop_repeat()
            return
        if self.send_bytes(data):
            self._add_history(text)
        else:
            self._stop_repeat()

    def send_macro(self, idx: int):
        m = self.cfg["macros"][idx]
        if not m.get("data"):
            self.edit_macro(idx)
            return
        data = self._payload(m["data"], m.get("mode", "ASCII"))
        if data is not None:
            self.send_bytes(data)

    def edit_macro(self, idx: int):
        MacroDialog(self.root, self.cfg["macros"][idx], on_save=lambda m:
                    self._save_macro(idx, m))

    def _save_macro(self, idx: int, macro: dict):
        self.cfg["macros"][idx] = macro
        self._refresh_macro_buttons()
        self._persist()

    def _refresh_macro_buttons(self):
        for b, m in zip(self.macro_buttons, self.cfg["macros"]):
            label = m.get("name") or "(empty)"
            if not m.get("data"):
                label += " (empty)"
            b.configure(text=label)

    def _toggle_repeat(self):
        if self.v_repeat.get():
            if not self.worker.is_open:
                messagebox.showwarning(APP_TITLE, "Not connected.")
                self.v_repeat.set(False)
                return
            self._repeat_tick()
        else:
            self._stop_repeat()

    def _repeat_tick(self):
        if not self.v_repeat.get() or not self.worker.is_open:
            self._stop_repeat()
            return
        self.send_current()
        try:
            ms = max(10, int(self.v_repeat_ms.get()))
        except ValueError:
            ms = 1000
        if self.v_repeat.get():
            self.repeat_job = self.root.after(ms, self._repeat_tick)

    def _stop_repeat(self):
        self.v_repeat.set(False)
        if self.repeat_job is not None:
            self.root.after_cancel(self.repeat_job)
            self.repeat_job = None

    # ---------------------------------------------------------- history
    def _add_history(self, text: str):
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
            self.history = self.history[-100:]
        self.history_index = len(self.history)

    def _history_up(self, _event):
        if self.history and self.history_index > 0:
            self.history_index -= 1
            self.v_send_text.set(self.history[self.history_index])
            self.ent_send.icursor("end")
        return "break"

    def _history_down(self, _event):
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self.v_send_text.set(self.history[self.history_index])
        else:
            self.history_index = len(self.history)
            self.v_send_text.set("")
        self.ent_send.icursor("end")
        return "break"

    # ------------------------------------------------------------ misc
    def clear_monitor(self):
        self.txt.delete("1.0", "end")
        self._at_line_start = True
        self._hex_col = 0
        self._dump_partial = b""
        self._last_was_cr = False
        self._last_dir = None

    def save_log(self):
        path = filedialog.asksaveasfilename(
            title="Save log", defaultextension=".txt",
            initialfile=datetime.now().strftime("serial_log_%Y%m%d_%H%M%S.txt"),
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.txt.get("1.0", "end-1c"))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not save log:\n{exc}")

    def reset_counters(self):
        self.rx_count = self.tx_count = 0
        self._update_counts()

    def _update_counts(self):
        self.v_counts.set(f"RX: {self.rx_count}  TX: {self.tx_count}")

    def _persist(self):
        self.cfg.update({
            "port": self.v_port.get(),
            "baud": self.v_baud.get(),
            "data_bits": self.v_data.get(),
            "parity": self.v_parity.get(),
            "stop_bits": self.v_stop.get(),
            "flow": self.v_flow.get(),
            "dtr": self.v_dtr.get(),
            "rts": self.v_rts.get(),
            "display_mode": self.v_display.get(),
            "timestamps": self.v_timestamps.get(),
            "show_tx": self.v_show_tx.get(),
            "autoscroll": self.v_autoscroll.get(),
            "show_control": self.v_show_control.get(),
            "send_mode": self.v_send_mode.get(),
            "line_ending": self.v_line_ending.get(),
            "escapes": self.v_escapes.get(),
            "repeat_ms": self.v_repeat_ms.get(),
            "history": self.history[-100:],
        })
        save_config(self.cfg)

    def on_close(self):
        self._stop_repeat()
        self.worker.close()
        self._persist()
        self.root.destroy()


class MacroDialog(tk.Toplevel):
    """Small dialog to edit a quick-command macro."""

    def __init__(self, parent, macro: dict, on_save):
        super().__init__(parent)
        self.title("Edit quick command")
        self.transient(parent)
        self.resizable(False, False)
        self.on_save = on_save
        self.v_name = tk.StringVar(value=macro.get("name", ""))
        self.v_data = tk.StringVar(value=macro.get("data", ""))
        self.v_mode = tk.StringVar(value=macro.get("mode", "ASCII"))

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Name").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.v_name, width=20).grid(
            row=0, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="Mode").grid(row=1, column=0, sticky="w", pady=3)
        mf = ttk.Frame(frm)
        mf.grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(mf, text="ASCII", value="ASCII", variable=self.v_mode).pack(side="left")
        ttk.Radiobutton(mf, text="HEX", value="HEX", variable=self.v_mode).pack(side="left", padx=6)
        ttk.Label(frm, text="Data").grid(row=2, column=0, sticky="w", pady=3)
        ent = ttk.Entry(frm, textvariable=self.v_data, width=44, font=("Courier New", 10))
        ent.grid(row=2, column=1, sticky="we", pady=3)
        ttk.Label(frm, text="The Send panel's line ending is appended when sent.",
                  foreground="#777").grid(row=3, column=0, columnspan=2, sticky="w")
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="Clear", command=self._clear).pack(side="left", padx=3)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=3)
        ttk.Button(btns, text="Save", command=self._save).pack(side="left", padx=3)
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        ent.focus_set()
        self.grab_set()

    def _clear(self):
        self.v_data.set("")

    def _save(self):
        data = self.v_data.get()
        mode = self.v_mode.get()
        if mode == "HEX" and data:
            try:
                codec.parse_hex(data)
            except codec.ParseError as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=self)
                return
        self.on_save({"name": self.v_name.get().strip() or "Macro",
                      "data": data, "mode": mode})
        self.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("clam" if root.tk.call("tk", "windowingsystem")
                                  == "x11" else ttk.Style(root).theme_use())
    except tk.TclError:
        pass
    SerialMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
