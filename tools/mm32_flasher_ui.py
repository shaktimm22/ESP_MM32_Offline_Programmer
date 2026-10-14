"""MM32 Offline Programmer - desktop UI.

Talks to the ESP8266 programmer over UART. Loads an Intel HEX / BIN file,
uploads it to the programmer's flash, configures the programmer, and shows
live pass/fail counters.

Requires: Python 3.8+, pyserial  (pip install pyserial)
"""

import csv
import datetime as dt
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import zlib
from tkinter import filedialog, messagebox, ttk

import serial
import serial.tools.list_ports

APP_TITLE = "MM32 Offline Programmer"
DEFAULT_BAUD = 460800
CHUNK = 256
FLASH_BASE = 0x08000000
LOG_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flash_log.csv")


# --------------------------------------------------------------------------- image loading

def parse_intel_hex(text):
    """Return (base_address, bytearray) for an Intel HEX file; gaps are filled with 0xFF."""
    mem = {}
    upper = 0
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"line {ln}: missing ':'")
        try:
            rec = bytes.fromhex(line[1:])
        except ValueError:
            raise ValueError(f"line {ln}: invalid hex characters")
        if len(rec) < 5 or len(rec) != rec[0] + 5:
            raise ValueError(f"line {ln}: bad record length")
        if sum(rec) & 0xFF:
            raise ValueError(f"line {ln}: checksum error")
        count, addr, rtype, data = rec[0], (rec[1] << 8) | rec[2], rec[3], rec[4:4 + rec[0]]
        if rtype == 0x00:
            base = upper + addr
            for i, b in enumerate(data):
                mem[base + i] = b
        elif rtype == 0x01:
            break
        elif rtype == 0x02:
            upper = ((data[0] << 8) | data[1]) << 4
        elif rtype == 0x04:
            upper = ((data[0] << 8) | data[1]) << 16
        # 0x03 / 0x05 (start address) are irrelevant for programming
    if not mem:
        raise ValueError("no data records in file")
    lo, hi = min(mem), max(mem) + 1
    buf = bytearray(b"\xff" * (hi - lo))
    for a, b in mem.items():
        buf[a - lo] = b
    return lo, buf


def load_image(path):
    """Load .hex or .bin. Returns (base, bytes) aligned/padded for halfword+word access."""
    if path.lower().endswith((".hex", ".ihx")):
        with open(path, "r", encoding="ascii", errors="replace") as f:
            base, data = parse_intel_hex(f.read())
    else:
        with open(path, "rb") as f:
            base, data = FLASH_BASE, bytearray(f.read())
    if base & 1:
        base -= 1
        data[0:0] = b"\xff"
    while len(data) % 4:
        data.append(0xFF)
    if not (FLASH_BASE <= base and base + len(data) <= FLASH_BASE + 0x100000):
        raise ValueError(f"image at 0x{base:08X} is outside MM32 flash (0x08000000). "
                         "Check the linker settings / IROM1 start in Keil.")
    return base, bytes(data)


# --------------------------------------------------------------------------- serial link

class LinkError(Exception):
    pass


class SerialLink:
    """Line-based link. '@OK'/'@ERR' lines answer requests, everything else is an event."""

    def __init__(self, on_line):
        self.ser = None
        self.on_line = on_line
        self.resp_q = queue.Queue()
        self.lock = threading.Lock()
        self.alive = False

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    def open(self, port, baud):
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout = port, baud, 0.05
        # NodeMCU wires DTR/RTS to GPIO0/EN; keep them released so the ESP is not reset.
        ser.dtr = False
        ser.rts = False
        ser.open()
        self.ser = ser
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def close(self):
        self.alive = False
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def _reader(self):
        buf = b""
        while self.alive:
            try:
                chunk = self.ser.read(512)
            except Exception as e:
                self.on_line(f"@EVT LINKLOST {e}")
                self.alive = False
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("@OK") or line.startswith("@ERR"):
                    self.resp_q.put(line)
                else:
                    self.on_line(line)

    def request(self, cmd, timeout=3.0):
        """Send a command, return the reply payload (text after '@OK <CMD>'), raise LinkError on @ERR."""
        if not self.is_open:
            raise LinkError("not connected")
        name = cmd.split(" ", 1)[0].upper()
        with self.lock:
            while not self.resp_q.empty():
                self.resp_q.get_nowait()
            self.ser.write((cmd + "\n").encode("ascii"))
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise LinkError(f"{name}: no reply from programmer")
                try:
                    line = self.resp_q.get(timeout=left)
                except queue.Empty:
                    continue
                parts = line.split(" ", 2)
                if len(parts) < 2 or parts[1] != name:
                    continue
                payload = parts[2] if len(parts) > 2 else ""
                if parts[0] == "@ERR":
                    raise LinkError(f"{name}: {payload}")
                return payload


def is_bluetooth_port(port):
    """Windows creates virtual COM ports for paired Bluetooth devices; they are never the programmer."""
    text = f"{port.hwid} {port.description}".upper()
    return "BTHENUM" in text or "BLUETOOTH" in text


def parse_kv(text):
    out = {}
    for tok in text.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def parse_chips(text):
    chips = []
    for item in filter(None, text.split(",")):
        dev, name, kb, page, kind = item.split(":")
        chips.append({"id": int(dev, 16), "name": name, "kb": int(kb), "page": int(page),
                      "builtin": kind == "b"})
    return chips


# --------------------------------------------------------------------------- UI

PREFS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_prefs.json")

C = {
    "bg": "#eef1f6", "card": "#ffffff", "border": "#dde3eb", "text": "#1e293b", "muted": "#64748b",
    "accent": "#2563eb", "accent_hover": "#1d4ed8", "accent_glow": "#60a5fa", "accent_soft": "#dbeafe",
    "ok": "#16a34a", "ok_soft": "#dcfce7", "err": "#dc2626", "err_soft": "#fee2e2",
    "warn": "#b45309", "warn_soft": "#fef3c7", "idle_soft": "#f1f5f9",
    "header": "#0f172a", "header_text": "#e2e8f0", "header_muted": "#94a3b8", "disabled": "#cbd5e1",
}
FONT = "Segoe UI"
MONO = "Consolas"


def load_prefs():
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prefs(prefs):
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except OSError:
        pass


class App(tk.Tk):
    STATE_TEXT = {
        "connect": "Connecting to target", "erase": "Erasing", "program": "Programming",
        "verify": "Verifying", "protect": "Setting read protection",
    }

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        screen_h = self.winfo_screenheight()
        self.geometry(f"1200x{min(920, screen_h - 80)}")
        self.minsize(1060, 760)
        if screen_h < 1000:
            self.state("zoomed")  # small laptop screens: use the whole screen
        self.configure(bg=C["bg"])
        self.ui_q = queue.Queue()
        self.link = SerialLink(self.ui_q.put)
        self.prefs = load_prefs()
        self.image = None          # (path, base, data, crc)
        self.chips = []
        self.dev_info = {}
        self.last_detect = None
        self.busy = False          # a PC-side request is running
        self.device_busy = False   # the programmer is flashing
        self.suppress_set = False  # don't echo settings back while loading them
        self.conn_widgets = []     # enabled only while connected
        self.pulse_on = False

        self._styles()
        self._build()
        self.refresh_ports()
        self._set_connected(False)
        last = self.prefs.get("last_file")
        if last and os.path.exists(last):
            self._load_file(last, quiet=True)
        self.bind("<F5>", lambda e: self.flash_now())
        self.bind("<Control-o>", lambda e: self.browse())
        self.after(50, self._drain)
        self.after(700, self._pulse)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ styling
    def _styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        base = (FONT, 10)
        s.configure(".", font=base, background=C["bg"], foreground=C["text"], bordercolor=C["border"],
                    lightcolor=C["border"], darkcolor=C["border"], troughcolor=C["idle_soft"],
                    focuscolor=C["accent_soft"])
        s.configure("TFrame", background=C["bg"])
        s.configure("Card.TFrame", background=C["card"])
        s.configure("Header.TFrame", background=C["header"])
        s.configure("TLabel", background=C["bg"])
        s.configure("Card.TLabel", background=C["card"])
        s.configure("Muted.TLabel", background=C["card"], foreground=C["muted"], font=(FONT, 9))
        s.configure("CardTitle.TLabel", background=C["card"], font=(FONT, 11, "bold"))
        s.configure("Step.TLabel", background=C["accent_soft"], foreground=C["accent"], font=(FONT, 9, "bold"),
                    padding=(7, 1))
        s.configure("FileName.TLabel", background=C["card"], font=(FONT, 15, "bold"))
        s.configure("Header.TLabel", background=C["header"], foreground=C["header_text"])
        s.configure("HeaderTitle.TLabel", background=C["header"], foreground="#ffffff", font=(FONT, 15, "bold"))
        s.configure("HeaderSub.TLabel", background=C["header"], foreground=C["header_muted"], font=(FONT, 9))

        s.configure("TButton", padding=(12, 6), background="#f8fafc", relief="flat")
        s.map("TButton", background=[("disabled", "#f1f5f9"), ("pressed", "#e2e8f0"), ("active", "#eef2f7")],
              foreground=[("disabled", C["disabled"])])
        for name, bg, hover in (("Accent", C["accent"], C["accent_hover"]),
                                ("Pulse", C["accent_glow"], C["accent"]),
                                ("Ok", C["ok"], "#15803d"),
                                ("Danger", C["err"], "#b91c1c")):
            s.configure(f"{name}.TButton", background=bg, foreground="#ffffff", bordercolor=bg,
                        lightcolor=bg, darkcolor=bg, font=(FONT, 10, "bold"), padding=(16, 7))
            s.map(f"{name}.TButton", background=[("disabled", C["disabled"]), ("pressed", hover), ("active", hover)],
                  foreground=[("disabled", "#f8fafc")])
        s.configure("Flash.TButton", background=C["accent"], foreground="#ffffff", bordercolor=C["accent"],
                    lightcolor=C["accent"], darkcolor=C["accent"], font=(FONT, 16, "bold"), padding=(20, 14))
        s.map("Flash.TButton", background=[("disabled", C["disabled"]), ("pressed", C["accent_hover"]),
                                           ("active", C["accent_hover"])],
              foreground=[("disabled", "#f8fafc")])
        s.configure("Link.TButton", background=C["card"], foreground=C["accent"], padding=(4, 2),
                    bordercolor=C["card"], lightcolor=C["card"], darkcolor=C["card"], font=(FONT, 9, "underline"))
        s.map("Link.TButton", background=[("active", C["card"])], foreground=[("disabled", C["disabled"])])

        s.configure("Header.TButton", background="#1e293b", foreground="#ffffff", bordercolor="#334155",
                    lightcolor="#1e293b", darkcolor="#1e293b", font=(FONT, 12, "bold"), padding=(6, 1))
        s.map("Header.TButton", background=[("disabled", "#1e293b"), ("active", "#334155")],
              foreground=[("disabled", "#475569")])
        s.configure("Card.TCheckbutton", background=C["card"])
        s.map("Card.TCheckbutton", background=[("active", C["card"])])
        s.configure("Card.TRadiobutton", background=C["card"])
        s.map("Card.TRadiobutton", background=[("active", C["card"])])
        s.configure("Seg.Toolbutton", background="#f1f5f9", foreground=C["text"], padding=(14, 5),
                    font=(FONT, 9, "bold"), anchor="center")
        s.map("Seg.Toolbutton", background=[("disabled", "#f8fafc"), ("selected", C["accent"]), ("active", "#e2e8f0")],
              foreground=[("disabled", C["disabled"]), ("selected", "#ffffff")])
        s.configure("TCombobox", padding=4, arrowsize=14)
        s.configure("Header.TCombobox", fieldbackground="#1e293b", background="#1e293b",
                    foreground="#ffffff", arrowcolor="#ffffff", bordercolor="#334155")
        s.map("Header.TCombobox", fieldbackground=[("readonly", "#1e293b"), ("disabled", "#1e293b")],
              foreground=[("readonly", "#ffffff"), ("disabled", C["header_muted"])],
              selectbackground=[("readonly", "#1e293b")], selectforeground=[("readonly", "#ffffff")])
        self.option_add("*TCombobox*Listbox.font", base)
        s.configure("TSpinbox", padding=3, arrowsize=12)
        s.configure("Horizontal.TProgressbar", thickness=10, background=C["accent"], bordercolor=C["idle_soft"],
                    lightcolor=C["accent"], darkcolor=C["accent"])
        s.configure("Ok.Horizontal.TProgressbar", background=C["ok"], lightcolor=C["ok"], darkcolor=C["ok"])
        s.configure("Err.Horizontal.TProgressbar", background=C["err"], lightcolor=C["err"], darkcolor=C["err"])

    def _card(self, parent, title, step=None, action=None):
        """White card with a title row. Returns the body frame."""
        outer = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
        head = ttk.Frame(outer, style="Card.TFrame", padding=(14, 10, 14, 0))
        head.pack(fill="x")
        if step:
            ttk.Label(head, text=step, style="Step.TLabel").pack(side="left", padx=(0, 8))
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left")
        if action:
            txt, fn = action
            b = ttk.Button(head, text=txt, style="Link.TButton", command=fn)
            b.pack(side="right")
            self.conn_widgets.append(b)
        body = ttk.Frame(outer, style="Card.TFrame", padding=(14, 8, 14, 12))
        body.pack(fill="both", expand=True)
        outer.body = body
        return outer

    def _track(self, *widgets):
        self.conn_widgets.extend(widgets)
        return widgets[0]

    # ================================================================ layout
    def _build(self):
        # ---- header
        header = ttk.Frame(self, style="Header.TFrame", padding=(18, 12))
        header.pack(fill="x")
        title = ttk.Frame(header, style="Header.TFrame")
        title.pack(side="left")
        ttk.Label(title, text="MM32 Offline Programmer", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(title, text="SWD production flasher  ·  ESP8266", style="HeaderSub.TLabel").pack(anchor="w")

        conn = ttk.Frame(header, style="Header.TFrame")
        conn.pack(side="right")
        self.status_pill = tk.Label(conn, text="●  Disconnected", bg="#3f1d24", fg="#fca5a5",
                                    font=(FONT, 9, "bold"), padx=10, pady=4)
        self.status_pill.pack(side="right", padx=(12, 0))
        self.conn_btn = ttk.Button(conn, text="Connect", style="Accent.TButton", command=self.toggle_connect,
                                   width=12)
        self.conn_btn.pack(side="right", padx=(10, 0))
        self.baud_var = tk.StringVar(value=str(self.prefs.get("baud", DEFAULT_BAUD)))
        self.baud_cb = ttk.Combobox(conn, textvariable=self.baud_var, width=8, style="Header.TCombobox",
                                    values=["115200", "230400", "460800", "921600"], state="readonly")
        self.baud_cb.pack(side="right")
        ttk.Label(conn, text="Baud", style="Header.TLabel").pack(side="right", padx=(12, 6))
        self.refresh_btn = ttk.Button(conn, text="↻", width=3, style="Header.TButton", command=self.refresh_ports)
        self.refresh_btn.pack(side="right", padx=(6, 0))
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(conn, textvariable=self.port_var, width=30, state="readonly",
                                    style="Header.TCombobox")
        self.port_cb.pack(side="right")
        ttk.Label(conn, text="Port", style="Header.TLabel").pack(side="right", padx=(0, 6))
        self.bind("<Return>", lambda e: None if self.link.is_open else self.toggle_connect())

        # ---- hint banner (visible while disconnected)
        self.hint = tk.Frame(self, bg=C["accent_soft"])
        tk.Label(self.hint, text="①", bg=C["accent_soft"], fg=C["accent"], font=(FONT, 14, "bold")).pack(
            side="left", padx=(18, 8), pady=6)
        self.hint_lbl = tk.Label(self.hint, bg=C["accent_soft"], fg=C["text"], font=(FONT, 10),
                                 text="Plug in the programmer, choose its COM port (e.g. USB-SERIAL CH340) "
                                      "and press Connect.")
        self.hint_lbl.pack(side="left")
        self.hint.pack(fill="x")

        # ---- body
        body = ttk.Frame(self, padding=(16, 14, 16, 14))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=5, uniform="col")
        body.columnconfigure(1, weight=6, uniform="col")
        body.rowconfigure(1, weight=1, minsize=190)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self._build_device_card(left)
        self._build_file_card(left)
        self._build_settings_card(left)
        self._build_status_card(right)
        self._build_counter_card(right)
        self._build_tools_card(right)
        self._build_log(body)

    def _build_device_card(self, parent):
        card = self._card(parent, "On the programmer", "2", action=("Refresh", self.refresh_device))
        card.pack(fill="x")
        b = card.body
        self.dev_name = ttk.Label(b, text="—", style="FileName.TLabel")
        self.dev_name.pack(anchor="w")
        self.dev_detail = ttk.Label(b, text="Connect to see the stored firmware.", style="Muted.TLabel")
        self.dev_detail.pack(anchor="w", pady=(2, 6))
        self.match_lbl = tk.Label(b, text="", font=(FONT, 9, "bold"), bg=C["card"], padx=8, pady=2)
        self.match_lbl.pack(anchor="w")

    def _build_file_card(self, parent):
        card = self._card(parent, "Load new firmware", "3")
        card.pack(fill="x", pady=10)
        b = card.body
        row = ttk.Frame(b, style="Card.TFrame")
        row.pack(fill="x")
        self.path_var = tk.StringVar(value="No file selected")
        ttk.Label(row, textvariable=self.path_var, style="Card.TLabel", font=(FONT, 10, "bold")).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self.browse).pack(side="right")
        self.img_lbl = ttk.Label(b, text="Intel HEX (.hex) or binary (.bin) from Keil", style="Muted.TLabel")
        self.img_lbl.pack(anchor="w", pady=(2, 8))
        row = ttk.Frame(b, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="For chip", style="Card.TLabel").pack(side="left")
        self.target_var = tk.StringVar(value=self.prefs.get("target", "ANY"))
        self.target_cb = self._track(ttk.Combobox(row, textvariable=self.target_var, width=14, state="readonly",
                                                  values=["ANY"]))
        self.target_cb.pack(side="left", padx=8)
        self.upload_btn = self._track(ttk.Button(row, text="Upload to programmer", style="Accent.TButton",
                                                 command=self.upload))
        self.upload_btn.pack(side="right")
        self.up_prog = ttk.Progressbar(b, maximum=100)
        self.up_prog.pack(fill="x", pady=(10, 0))

    def _build_settings_card(self, parent):
        card = self._card(parent, "Programmer settings")
        card.pack(fill="both", expand=True)
        b = card.body
        b.columnconfigure(1, weight=1)

        def seg(row, label, var, options):
            ttk.Label(b, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            f = ttk.Frame(b, style="Card.TFrame")
            f.grid(row=row, column=1, sticky="w", pady=4)
            for value, text in options:
                self._track(ttk.Radiobutton(f, text=text, value=value, variable=var, style="Seg.Toolbutton")).pack(
                    side="left")

        self.mode_var = tk.StringVar(value="button")
        self.erase_var = tk.StringVar(value="chip")
        seg(0, "Start flashing by", self.mode_var, [("button", "Button press"), ("auto", "Auto-detect board")])
        seg(1, "Erase", self.erase_var, [("chip", "Full chip"), ("pages", "Only image pages")])

        checks = ttk.Frame(b, style="Card.TFrame")
        checks.grid(row=2, column=1, sticky="w", pady=(6, 2))
        self.verify_var = tk.BooleanVar(value=True)
        self.run_var = tk.BooleanVar(value=True)
        self.protect_var = tk.BooleanVar(value=False)
        for text, var in (("Verify", self.verify_var), ("Run after flash", self.run_var),
                          ("Read-protect (RDP)", self.protect_var)):
            self._track(ttk.Checkbutton(checks, text=text, variable=var, style="Card.TCheckbutton")).pack(
                side="left", padx=(0, 14))

        nums = ttk.Frame(b, style="Card.TFrame")
        nums.grid(row=3, column=1, sticky="w", pady=(6, 0))
        self.swdelay_var = tk.StringVar(value="0")
        self.settle_var = tk.StringVar(value="500")
        ttk.Label(nums, text="SWD delay", style="Card.TLabel").pack(side="left")
        sp1 = self._track(ttk.Spinbox(nums, from_=0, to=250, width=5, textvariable=self.swdelay_var,
                                      command=self._apply_numbers))
        sp1.pack(side="left", padx=(6, 18))
        ttk.Label(nums, text="Auto-detect settle (ms)", style="Card.TLabel").pack(side="left")
        sp2 = self._track(ttk.Spinbox(nums, from_=0, to=10000, increment=100, width=7,
                                      textvariable=self.settle_var, command=self._apply_numbers))
        sp2.pack(side="left", padx=6)
        for sp in (sp1, sp2):
            sp.bind("<Return>", lambda e: self._apply_numbers())
            sp.bind("<FocusOut>", lambda e: self._apply_numbers())

        self.saved_lbl = ttk.Label(b, text="Changes are saved to the programmer automatically.",
                                   style="Muted.TLabel")
        self.saved_lbl.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        for key, var in (("mode", self.mode_var), ("erase", self.erase_var)):
            var.trace_add("write", lambda *a, k=key, v=var: self._apply_setting(k, v.get()))
        for key, var in (("verify", self.verify_var), ("run", self.run_var), ("protect", self.protect_var)):
            var.trace_add("write", lambda *a, k=key, v=var: self._apply_setting(k, int(v.get())))
        self.mode_var.trace_add("write", lambda *a: self._update_flash_hint())

    def _build_status_card(self, parent):
        outer = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
        outer.pack(fill="x")
        self.banner = tk.Frame(outer, bg=C["idle_soft"])
        self.banner.pack(fill="x")
        self.state_lbl = tk.Label(self.banner, text="Not connected", bg=C["idle_soft"], fg=C["muted"],
                                  font=(FONT, 24, "bold"), anchor="w")
        self.state_lbl.pack(fill="x", padx=18, pady=(14, 0))
        self.result_lbl = tk.Label(self.banner, text="", bg=C["idle_soft"], fg=C["muted"], font=(FONT, 10),
                                   anchor="w", justify="left", wraplength=560)
        self.result_lbl.pack(fill="x", padx=18, pady=(2, 12))
        inner = ttk.Frame(outer, style="Card.TFrame", padding=(18, 12, 18, 14))
        inner.pack(fill="x")
        self.prog = ttk.Progressbar(inner, maximum=100)
        self.prog.pack(fill="x")
        self.flash_btn = self._track(ttk.Button(inner, text="⚡  FLASH TARGET", style="Flash.TButton",
                                                command=self.flash_now))
        self.flash_btn.pack(fill="x", pady=(12, 4))
        self.flash_hint = ttk.Label(inner, text="", style="Muted.TLabel")
        self.flash_hint.pack(anchor="w")

    def _build_counter_card(self, parent):
        card = self._card(parent, "Production counters", action=("Reset counters", self.reset_counters))
        card.pack(fill="x", pady=10)
        b = card.body
        for c in range(4):
            b.columnconfigure(c, weight=1, uniform="cnt")

        def tile(col, title, fg, bg):
            f = tk.Frame(b, bg=bg)
            f.grid(row=0, column=col, sticky="nsew", padx=4)
            tk.Label(f, text=title, bg=bg, fg=fg, font=(FONT, 9, "bold")).pack(pady=(8, 0))
            lbl = tk.Label(f, text="0", bg=bg, fg=fg, font=(FONT, 26, "bold"))
            lbl.pack(pady=(0, 6))
            return lbl

        self.pass_lbl = tile(0, "PASS", C["ok"], C["ok_soft"])
        self.fail_lbl = tile(1, "FAIL", C["err"], C["err_soft"])
        self.total_lbl = tile(2, "TOTAL", C["text"], C["idle_soft"])
        self.yield_lbl = tile(3, "YIELD", C["accent"], C["accent_soft"])

    def _build_tools_card(self, parent):
        card = self._card(parent, "Target tools")
        card.pack(fill="both", expand=True)
        b = card.body
        items = [("Detect chip", self.detect, "TButton"), ("Reset target", self.reset_target, "TButton"),
                 ("Chip table…", self.chips_dialog, "TButton"), ("Erase chip", self.erase_chip, "TButton"),
                 ("Unlock (remove RDP)", self.unlock, "TButton")]
        for i, (txt, fn, style) in enumerate(items):
            self._track(ttk.Button(b, text=txt, command=fn, style=style)).grid(
                row=i // 3, column=i % 3, sticky="ew", padx=3, pady=3)
        for c in range(3):
            b.columnconfigure(c, weight=1)

    def _build_log(self, parent):
        card = self._card(parent, "Activity log")
        card.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        b = card.body
        cmdrow = ttk.Frame(b, style="Card.TFrame")
        cmdrow.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Label(cmdrow, text="Command", style="Muted.TLabel").pack(side="left")
        self.cmd_var = tk.StringVar()
        cmd_entry = self._track(ttk.Entry(cmdrow, textvariable=self.cmd_var, font=(MONO, 9)))
        cmd_entry.pack(side="left", fill="x", expand=True, padx=6)
        cmd_entry.bind("<Return>", lambda e: self.send_raw())
        self._track(ttk.Button(cmdrow, text="Send", command=self.send_raw)).pack(side="left")
        self.log = tk.Text(b, height=7, font=(MONO, 9), state="disabled", wrap="none", relief="flat",
                           bg="#f8fafc", fg=C["text"], padx=8, pady=6, highlightthickness=0)
        sb = ttk.Scrollbar(b, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_configure("err", foreground=C["err"])
        self.log.tag_configure("ok", foreground=C["ok"])
        self.log.tag_configure("dim", foreground=C["muted"])
        self.log.tag_configure("time", foreground="#94a3b8")

    # ================================================================ helpers
    def write_log(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", f"{dt.datetime.now():%H:%M:%S}  ", "time")
        self.log.insert("end", f"{text}\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def run_bg(self, title, fn, *args, quiet=False):
        """Run a blocking link operation off the UI thread."""
        if not self.link.is_open:
            if not quiet:
                messagebox.showwarning(APP_TITLE, "Connect to the programmer first.")
            return
        if self.busy:
            if not quiet:
                messagebox.showinfo(APP_TITLE, "Another operation is still running.")
            return
        self.busy = True

        def worker():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001 - report everything to the user
                self.ui_q.put(("error", f"{title}: {e}"))
            finally:
                self.ui_q.put(("idle", None))

        threading.Thread(target=worker, daemon=True).start()

    def call(self, fn, *args, **kwargs):
        """Schedule fn on the UI thread from a worker."""
        self.ui_q.put(("call", (fn, args, kwargs)))

    def _set_banner(self, title, msg="", kind="idle"):
        bg, fg = {"idle": (C["idle_soft"], C["text"]), "busy": (C["accent_soft"], C["accent"]),
                  "ok": (C["ok_soft"], C["ok"]), "err": (C["err_soft"], C["err"]),
                  "off": (C["idle_soft"], C["muted"])}[kind]
        for w in (self.banner, self.state_lbl, self.result_lbl):
            w.configure(bg=bg)
        self.state_lbl.configure(text=title, fg=fg)
        self.result_lbl.configure(text=msg, fg=fg if kind in ("ok", "err") else C["muted"])
        self.prog.configure(style={"ok": "Ok.Horizontal.TProgressbar",
                                   "err": "Err.Horizontal.TProgressbar"}.get(kind, "Horizontal.TProgressbar"))

    def _update_flash_hint(self):
        if not self.link.is_open:
            self.flash_hint.configure(text="Connect the programmer first.")
        elif self.dev_info.get("img") != "1":
            self.flash_hint.configure(text="No firmware on the programmer yet — upload one first (step 3).")
        elif self.mode_var.get() == "auto":
            self.flash_hint.configure(text="Auto-detect is on: place a board on the fixture and it is flashed "
                                           "automatically.  F5 flashes now.")
        else:
            self.flash_hint.configure(text="Press FLASH (F5) or the button on the programmer.")

    def _update_enabled(self):
        on = self.link.is_open and not self.device_busy
        for w in self.conn_widgets:
            try:
                if isinstance(w, ttk.Combobox):
                    w.state(["!disabled", "readonly"] if on else ["disabled"])
                else:
                    w.state(["!disabled"] if on else ["disabled"])
            except tk.TclError:
                pass
        self._update_upload_button()
        self._update_flash_hint()

    # ================================================================ connection
    def refresh_ports(self):
        ports = sorted((p for p in serial.tools.list_ports.comports() if not is_bluetooth_port(p)),
                       key=lambda p: p.device)
        values = [f"{p.device} - {p.description}" for p in ports]
        self.port_cb["values"] = values
        want = self.prefs.get("port")
        current = self.port_var.get()
        if current not in values:
            match = next((v for v in values if want and v.startswith(want + " ")), None)
            self.port_var.set(match or (values[0] if values else ""))
        if not values:
            self.port_var.set("")
            self.hint_lbl.configure(text="No serial port found. Plug in the programmer's USB cable, then press ⟳.")

    def toggle_connect(self):
        if self.link.is_open:
            self.link.close()
            self._set_connected(False)
            return
        port = self.port_var.get().split(" - ")[0]
        if not port:
            messagebox.showwarning(APP_TITLE, "Select the programmer's COM port first.")
            return
        try:
            self.link.open(port, int(self.baud_var.get()))
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Cannot open {port}:\n{e}\n\nIs another program (serial monitor, "
                                            f"PlatformIO upload) using the port?")
            return
        self.prefs.update(port=port, baud=int(self.baud_var.get()))
        save_prefs(self.prefs)
        self.conn_btn.configure(text="Connecting…", style="Accent.TButton")
        self.conn_btn.state(["disabled"])
        self.run_bg("Connect", self._handshake)

    def _handshake(self):
        time.sleep(0.3)
        ver = None
        for _ in range(3):
            try:
                ver = self.link.request("PING", timeout=1.5)
                break
            except LinkError:
                pass
        if ver is None:
            self.call(self._connect_failed)
            raise LinkError("the programmer did not answer. Check the COM port and baud rate "
                            "(the firmware uses 460800).")
        info = parse_kv(self.link.request("INFO"))
        chips = parse_chips(self.link.request("CHIPS"))
        self.ui_q.put(("log", (f"Connected to {ver}", "ok")))
        self.call(self._set_connected, True)
        self.call(self._apply_device_info, info, chips)

    def _connect_failed(self):
        self.link.close()
        self._set_connected(False)

    def refresh_device(self):
        def work():
            info = parse_kv(self.link.request("INFO"))
            chips = parse_chips(self.link.request("CHIPS"))
            self.call(self._apply_device_info, info, chips)
        self.run_bg("Refresh", work, quiet=True)

    def _set_connected(self, on):
        self.conn_btn.state(["!disabled"])
        self.port_cb.state(["disabled"] if on else ["!disabled", "readonly"])
        self.baud_cb.state(["disabled"] if on else ["!disabled", "readonly"])
        self.refresh_btn.state(["disabled"] if on else ["!disabled"])
        if on:
            self.conn_btn.configure(text="Disconnect", style="TButton")
            self.status_pill.configure(text="●  Connected", bg="#14352a", fg="#86efac")
            self.hint.pack_forget()
            self._set_banner("Ready", "Programmer connected.", "idle")
        else:
            self.device_busy = False
            self.conn_btn.configure(text="Connect", style="Accent.TButton")
            self.status_pill.configure(text="●  Disconnected", bg="#3f1d24", fg="#fca5a5")
            if not self.hint.winfo_ismapped():
                self.hint.pack(fill="x", after=self.winfo_children()[0])
            self._set_banner("Not connected", "Connect the programmer to begin.", "off")
            self.dev_info = {}
            self.dev_name.configure(text="—")
            self.dev_detail.configure(text="Connect to see the stored firmware.")
            self.match_lbl.configure(text="", bg=C["card"])
        self._update_enabled()

    def _pulse(self):
        """Gently pulse the Connect button while disconnected so it is the obvious first step."""
        if not self.link.is_open and "disabled" not in self.conn_btn.state():
            self.pulse_on = not self.pulse_on
            self.conn_btn.configure(style="Pulse.TButton" if self.pulse_on else "Accent.TButton")
        self.after(700, self._pulse)

    def _apply_device_info(self, info, chips):
        self.dev_info = info
        self.chips = chips
        names = ["ANY"] + [c["name"] for c in chips]
        self.target_cb["values"] = names
        if self.target_var.get() not in names:
            self.target_var.set("ANY")

        if info.get("img") == "1":
            name = info.get("name", "-")
            self.dev_name.configure(text=name if name != "-" else "(unnamed image)")
            self.dev_detail.configure(
                text=f"{int(info.get('size', 0)):,} bytes @ {info.get('base')}   ·   CRC {info.get('crc')}   ·   "
                     f"for {info.get('target')}   ·   fw {info.get('fw')}")
            self.write_log(f"Programmer holds {name} ({int(info.get('size', 0)):,} bytes, CRC {info.get('crc')}, "
                           f"for {info.get('target')})")
        else:
            self.dev_name.configure(text="No firmware stored")
            self.dev_detail.configure(text="Load a .hex file below and upload it to the programmer.")

        self.suppress_set = True
        try:
            self.mode_var.set(info.get("mode", "button"))
            self.erase_var.set(info.get("erase", "chip"))
            self.verify_var.set(info.get("verify", "1") == "1")
            self.run_var.set(info.get("run", "1") == "1")
            self.protect_var.set(info.get("protect", "0") == "1")
            self.settle_var.set(info.get("settle", "500"))
            self.swdelay_var.set(info.get("swdelay", "0"))
        finally:
            self.suppress_set = False
        self._applied_numbers = (self.swdelay_var.get(), self.settle_var.get())
        self._set_counts(int(info.get("pass", 0)), int(info.get("fail", 0)))
        self._update_match()
        self._update_enabled()

    def _set_counts(self, p, f):
        self.pass_lbl.configure(text=str(p))
        self.fail_lbl.configure(text=str(f))
        self.total_lbl.configure(text=str(p + f))
        self.yield_lbl.configure(text=f"{100.0 * p / (p + f):.0f}%" if p + f else "–")

    def _stored_crc(self):
        try:
            return int(self.dev_info.get("crc", "0"), 16) if self.dev_info.get("img") == "1" else None
        except ValueError:
            return None

    def _update_match(self):
        stored = self._stored_crc()
        if not self.link.is_open or not self.image or stored is None:
            self.match_lbl.configure(text="", bg=C["card"])
        elif stored == self.image[3]:
            self.match_lbl.configure(text="✓  Same as the file selected below", bg=C["ok_soft"], fg=C["ok"])
        else:
            self.match_lbl.configure(text="⚠  Different from the file selected below — upload to replace it",
                                     bg=C["warn_soft"], fg=C["warn"])
        self._update_upload_button()

    def _update_upload_button(self):
        stored = self._stored_crc()
        same = self.image and stored == self.image[3]
        if same:
            self.upload_btn.configure(text="Upload again", style="TButton")
        else:
            self.upload_btn.configure(text="Upload to programmer", style="Accent.TButton")
        if not self.image:
            self.upload_btn.state(["disabled"])

    # ================================================================ image
    def browse(self):
        start = os.path.dirname(self.prefs.get("last_file", "")) or None
        path = filedialog.askopenfilename(
            title="Select firmware", initialdir=start,
            filetypes=[("Firmware", "*.hex *.ihx *.bin"), ("All files", "*.*")])
        if path:
            self._load_file(path)

    def _load_file(self, path, quiet=False):
        try:
            base, data = load_image(path)
        except Exception as e:
            if not quiet:
                messagebox.showerror(APP_TITLE, f"Cannot load image:\n{e}")
            return
        crc = zlib.crc32(data)
        self.image = (path, base, data, crc)
        self.path_var.set(os.path.basename(path))
        self.img_lbl.configure(text=f"{len(data):,} bytes   ·   0x{base:08X} – 0x{base + len(data) - 1:08X}   ·   "
                                    f"CRC 0x{crc:08X}   ·   {os.path.dirname(path)}")
        self.prefs["last_file"] = path
        save_prefs(self.prefs)
        self.write_log(f"Selected {os.path.basename(path)} ({len(data):,} bytes @ 0x{base:08X})")
        self._update_match()
        self._update_enabled()

    def upload(self):
        if not self.image:
            messagebox.showwarning(APP_TITLE, "Select a firmware file first.")
            return
        path, base, data, crc = self.image
        target = self.target_var.get() or "ANY"
        chip = next((c for c in self.chips if c["name"] == target), None)
        if chip and base + len(data) > FLASH_BASE + chip["kb"] * 1024:
            messagebox.showerror(APP_TITLE, f"Image ({len(data):,} bytes) does not fit {target} ({chip['kb']} KB).")
            return
        self.prefs["target"] = target
        save_prefs(self.prefs)
        self.run_bg("Upload", self._upload_worker, path, base, data, target)

    def _upload_worker(self, path, base, data, target):
        name = os.path.basename(path).replace(" ", "_")[:40]
        crc = zlib.crc32(data)
        self.link.request(f"BEGIN {len(data)} 0x{crc:08X} 0x{base:08X} {target} {name}")
        t0 = time.time()
        try:
            for off in range(0, len(data), CHUNK):
                self.link.request(f"DATA {off} {data[off:off + CHUNK].hex()}", timeout=3)
                self.ui_q.put(("upload", min(100, (off + CHUNK) * 100 // len(data))))
            info = self.link.request("END", timeout=5)
        except Exception:
            try:
                self.link.request("ABORT")
            except LinkError:
                pass
            raise
        self.ui_q.put(("log", (f"Uploaded {name}: {len(data):,} bytes in {time.time() - t0:.1f}s, "
                               f"CRC 0x{crc:08X} verified", "ok")))
        self.call(self._apply_device_info, parse_kv(info), self.chips)

    # ================================================================ settings
    def _apply_setting(self, key, value):
        if self.suppress_set or not self.link.is_open:
            return

        def work():
            self.link.request(f"SET {key} {value}")
            self.call(self.saved_lbl.configure, text=f"✓  Saved: {key} = {value}")
            if key == "mode":
                self.call(self._update_flash_hint)

        threading.Thread(target=self._safe, args=(work, "Settings"), daemon=True).start()

    def _apply_numbers(self):
        if self.suppress_set or not self.link.is_open:
            return
        try:
            vals = (str(int(self.swdelay_var.get())), str(int(self.settle_var.get())))
        except ValueError:
            return
        if vals == getattr(self, "_applied_numbers", None):
            return
        self._applied_numbers = vals
        self._apply_setting("swdelay", vals[0])
        self._apply_setting("settle", vals[1])

    def _safe(self, fn, title):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            self.ui_q.put(("log", (f"{title}: {e}", "err")))

    # ================================================================ tools
    def flash_now(self):
        if self.device_busy:
            return
        if self.link.is_open and self.dev_info.get("img") != "1":
            messagebox.showinfo(APP_TITLE, "The programmer has no firmware yet. Upload one first.")
            return
        self.run_bg("Flash", lambda: self.link.request("FLASH"))

    def detect(self):
        def work():
            info = parse_kv(self.link.request("DETECT", timeout=10))
            self.last_detect = info
            msg = (f"{info.get('chip')}  ·  DEV_ID {info.get('devid')}  ·  {info.get('flash')} KB flash  ·  "
                   f"read-protected: {'yes' if info.get('protected') == '1' else 'no'}  ·  "
                   f"SWD delay {info.get('swdelay')}")
            self.ui_q.put(("log", (f"Detected {msg}", "ok")))
            kind = "err" if info.get("chip") == "UNKNOWN" else "ok"
            self.call(self._set_banner, "Chip detected" if kind == "ok" else "Unknown chip", msg, kind)
            if kind == "err":
                self.call(self._ask_identify, info.get("devid", ""))

        self.run_bg("Detect", work)

    def reset_target(self):
        def work():
            self.link.request("RESET", timeout=10)
            self.ui_q.put(("log", ("Target reset", "ok")))
        self.run_bg("Reset", work)

    def erase_chip(self):
        if messagebox.askyesno(APP_TITLE, "Mass-erase the connected target?"):
            def work():
                self.link.request("ERASE", timeout=15)
                self.ui_q.put(("log", ("Target erased", "ok")))
            self.run_bg("Erase", work)

    def unlock(self):
        if messagebox.askyesno(APP_TITLE, "Remove read protection?\n\nThis ERASES the whole target flash. "
                                          "Power-cycle the target afterwards."):
            def work():
                msg = self.link.request("UNLOCK", timeout=20)
                self.ui_q.put(("log", (f"Unlock: {msg}", "ok")))
            self.run_bg("Unlock", work)

    def send_raw(self):
        """Send a raw protocol command (e.g. 'READ 0x08000000 16') and log the reply."""
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return
        self.cmd_var.set("")
        self.write_log(f"> {cmd}", "dim")

        def work():
            reply = self.link.request(cmd, timeout=20)
            self.ui_q.put(("log", (reply or "OK", "ok")))
        self.run_bg(cmd.split()[0].upper(), work)

    def reset_counters(self):
        if messagebox.askyesno(APP_TITLE, "Reset PASS / FAIL counters on the programmer?"):
            self.run_bg("Reset counters", lambda: self.link.request("RESETCNT"))

    # ================================================================ chips dialog
    def chips_dialog(self):
        if not self.link.is_open:
            messagebox.showwarning(APP_TITLE, "Connect to the programmer first.")
            return
        win = tk.Toplevel(self)
        win.title("Chip table")
        win.configure(bg=C["card"])
        win.transient(self)
        ttk.Label(win, text="Chips the programmer recognises (by DEV_ID @ 0x40013400)",
                  style="Muted.TLabel").pack(anchor="w", padx=10, pady=(10, 0))
        tree = ttk.Treeview(win, columns=("id", "name", "kb", "page", "kind"), show="headings", height=8)
        for col, txt, w in (("id", "DEV_ID", 110), ("name", "Name", 130), ("kb", "Flash KB", 70),
                            ("page", "Page", 60), ("kind", "Type", 70)):
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="center")
        tree.pack(fill="both", expand=True, padx=10, pady=8)

        def fill():
            tree.delete(*tree.get_children())
            for c in self.chips:
                pending = c["id"] == 0
                tree.insert("", "end", values=("not learned yet" if pending else f"0x{c['id']:08X}", c["name"],
                                               c["kb"], c["page"], "built-in" if c["builtin"] else "custom"))

        form = ttk.Frame(win, style="Card.TFrame", padding=10)
        form.pack(fill="x")
        id_var, name_var, kb_var, page_var = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar(value="1024")
        for i, (lbl, var, w) in enumerate((("DEV_ID", id_var, 12), ("Name", name_var, 14),
                                          ("Flash KB", kb_var, 6), ("Page", page_var, 6))):
            ttk.Label(form, text=lbl, style="Card.TLabel").grid(row=0, column=i, sticky="w")
            ttk.Entry(form, textvariable=var, width=w).grid(row=1, column=i, padx=(0, 6))

        def use_detected():
            if self.last_detect:
                id_var.set(self.last_detect.get("devid", ""))
            else:
                messagebox.showinfo(APP_TITLE, "Run 'Detect chip' with the new chip connected first.", parent=win)

        def add():
            try:
                dev = int(id_var.get(), 0)
                kb = int(kb_var.get())
                page = int(page_var.get())
                name = name_var.get().strip().replace(" ", "_")
                assert name and dev and kb and page
            except Exception:
                messagebox.showerror(APP_TITLE, "Enter DEV_ID (e.g. 0xCC4350D1), name, flash KB and page size.",
                                     parent=win)
                return

            def work():
                self.chips = parse_chips(self.link.request(f"CHIPADD 0x{dev:08X} {name} {kb} {page}"))
                self.call(fill)
                self.call(self._apply_device_info, self.dev_info, self.chips)
            self.run_bg("Add chip", work)

        def delete():
            sel = tree.selection()
            if not sel:
                return
            dev = tree.item(sel[0])["values"][0]

            def work():
                self.chips = parse_chips(self.link.request(f"CHIPDEL {dev}"))
                self.call(fill)
                self.call(self._apply_device_info, self.dev_info, self.chips)
            self.run_bg("Delete chip", work)

        btns = ttk.Frame(win, style="Card.TFrame", padding=(10, 0, 10, 10))
        btns.pack(fill="x")
        ttk.Button(btns, text="Use detected ID", command=use_detected).pack(side="left")
        ttk.Button(btns, text="Add / update", style="Accent.TButton", command=add).pack(side="left", padx=6)
        ttk.Button(btns, text="Delete selected", command=delete).pack(side="left")
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")
        fill()

    # ================================================================ events
    def _drain(self):
        try:
            while True:
                item = self.ui_q.get_nowait()
                if isinstance(item, str):
                    self._on_device_line(item)
                    continue
                kind, val = item
                if kind == "log":
                    self.write_log(*val)
                elif kind == "error":
                    self.write_log(val, "err")
                    messagebox.showerror(APP_TITLE, val)
                elif kind == "idle":
                    self.busy = False
                elif kind == "upload":
                    self.up_prog["value"] = val
                elif kind == "call":
                    fn, args, kwargs = val
                    fn(*args, **kwargs)
        except queue.Empty:
            pass
        self.after(50, self._drain)

    def _set_device_busy(self, busy):
        if busy != self.device_busy:
            self.device_busy = busy
            self._update_enabled()

    def _on_device_line(self, line):
        if not line.startswith("@EVT"):
            return  # boot noise from the ESP8266 ROM
        parts = line.split(" ", 3)
        ev = parts[1] if len(parts) > 1 else ""
        if ev == "COUNT":
            self._set_counts(int(parts[2]), int(parts[3]))
        elif ev == "STATE":
            st = parts[2]
            if st == "idle":
                self._set_device_busy(False)
                return
            self._set_device_busy(True)
            if st == "connect":
                self.prog["value"] = 0
            self._set_banner(self.STATE_TEXT.get(st, st) + "…", "", "busy")
        elif ev == "PROGRESS":
            self.prog["value"] = int(parts[3])
            self.result_lbl.configure(text=f"{parts[2].capitalize()}  {parts[3]}%")
        elif ev == "CHIP":
            self.write_log(f"Target: {parts[2]} ({parts[3]})")
        elif ev == "TARGET":
            self.write_log(f"Board {parts[2]}", "dim")
        elif ev == "RESULT":
            self._on_result(line)
        elif ev == "BOOT":
            self.write_log("Programmer restarted", "dim")
            if self.link.is_open:
                self.refresh_device()
        elif ev == "LINKLOST":
            self.link.close()
            self._set_connected(False)
            self.write_log("Serial link lost — USB cable unplugged?", "err")
        else:
            self.write_log(line, "dim")

    def _on_result(self, line):
        parts = line.split(" ")
        img = self.dev_info.get("name", "")
        if parts[2] == "PASS":
            ms, chip, devid = parts[3], parts[4], parts[5]
            self.prog["value"] = 100
            msg = f"{chip} programmed and verified in {int(ms) / 1000:.2f} s"
            self._set_banner("✓  PASS", msg, "ok")
            self.write_log(f"PASS  {msg}", "ok")
            self._csv(["PASS", chip, devid, ms, "", img])
        else:
            counted = parts[3] == "1"
            reason = " ".join(parts[4:])
            self._set_banner("✕  FAIL", reason + ("" if counted else "   (not counted)"), "err")
            self.write_log(f"FAIL  {reason}" + ("" if counted else "  (not counted)"), "err")
            self._csv(["FAIL" if counted else "NO-TARGET", "", "", "", reason, img])
            m = re.search(r"Unknown chip ID (0x[0-9A-Fa-f]{8})", reason)
            if m:
                self.after(100, self._ask_identify, m.group(1))
        self.bell()

    def _ask_identify(self, devid):
        """An unknown DEV_ID was read: let the user say which chip it is, then teach the programmer."""
        try:
            dev = int(devid, 16)
        except ValueError:
            return
        if not dev or getattr(self, "_identify_open", False):
            return
        pending = [c for c in self.chips if c["id"] == 0]
        win = tk.Toplevel(self)
        win.title("Unknown chip")
        win.configure(bg=C["card"])
        win.transient(self)
        win.resizable(False, False)
        self._identify_open = True

        def close():
            self._identify_open = False
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

        body = ttk.Frame(win, style="Card.TFrame", padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="New chip found", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"The connected chip reports DEV_ID 0x{dev:08X}, which the programmer does not "
                             f"know yet.\nWhich chip is on this board? The programmer will remember it.",
                  style="Card.TLabel", justify="left").pack(anchor="w", pady=(4, 12))
        choice = tk.StringVar()
        target = self.dev_info.get("target", "")
        for c in pending:
            ttk.Radiobutton(body, text=f"{c['name']}   ({c['kb']} KB flash)", value=c["name"], variable=choice,
                            style="Card.TRadiobutton").pack(anchor="w", pady=2)
        ttk.Radiobutton(body, text="Another chip (enter the details in the chip table)", value="__other__",
                        variable=choice, style="Card.TRadiobutton").pack(anchor="w", pady=2)
        choice.set(target if any(c["name"] == target for c in pending) else
                   (pending[0]["name"] if pending else "__other__"))

        def ok():
            name = choice.get()
            close()
            if name == "__other__":
                self.last_detect = {"devid": f"0x{dev:08X}"}
                self.chips_dialog()
                return
            chip = next(c for c in pending if c["name"] == name)

            def work():
                self.chips = parse_chips(self.link.request(
                    f"CHIPADD 0x{dev:08X} {chip['name']} {chip['kb']} {chip['page']}"))
                self.ui_q.put(("log", (f"{chip['name']} learned (DEV_ID 0x{dev:08X}). It is recognised from now "
                                       f"on — press FLASH again.", "ok")))
                self.call(self._apply_device_info, self.dev_info, self.chips)
                self.call(self._set_banner, "Chip learned", f"{chip['name']} = DEV_ID 0x{dev:08X}. "
                                                            f"Press FLASH to program it.", "ok")
            self.run_bg("Learn chip", work)

        btns = ttk.Frame(body, style="Card.TFrame")
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=close).pack(side="right")
        ttk.Button(btns, text="Remember this chip", style="Accent.TButton", command=ok).pack(side="right", padx=6)
        win.grab_set()

    def _csv(self, row):
        try:
            new = not os.path.exists(LOG_CSV)
            with open(LOG_CSV, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "result", "chip", "dev_id", "ms", "message", "image"])
                w.writerow([dt.datetime.now().isoformat(timespec="seconds")] + row)
        except OSError as e:
            self.write_log(f"Cannot write {LOG_CSV}: {e}", "err")

    def _on_close(self):
        self.link.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
