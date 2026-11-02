"""Offline Programmer - desktop UI (MM32 and Renesas RA2E1 over SWD, Fortior FU68xx over FICE).

Talks to the ESP8266 programmer over UART. Selects the chip family, loads an
Intel HEX / S-record / BIN file, uploads it to the programmer's flash, configures the
programmer, and shows live pass/fail counters.

Product models: scan or type a model code; its firmware and settings come from the firmware
catalog (see catalog.py) and are loaded onto the programmer.

Requires: Python 3.8+, pyserial  (pip install pyserial)
"""

import binascii
import csv
import datetime as dt
import glob
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

import catalog as cat

APP_TITLE = "Offline Programmer"
DEFAULT_BAUD = 460800
CHUNK = 256
LOG_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flash_log.csv")

# Chip families the firmware supports ("SET family <key>"). Settings lists the family-specific
# programmer settings; mode, verify, run and settle are common to all.
FAMILIES = {
    "mm32": {"label": "MM32  ·  SWD", "name": "MM32", "iface": "SWD", "settings": ("erase", "protect", "swdelay")},
    "fortior": {"label": "Fortior  ·  FICE", "name": "Fortior FU68xx", "iface": "FICE",
                "settings": ("speed",)},
    "renesas": {"label": "Renesas RA  ·  SWD", "name": "Renesas RA2E1", "iface": "SWD",
                "settings": ("erase", "reset", "swdelay")},
}
COMMON_SETTINGS = ("mode", "verify", "run", "settle")
SETTING_DEFAULTS = {"mode": "button", "verify": "1", "run": "1", "settle": "500", "erase": "chip", "protect": "0",
                    "swdelay": "0", "speed": "100", "reset": "1"}

# MM32
FLASH_BASE = 0x08000000

# Fortior FU68xx
FU68_FLASH_SIZE = 0x4000
OPT_ADDR = 0x3FFC  # Keil appends 4 option bytes here (mirrored into CCFG 0x401C..0x401E)
# Option bytes as Keil writes them (from captures). 0x3FFD bits 7:6 = LVR, 0x3FFE bit 5 = WDT enable.
OPT_DEFAULT = "00F000FF"
LVR_CHOICES = [("3.8 V", 0xC0), ("3.0 V", 0x80)]  # values verified against Keil captures
LVR_MASK = 0xC0
WDT_BIT = 0x20

# Renesas RA2E1 memory map (R7FA2E1A9 / A7 / A5: 128 / 64 / 32 KB code flash)
RA_CF_BASE, RA_CF_MAX = 0x00000000, 128 * 1024
RA_DF_BASE, RA_DF_SIZE = 0x40100000, 4 * 1024
RA_EXTRA_BASE, RA_EXTRA_END = 0x01010000, 0x01010040  # option-setting extra area: ID code, access window


def crc16_ccitt(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE, the algorithm of the FU68xx CRC unit Keil uses for verify."""
    return binascii.crc_hqx(bytes(data), crc)  # same polynomial (0x1021), unreflected, in C


def chip_crc(data, opt):
    """CRC the FU68xx reports after programming `data` (padded with 0x00) plus the option bytes."""
    img = bytes(data[:OPT_ADDR]) + bytes(OPT_ADDR - min(len(data), OPT_ADDR))
    return crc16_ccitt(bytes.fromhex(opt), crc16_ccitt(img))


# --------------------------------------------------------------------------- image loading

def intel_hex_mem(text):
    """Return {address: byte} for an Intel HEX file."""
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
    return mem


def parse_intel_hex(text, fill=0xFF):
    """Return (base_address, bytearray) for an Intel HEX file; gaps are filled with `fill`."""
    mem = intel_hex_mem(text)
    if not mem:
        raise ValueError("no data records in file")
    lo, hi = min(mem), max(mem) + 1
    buf = bytearray(bytes([fill]) * (hi - lo))
    for a, b in mem.items():
        buf[a - lo] = b
    return lo, buf


def parse_srec(text):
    """Return {address: byte} for a Motorola S-record file (e2 studio .srec / .mot)."""
    mem = {}
    addr_len = {"1": 2, "2": 3, "3": 4}
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line[0] != "S" or len(line) < 4:
            raise ValueError(f"line {ln}: not an S-record")
        kind = line[1]
        try:
            rec = bytes.fromhex(line[2:])
        except ValueError:
            raise ValueError(f"line {ln}: invalid hex characters")
        if len(rec) != rec[0] + 1:
            raise ValueError(f"line {ln}: bad record length")
        if (sum(rec) & 0xFF) != 0xFF:
            raise ValueError(f"line {ln}: checksum error")
        if kind in addr_len:
            n = addr_len[kind]
            base = int.from_bytes(rec[1:1 + n], "big")
            for i, b in enumerate(rec[1 + n:-1]):
                mem[base + i] = b
        elif kind in "789":
            break
        # S0 header, S5/S6 counts: nothing to program
    return mem


def load_image(path, family):
    """Load an image for a chip family. Returns (base, bytes, layout); layout is {} except for Renesas."""
    if family == "renesas":
        return load_ra_image(path)
    loader = load_fu68_image if family == "fortior" else load_mm32_image
    return loader(path) + ({},)


def load_ra_image(path):
    """Renesas RA2E1: code flash bytes from 0x0, then data flash bytes from 0x40100000; gaps are 0xFF.
    Returns (0, bytes, {"cf": n, "df": n})."""
    with open(path, "rb") as f:
        raw = f.read()
    if path.lower().endswith(".bin"):
        mem = {RA_CF_BASE + i: b for i, b in enumerate(raw)}
    elif raw.lstrip()[:1] == b":":
        mem = intel_hex_mem(raw.decode("ascii", errors="replace"))
    elif raw.lstrip()[:1] == b"S":
        mem = parse_srec(raw.decode("ascii", errors="replace"))
    else:
        raise ValueError("not an Intel HEX, S-record or .bin file")
    if not mem:
        raise ValueError("no data records in file")
    cf, df, extra, other = {}, {}, {}, []
    for a, b in mem.items():
        if RA_CF_BASE <= a < RA_CF_BASE + RA_CF_MAX:
            cf[a - RA_CF_BASE] = b
        elif RA_DF_BASE <= a < RA_DF_BASE + RA_DF_SIZE:
            df[a - RA_DF_BASE] = b
        elif RA_EXTRA_BASE <= a < RA_EXTRA_END:
            extra[a] = b
        else:
            other.append(a)
    if other:
        raise ValueError(f"data at 0x{min(other):08X}-0x{max(other):08X} is outside the RA2E1 code flash "
                         f"(0x00000000), data flash (0x40100000) and option area")
    custom = sorted(a for a, b in extra.items() if b != 0xFF)
    if custom:
        raise ValueError(f"the file sets the ID code / access window at 0x{custom[0]:08X} (option-setting extra "
                         f"area). This programmer only supports the default (all 0xFF, unlocked) setting.")

    def blob(m, align):
        if not m:
            return b""
        size = max(m) + 1
        size += -size % align
        buf = bytearray(b"\xff" * size)
        for off, b in m.items():
            buf[off] = b
        return bytes(buf)

    cf_blob, df_blob = blob(cf, 4), blob(df, 1)
    return 0, cf_blob + df_blob, {"cf": len(cf_blob), "df": len(df_blob)}


def describe_ra_layout(layout):
    parts = [f"code flash {layout['cf']:,} B" if layout["cf"] else "no code flash data"]
    if layout["df"]:
        parts.append(f"data flash {layout['df']:,} B")
    return "   ·   ".join(parts)


def load_mm32_image(path):
    """MM32: (base, bytes) aligned/padded for halfword+word access; gaps are 0xFF (erased)."""
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


def load_fu68_image(path):
    """Fortior FU68xx: the image as bytes from address 0x0000; gaps are 0x00 (erased)."""
    if path.lower().endswith((".hex", ".ihx")):
        with open(path, "r", encoding="ascii", errors="replace") as f:
            base, data = parse_intel_hex(f.read(), fill=0x00)
        data = bytearray(base) + data  # place at its address; flash starts at 0x0000
    else:
        with open(path, "rb") as f:
            data = bytearray(f.read())
    while data and data[-1] == 0:  # trailing 0x00 is the erased state anyway
        data.pop()
    if not data:
        raise ValueError("the image is empty")
    if len(data) > OPT_ADDR:
        if len(data) > FU68_FLASH_SIZE:
            raise ValueError(f"image ends at 0x{len(data) - 1:04X}, beyond the 16 KB flash of the FU68xx.")
        raise ValueError(f"image uses 0x{OPT_ADDR:04X}-0x3FFF, where the programmer writes the option bytes.")
    return 0, bytes(data)


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
    # dark theme (slate)
    "bg": "#0e131b", "card": "#161d28", "border": "#273244", "text": "#e6eaf0", "muted": "#8c99ad",
    "accent": "#3b82f6", "accent_hover": "#2563eb", "accent_glow": "#60a5fa", "accent_soft": "#172a46",
    "accent_text": "#93c5fd",
    "ok": "#22c55e", "ok_hover": "#16a34a", "ok_soft": "#0f2a1d", "ok_text": "#4ade80",
    "err": "#ef4444", "err_hover": "#dc2626", "err_soft": "#341719", "err_text": "#f87171",
    "warn": "#fbbf24", "warn_soft": "#33280e", "idle_soft": "#1c2433",
    "header": "#090d13", "header_text": "#e6eaf0", "header_muted": "#7c899c", "disabled": "#4b5567",
    "btn": "#263142", "btn_hover": "#303d52", "btn_pressed": "#3a4962", "btn_border": "#3d4c64",
    "btn_dis": "#1b2230", "btn_dis_border": "#262f3f", "btn_dis_text": "#5d6a7d", "acc_dis": "#2a3446",
    "field": "#0f1520", "field_border": "#334155", "log_bg": "#0f1520", "log_time": "#5d6a7d",
    "pill_on_bg": "#0f2a1d", "pill_on_fg": "#4ade80", "pill_off_bg": "#341719", "pill_off_fg": "#fca5a5",
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
        self.geometry(f"1200x{min(960, screen_h - 80)}")
        self.minsize(1060, 760)
        if screen_h < 1000:
            self.state("zoomed")  # small laptop screens: use the whole screen
        self.configure(bg=C["bg"])
        self.ui_q = queue.Queue()
        self.link = SerialLink(self.ui_q.put)
        self.prefs = load_prefs()
        files = self.prefs.setdefault("files", {})  # last file per family
        if "last_file" in self.prefs:  # prefs from the single-family UI
            files.setdefault(self.prefs.get("family", "mm32"), self.prefs.pop("last_file"))
        self.family = self.prefs.get("family", "mm32")
        if self.family not in FAMILIES:
            self.family = "mm32"
        self.image = None          # (path, base, data, crc), loaded for self.family
        self.image_layout = {}     # Renesas: {"cf": code flash bytes, "df": data flash bytes} of self.image
        self.chips = []
        self.dev_info = {}
        self.last_detect = None
        self.busy = False          # a PC-side request is running
        self.device_busy = False   # the programmer is flashing
        self.suppress_set = False  # don't echo settings back while loading them
        self.conn_widgets = []     # enabled only while connected
        self.tool_btns = []
        self.pulse_on = False
        self.catalog_dir = self.prefs.get("catalog_dir") or cat.default_dir()
        self.models = []
        self.active_model = None   # catalog model whose image the programmer holds
        for old in ("production", "pin_hash"):  # production mode was removed
            self.prefs.pop(old, None)
        self._load_catalog()

        self._styles()
        self._build()
        self.refresh_ports()
        self._set_connected(False)
        self._show_family(self.family)
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
                    focuscolor=C["accent"], selectbackground=C["accent"], selectforeground="#ffffff",
                    fieldbackground=C["field"], insertcolor=C["text"], arrowcolor=C["text"])
        s.configure("TFrame", background=C["bg"])
        s.configure("Card.TFrame", background=C["card"])
        s.configure("Header.TFrame", background=C["header"])
        s.configure("TLabel", background=C["bg"], foreground=C["text"])
        s.configure("Card.TLabel", background=C["card"])
        s.configure("Muted.TLabel", background=C["card"], foreground=C["muted"], font=(FONT, 9))
        s.configure("CardTitle.TLabel", background=C["card"], font=(FONT, 11, "bold"))
        s.configure("Step.TLabel", background=C["accent_soft"], foreground=C["accent_text"],
                    font=(FONT, 9, "bold"), padding=(7, 1))
        s.configure("FileName.TLabel", background=C["card"], font=(FONT, 15, "bold"))
        s.configure("Header.TLabel", background=C["header"], foreground=C["header_text"])
        s.configure("HeaderTitle.TLabel", background=C["header"], foreground="#ffffff", font=(FONT, 15, "bold"))
        s.configure("HeaderSub.TLabel", background=C["header"], foreground=C["header_muted"], font=(FONT, 9))

        def button(style, bg, hover, pressed, border, fg, dis_bg, dis_fg, dis_border, **extra):
            s.configure(style, background=bg, foreground=fg, bordercolor=border, lightcolor=bg, darkcolor=bg,
                        relief="flat", **extra)
            fills = [("disabled", dis_bg), ("pressed", pressed), ("active", hover)]
            s.map(style, background=fills, lightcolor=fills, darkcolor=fills,
                  bordercolor=[("disabled", dis_border), ("active", hover if border == bg else C["accent"])],
                  foreground=[("disabled", dis_fg)])

        # Secondary buttons: slate fill with a visible border.
        button("TButton", C["btn"], C["btn_hover"], C["btn_pressed"], C["btn_border"], C["text"],
               C["btn_dis"], C["btn_dis_text"], C["btn_dis_border"], padding=(12, 6), font=(FONT, 10, "bold"))
        for name, bg, hover in (("Accent", C["accent"], C["accent_hover"]),
                                ("Pulse", C["accent_glow"], C["accent"]),
                                ("Ok", C["ok"], C["ok_hover"]),
                                ("Danger", C["err"], C["err_hover"])):
            button(f"{name}.TButton", bg, hover, hover, bg, "#ffffff", C["acc_dis"], C["btn_dis_text"],
                   C["acc_dis"], padding=(16, 7), font=(FONT, 10, "bold"))
        button("Flash.TButton", C["accent"], C["accent_hover"], C["accent_hover"], C["accent"], "#ffffff",
               C["acc_dis"], C["btn_dis_text"], C["acc_dis"], padding=(20, 14), font=(FONT, 16, "bold"))
        button("Header.TButton", C["btn"], C["btn_hover"], C["btn_pressed"], C["btn_border"], "#ffffff",
               C["header"], C["btn_dis_text"], C["btn_dis_border"], padding=(6, 1), font=(FONT, 12, "bold"))
        s.configure("Link.TButton", background=C["card"], foreground=C["accent_text"], padding=(4, 2),
                    bordercolor=C["card"], lightcolor=C["card"], darkcolor=C["card"], relief="flat",
                    font=(FONT, 9, "underline"))
        s.map("Link.TButton", background=[("active", C["accent_soft"])],
              lightcolor=[("active", C["accent_soft"])], darkcolor=[("active", C["accent_soft"])],
              bordercolor=[("active", C["accent_soft"])], foreground=[("disabled", C["btn_dis_text"])])

        for style in ("Card.TCheckbutton", "Card.TRadiobutton"):
            s.configure(style, background=C["card"], foreground=C["text"], indicatorbackground=C["field"],
                        indicatorforeground=C["accent_text"], upperbordercolor=C["field_border"],
                        lowerbordercolor=C["field_border"])
            s.map(style, background=[("active", C["card"])],
                  indicatorbackground=[("disabled", C["btn_dis"]), ("pressed", C["btn"])],
                  foreground=[("disabled", C["btn_dis_text"])])
        s.configure("Seg.Toolbutton", background=C["btn"], foreground=C["text"], padding=(14, 5),
                    font=(FONT, 9, "bold"), anchor="center", bordercolor=C["btn_border"], relief="flat",
                    lightcolor=C["btn"], darkcolor=C["btn"])
        seg = [("disabled", C["btn_dis"]), ("selected", C["accent"]), ("active", C["btn_hover"])]
        s.map("Seg.Toolbutton", background=seg, lightcolor=seg, darkcolor=seg,
              bordercolor=[("disabled", C["btn_dis_border"]), ("selected", C["accent"])],
              foreground=[("disabled", C["btn_dis_text"]), ("selected", "#ffffff")])

        for style in ("TCombobox", "Header.TCombobox"):
            s.configure(style, padding=4, arrowsize=14, fieldbackground=C["field"], background=C["btn"],
                        foreground=C["text"], arrowcolor=C["text"], bordercolor=C["field_border"],
                        lightcolor=C["field"], darkcolor=C["field"])
            s.map(style, fieldbackground=[("readonly", C["field"]), ("disabled", C["btn_dis"])],
                  background=[("active", C["btn_hover"]), ("disabled", C["btn_dis"])],
                  foreground=[("disabled", C["btn_dis_text"]), ("readonly", C["text"])],
                  arrowcolor=[("disabled", C["btn_dis_text"])],
                  selectbackground=[("readonly", C["field"])], selectforeground=[("readonly", C["text"])])
        self.option_add("*TCombobox*Listbox.font", base)
        self.option_add("*TCombobox*Listbox.background", C["field"])
        self.option_add("*TCombobox*Listbox.foreground", C["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        for style in ("TEntry", "TSpinbox"):
            s.configure(style, fieldbackground=C["field"], foreground=C["text"], bordercolor=C["field_border"],
                        lightcolor=C["field"], darkcolor=C["field"], insertcolor=C["text"], padding=3,
                        arrowsize=12, background=C["btn"], arrowcolor=C["text"])
            s.map(style, fieldbackground=[("disabled", C["btn_dis"])], foreground=[("disabled", C["btn_dis_text"])],
                  bordercolor=[("focus", C["accent"])], arrowcolor=[("disabled", C["btn_dis_text"])])
        s.configure("Vertical.TScrollbar", background=C["btn"], troughcolor=C["log_bg"], bordercolor=C["log_bg"],
                    lightcolor=C["btn"], darkcolor=C["btn"], arrowcolor=C["muted"])
        s.map("Vertical.TScrollbar", background=[("active", C["btn_hover"])])
        s.configure("Treeview", background=C["field"], fieldbackground=C["field"], foreground=C["text"],
                    bordercolor=C["border"], rowheight=24)
        s.map("Treeview", background=[("selected", C["accent"])], foreground=[("selected", "#ffffff")])
        s.configure("Treeview.Heading", background=C["btn"], foreground=C["text"], relief="flat",
                    font=(FONT, 9, "bold"), lightcolor=C["btn"], darkcolor=C["btn"], bordercolor=C["border"])
        s.map("Treeview.Heading", background=[("active", C["btn_hover"])])
        s.configure("Horizontal.TProgressbar", thickness=10, background=C["accent"], troughcolor=C["idle_soft"],
                    bordercolor=C["idle_soft"], lightcolor=C["accent"], darkcolor=C["accent"])
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
        ttk.Label(title, text="Offline Programmer", style="HeaderTitle.TLabel").pack(anchor="w")
        self.header_sub = ttk.Label(title, text="", style="HeaderSub.TLabel")
        self.header_sub.pack(anchor="w")

        conn = ttk.Frame(header, style="Header.TFrame")
        conn.pack(side="right")
        self.status_pill = tk.Label(conn, text="●  Disconnected", bg=C["pill_off_bg"], fg=C["pill_off_fg"],
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
        tk.Label(self.hint, text="①", bg=C["accent_soft"], fg=C["accent_text"], font=(FONT, 14, "bold")).pack(
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
        self._build_model_card(right)
        self._build_status_card(right)
        self._build_counter_card(right)
        self._build_tools_card(right)
        self._build_log(body)

    def _build_model_card(self, parent):
        card = self._card(parent, "Product model", "2")
        card.pack(fill="x", pady=(0, 10))
        b = card.body
        row = ttk.Frame(b, style="Card.TFrame")
        row.pack(fill="x")
        self.model_var = tk.StringVar()
        self.model_cb = ttk.Combobox(row, textvariable=self.model_var, font=(MONO, 13))
        self.model_cb.pack(side="left", fill="x", expand=True)
        self.model_cb.bind("<Return>", lambda e: (self.select_model(), "break")[1])
        self.model_cb.bind("<KeyRelease>", self._filter_models)
        self.model_cb.bind("<<ComboboxSelected>>", lambda e: self.select_model())
        self.catalog_btn = ttk.Button(row, text="Catalog…", command=self.catalog_dialog)
        self.catalog_btn.pack(side="right", padx=(8, 0))
        self.model_btn = self._track(ttk.Button(row, text="Load model", style="Accent.TButton",
                                                command=self.select_model))
        self.model_btn.pack(side="right", padx=(8, 0))
        self.model_name = ttk.Label(b, text="", style="FileName.TLabel")
        self.model_name.pack(anchor="w", pady=(8, 0))
        self.model_detail = ttk.Label(b, text="", style="Muted.TLabel")
        self.model_detail.pack(anchor="w")
        self._fill_model_list()
        self._show_active_model()

    def _build_device_card(self, parent):
        card = self._card(parent, "On the programmer", action=("Refresh", self.refresh_device))
        card.pack(fill="x")
        self.device_card = card
        b = card.body
        self.family_row = row = ttk.Frame(b, style="Card.TFrame")
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="Chip family", style="Card.TLabel").pack(side="left", padx=(0, 12))
        self.family_var = tk.StringVar(value=self.family)
        self.family_btns = []
        for key, fam in FAMILIES.items():
            btn = ttk.Radiobutton(row, text=fam["label"], value=key, variable=self.family_var,
                                  style="Seg.Toolbutton", command=self.pick_family)
            btn.pack(side="left")
            self.family_btns.append(btn)
        self.dev_name = ttk.Label(b, text="—", style="FileName.TLabel")
        self.dev_name.pack(anchor="w")
        self.dev_detail = ttk.Label(b, text="Connect to see the stored firmware.", style="Muted.TLabel")
        self.dev_detail.pack(anchor="w", pady=(2, 6))
        self.match_lbl = tk.Label(b, text="", font=(FONT, 9, "bold"), bg=C["card"], padx=8, pady=2)
        self.match_lbl.pack(anchor="w")

    def _build_file_card(self, parent):
        card = self._card(parent, "Load firmware file")
        card.pack(fill="x", pady=10)
        self.file_card = card
        b = card.body
        row = ttk.Frame(b, style="Card.TFrame")
        row.pack(fill="x")
        self.path_var = tk.StringVar(value="No file selected")
        ttk.Label(row, textvariable=self.path_var, style="Card.TLabel", font=(FONT, 10, "bold")).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self.browse).pack(side="right")
        self.img_lbl = ttk.Label(b, text="Intel HEX (.hex) or binary (.bin) from Keil", style="Muted.TLabel")
        self.img_lbl.pack(anchor="w", pady=(2, 8))

        # Fortior: option bytes Keil sets in "Fortior C51 FICE Driver Setting" (written to 0x3FFC..0x3FFF).
        self.opt_row = row = ttk.Frame(b, style="Card.TFrame")
        ttk.Label(row, text="LVR", style="Card.TLabel").pack(side="left")
        self.lvr_var = tk.StringVar()
        lvr = ttk.Combobox(row, textvariable=self.lvr_var, width=8, state="readonly",
                           values=[n for n, _ in LVR_CHOICES] + ["custom"])
        lvr.pack(side="left", padx=(6, 14))
        self.wdt_var = tk.BooleanVar()
        ttk.Checkbutton(row, text="Watchdog enable", variable=self.wdt_var, style="Card.TCheckbutton").pack(
            side="left")
        ttk.Label(row, text="Option bytes", style="Card.TLabel").pack(side="left", padx=(14, 6))
        self.opt_var = tk.StringVar(value=self.prefs.get("opt", OPT_DEFAULT))
        ttk.Entry(row, textvariable=self.opt_var, width=10, font=(MONO, 10)).pack(side="left")
        self._opt_sync = False
        self.lvr_var.trace_add("write", lambda *a: self._opt_from_controls())
        self.wdt_var.trace_add("write", lambda *a: self._opt_from_controls())
        self.opt_var.trace_add("write", lambda *a: self._controls_from_opt())

        self.upload_row = row = ttk.Frame(b, style="Card.TFrame")
        row.pack(fill="x")
        self.upload_btn = self._track(ttk.Button(row, text="Upload to programmer", style="Accent.TButton",
                                                 command=self.upload))
        self.upload_btn.pack(side="right")
        # MM32: which chip the image is for. Fortior: the CRC the chip must report.
        self.target_box = ttk.Frame(row, style="Card.TFrame")
        ttk.Label(self.target_box, text="For chip", style="Card.TLabel").pack(side="left")
        self.target_var = tk.StringVar(value=self.prefs.get("target", "ANY"))
        self.target_cb = self._track(ttk.Combobox(self.target_box, textvariable=self.target_var, width=14,
                                                  state="readonly", values=["ANY"]))
        self.target_cb.pack(side="left", padx=8)
        self.crc_lbl = ttk.Label(row, text="", style="Muted.TLabel")
        self.up_prog = ttk.Progressbar(b, maximum=100)
        self.up_prog.pack(fill="x", pady=(10, 0))
        self._controls_from_opt()

    # ---- Fortior option bytes <-> LVR / watchdog controls
    def _opt_bytes(self):
        """The 4 option bytes from the entry, or None when it is not 8 hex digits."""
        text = self.opt_var.get().strip().replace(" ", "")
        if not re.fullmatch(r"[0-9A-Fa-f]{8}", text):
            return None
        return bytearray(bytes.fromhex(text))

    def _opt_from_controls(self):
        if self._opt_sync:
            return
        opt = self._opt_bytes() or bytearray(bytes.fromhex(OPT_DEFAULT))
        lvr = dict(LVR_CHOICES).get(self.lvr_var.get())
        if lvr is not None:
            opt[1] = (opt[1] & ~LVR_MASK & 0xFF) | lvr
        opt[2] = (opt[2] | WDT_BIT) if self.wdt_var.get() else (opt[2] & ~WDT_BIT & 0xFF)
        self._opt_sync = True
        self.opt_var.set(opt.hex().upper())
        self._opt_sync = False
        self._update_crc()

    def _controls_from_opt(self):
        if self._opt_sync:
            return
        opt = self._opt_bytes()
        self._opt_sync = True
        if opt:
            names = {v: n for n, v in LVR_CHOICES}
            self.lvr_var.set(names.get(opt[1] & LVR_MASK, "custom"))
            self.wdt_var.set(bool(opt[2] & WDT_BIT))
        self._opt_sync = False
        self._update_crc()

    def _update_crc(self):
        if not hasattr(self, "crc_lbl") or self.family != "fortior":
            return
        opt = self._opt_bytes()
        if opt is None:
            self.crc_lbl.configure(text="Option bytes must be 8 hex digits, e.g. 00F000FF")
        elif self.image:
            self.crc_lbl.configure(text=f"Expected chip CRC 0x{chip_crc(self.image[2], opt.hex()):04X}")
        else:
            self.crc_lbl.configure(text="")
        if hasattr(self, "match_lbl"):
            self._update_match()

    def _build_settings_card(self, parent):
        card = self._card(parent, "Programmer settings")
        card.pack(fill="both", expand=True)
        self.settings_card = card
        b = card.body
        b.columnconfigure(1, weight=1)

        # Rows that only apply to some families: family -> widgets to grid / grid_remove.
        # `family` is one family key or a tuple of them.
        self.family_rows = {key: [] for key in FAMILIES}

        def owners(family):
            return (family,) if isinstance(family, str) else family or ()

        def label(row, text, family=None):
            lbl = ttk.Label(b, text=text, style="Card.TLabel")
            lbl.grid(row=row, column=0, sticky="w", padx=(0, 18), pady=3)
            for fam in owners(family):
                self.family_rows[fam].append(lbl)

        def cell(row, family=None):
            f = ttk.Frame(b, style="Card.TFrame")
            f.grid(row=row, column=1, sticky="w", pady=3)
            for fam in owners(family):
                self.family_rows[fam].append(f)
            return f

        def seg(row, text, var, options, family=None):
            label(row, text, family)
            f = cell(row, family)
            for value, opt in options:
                self._track(ttk.Radiobutton(f, text=opt, value=value, variable=var, width=17,
                                            style="Seg.Toolbutton")).pack(side="left")

        self.mode_var = tk.StringVar(value="button")
        self.erase_var = tk.StringVar(value="chip")
        seg(0, "Start flashing by", self.mode_var, [("button", "Button press"), ("auto", "Auto-detect board")])
        seg(1, "Erase", self.erase_var, [("chip", "Full chip"), ("pages", "Only image pages")], "mm32")
        seg(1, "Erase", self.erase_var, [("chip", "Full chip"), ("blocks", "Only image blocks")], "renesas")

        label(2, "After programming")
        checks = cell(2)
        self.verify_var = tk.BooleanVar(value=True)
        self.run_var = tk.BooleanVar(value=True)
        self.protect_var = tk.BooleanVar(value=False)
        self.verify_chk = self._track(ttk.Checkbutton(checks, text="Verify", variable=self.verify_var,
                                                      style="Card.TCheckbutton"))
        self.verify_chk.pack(side="left", padx=(0, 16))
        self._track(ttk.Checkbutton(checks, text="Run firmware", variable=self.run_var,
                                    style="Card.TCheckbutton")).pack(side="left", padx=(0, 16))
        self.protect_chk = self._track(ttk.Checkbutton(checks, text="Read-protect", variable=self.protect_var,
                                                       style="Card.TCheckbutton"))
        self.protect_chk.pack(side="left", padx=(0, 16))
        self.reset_var = tk.BooleanVar(value=True)
        self.reset_chk = self._track(ttk.Checkbutton(checks, text="Use RES pin", variable=self.reset_var,
                                                     style="Card.TCheckbutton"))

        def number(row, text, var, lo, hi, step, hint, family=None):
            label(row, text, family)
            f = cell(row, family)
            self._track(ttk.Spinbox(f, from_=lo, to=hi, increment=step, width=7, textvariable=var)).pack(
                side="left")
            ttk.Label(f, text=hint, style="Muted.TLabel").pack(side="left", padx=(10, 0))

        self.swdelay_var = tk.StringVar(value="0")
        self.speed_var = tk.StringVar(value="100")
        self.settle_var = tk.StringVar(value="500")
        number(3, "SWD clock delay", self.swdelay_var, 0, 250, 1, "0 = fastest; raise for long wires",
               ("mm32", "renesas"))
        number(3, "FICE timing", self.speed_var, 50, 1000, 10, "% of emulator timing; raise (e.g. 200) for long wires",
               "fortior")
        number(4, "Auto-detect settle", self.settle_var, 0, 10000, 100, "ms to wait after a board is detected")

        tk.Frame(b, bg=C["border"], height=1).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        foot = ttk.Frame(b, style="Card.TFrame")
        foot.grid(row=6, column=0, columnspan=2, sticky="ew")
        self.saved_lbl = tk.Label(foot, text="", bg=C["card"], fg=C["muted"], font=(FONT, 9, "bold"))
        self.saved_lbl.pack(side="left")
        self.save_btn = ttk.Button(foot, text="Save to programmer", style="Accent.TButton", command=self.save_settings)
        self.save_btn.pack(side="right")
        self.revert_btn = ttk.Button(foot, text="Undo changes", command=self.revert_settings)
        self.revert_btn.pack(side="right", padx=(0, 8))

        self._saved_settings = None
        self.setting_vars = {"mode": self.mode_var, "verify": self.verify_var, "run": self.run_var,
                             "settle": self.settle_var, "erase": self.erase_var, "protect": self.protect_var,
                             "swdelay": self.swdelay_var, "speed": self.speed_var, "reset": self.reset_var}
        for var in self.setting_vars.values():
            var.trace_add("write", lambda *a: self._update_settings_state())

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

        self.pass_lbl = tile(0, "PASS", C["ok_text"], C["ok_soft"])
        self.fail_lbl = tile(1, "FAIL", C["err_text"], C["err_soft"])
        self.total_lbl = tile(2, "TOTAL", C["text"], C["idle_soft"])
        self.yield_lbl = tile(3, "YIELD", C["accent_text"], C["accent_soft"])

    def _build_tools_card(self, parent):
        card = self._card(parent, "Target tools")
        card.pack(fill="both", expand=True)
        self.tools_body = card.body
        for c in range(3):
            self.tools_body.columnconfigure(c, weight=1)

    def _build_tool_buttons(self):
        for btn in self.tool_btns:
            self.conn_widgets.remove(btn)
            btn.destroy()
        if self.family == "mm32":
            items = [("Identify board", self.identify), ("Detect chip", self.detect),
                     ("Reset target", self.reset_target), ("Chip table…", self.chips_dialog),
                     ("Erase chip", self.erase_chip), ("Unlock (remove RDP)", self.unlock)]
        else:  # Fortior, Renesas
            items = [("Identify board", self.identify), ("Detect chip", self.detect),
                     ("Reset & run target", self.reset_target), ("Erase chip", self.erase_chip)]
        self.tool_btns = []
        for i, (txt, fn) in enumerate(items):
            btn = self._track(ttk.Button(self.tools_body, text=txt, command=fn))
            btn.grid(row=i // 3, column=i % 3, sticky="ew", padx=3, pady=3)
            self.tool_btns.append(btn)

    def _build_log(self, parent):
        card = self._card(parent, "Activity log")
        card.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        b = card.body
        self.cmdrow = cmdrow = ttk.Frame(b, style="Card.TFrame")
        cmdrow.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Label(cmdrow, text="Command", style="Muted.TLabel").pack(side="left")
        self.cmd_var = tk.StringVar()
        cmd_entry = self._track(ttk.Entry(cmdrow, textvariable=self.cmd_var, font=(MONO, 9)))
        cmd_entry.pack(side="left", fill="x", expand=True, padx=6)
        cmd_entry.bind("<Return>", lambda e: self.send_raw())
        self._track(ttk.Button(cmdrow, text="Send", command=self.send_raw)).pack(side="left")
        self.log = tk.Text(b, height=7, font=(MONO, 9), state="disabled", wrap="none", relief="flat",
                           bg=C["log_bg"], fg=C["text"], padx=8, pady=6, highlightthickness=0,
                           insertbackground=C["text"], selectbackground=C["accent"])
        sb = ttk.Scrollbar(b, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_configure("err", foreground=C["err_text"])
        self.log.tag_configure("ok", foreground=C["ok_text"])
        self.log.tag_configure("dim", foreground=C["muted"])
        self.log.tag_configure("time", foreground=C["log_time"])

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
        bg, fg = {"idle": (C["idle_soft"], C["text"]), "busy": (C["accent_soft"], C["accent_text"]),
                  "ok": (C["ok_soft"], C["ok_text"]), "err": (C["err_soft"], C["err_text"]),
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
            self.flash_hint.configure(text="No firmware on the programmer yet — load a model or upload a file.")
        elif (self._saved_settings or {}).get("mode") == "auto":
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
        for btn in self.family_btns:
            btn.state(["disabled"] if self.device_busy else ["!disabled"])
        self._update_upload_button()
        self._update_settings_state()

    # ================================================================ chip family
    def pick_family(self):
        """The user picked a family: switch the programmer (if connected), then the UI."""
        fam = self.family_var.get()
        if fam == self.family:
            return
        if not self.link.is_open:
            self._show_family(fam)
            return
        if self.busy or self.device_busy:
            self.family_var.set(self.family)
            return

        def work():
            try:
                self.link.request(f"SET family {fam}")
            except Exception:
                self.call(self.family_var.set, self.family)
                raise
            self.ui_q.put(("log", (f"Chip family set to {FAMILIES[fam]['name']}", "ok")))
            self._read_device()
        self.run_bg("Chip family", work)

    def _show_family(self, fam):
        """Show the widgets, settings and file of a family."""
        self.family = fam
        self.family_var.set(fam)
        self.prefs["family"] = fam
        save_prefs(self.prefs)
        self._update_header()
        mm32 = fam == "mm32"
        self.opt_row.pack_forget()
        self.crc_lbl.pack_forget()
        self.target_box.pack_forget()
        if mm32:
            self.target_box.pack(side="left")
            self.img_lbl.configure(text="Intel HEX (.hex) or binary (.bin) from Keil")
        elif fam == "fortior":
            self.opt_row.pack(fill="x", pady=(0, 8), before=self.upload_row)
            self.crc_lbl.pack(side="left")
            self.img_lbl.configure(text="Intel HEX (.hex) from Keil (C51)")
        else:
            self.img_lbl.configure(text="S-record (.srec / .mot) or Intel HEX (.hex) from e2 studio")
        # A row can belong to several families: show it if the active family owns it.
        shown = set(self.family_rows[fam])
        for widgets in self.family_rows.values():
            for w in widgets:
                if w in shown:
                    w.grid()
                else:
                    w.grid_remove()
        self.protect_chk.pack_forget()
        self.reset_chk.pack_forget()
        if mm32:
            self.protect_chk.pack(side="left", padx=(0, 16))
        elif fam == "renesas":
            self.reset_chk.pack(side="left", padx=(0, 16))
        self.verify_chk.configure(text="Verify (chip CRC)" if fam == "fortior" else "Verify")
        self._build_tool_buttons()

        # The selected file belongs to a family; load this family's last one.
        self.image = None
        self.path_var.set("No file selected")
        last = self.prefs["files"].get(fam)
        if last and os.path.exists(last):
            self._load_file(last, quiet=True)
        self._update_crc()
        self._update_match()
        self._update_enabled()

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
        if not ver.startswith("FLASHER "):
            self.call(self._connect_failed)
            raise LinkError(f"the programmer runs old firmware ({ver}). Upload the current firmware "
                            f"with PlatformIO first.")
        self.ui_q.put(("log", (f"Connected to {ver}", "ok")))
        self.call(self._set_connected, True)
        self._read_device()

    def _read_device(self):
        """(worker) Read INFO (and the MM32 chip table) and show it."""
        info = parse_kv(self.link.request("INFO"))
        chips = parse_chips(self.link.request("CHIPS")) if info.get("family") == "mm32" else None
        self.call(self._apply_device_info, info, chips)

    def _connect_failed(self):
        self.link.close()
        self._set_connected(False)

    def refresh_device(self):
        self.run_bg("Refresh", self._read_device, quiet=True)

    def _set_connected(self, on):
        self.conn_btn.state(["!disabled"])
        self.port_cb.state(["disabled"] if on else ["!disabled", "readonly"])
        self.baud_cb.state(["disabled"] if on else ["!disabled", "readonly"])
        self.refresh_btn.state(["disabled"] if on else ["!disabled"])
        if on:
            self.conn_btn.configure(text="Disconnect", style="TButton")
            self.status_pill.configure(text="●  Connected", bg=C["pill_on_bg"], fg=C["pill_on_fg"])
            self.hint.pack_forget()
            self._set_banner("Ready", "Programmer connected.", "idle")
        else:
            self.device_busy = False
            self.conn_btn.configure(text="Connect", style="Accent.TButton")
            self.status_pill.configure(text="●  Disconnected", bg=C["pill_off_bg"], fg=C["pill_off_fg"])
            if not self.hint.winfo_ismapped():
                self.hint.pack(fill="x", after=self.winfo_children()[0])
            self._set_banner("Not connected", "Connect the programmer to begin.", "off")
            self.dev_info = {}
            self._saved_settings = None
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

    def _apply_device_info(self, info, chips=None):
        fam = info.get("family", self.family)
        if fam in FAMILIES and fam != self.family:
            self._show_family(fam)  # the programmer decides; it may have been switched from the web UI
        self.dev_info = info
        if chips is not None:
            self.chips = chips
            names = ["ANY"] + [c["name"] for c in chips]
            self.target_cb["values"] = names
            if self.target_var.get() not in names:
                self.target_var.set("ANY")

        if info.get("img") == "1":
            name = info.get("name", "-")
            size = f"{int(info.get('size', 0)):,} bytes"
            if self.family == "mm32":
                what = f"{size} @ {info.get('base')}   ·   CRC {info.get('crc')}   ·   for {info.get('target')}"
                log = f"{size}, CRC {info.get('crc')}, for {info.get('target')}"
            elif self.family == "renesas":
                layout = describe_ra_layout({"cf": int(info.get("cf", 0)), "df": int(info.get("df", 0))})
                what = f"{size}   ·   {layout}   ·   CRC {info.get('crc')}"
                log = f"{size}, {layout}, CRC {info.get('crc')}"
            else:
                what = f"{size}   ·   options {info.get('opt')}   ·   chip CRC {info.get('chipcrc')}"
                log = f"{size}, options {info.get('opt')}, chip CRC {info.get('chipcrc')}"
            self.dev_name.configure(text=name if name != "-" else "(unnamed image)")
            self.dev_detail.configure(text=f"{what}   ·   fw {info.get('fw')}")
            self.write_log(f"Programmer holds {name} for {FAMILIES[self.family]['name']} ({log})")
        else:
            self.dev_name.configure(text="No firmware stored")
            self.dev_detail.configure(text=f"No {FAMILIES[self.family]['name']} image yet. Load a product model, "
                                           f"or a firmware file in engineer mode.")
        self._match_active_model()  # the image name tells which catalog model it is (Model.image_name)

        if "mode" in info:
            self._saved_settings = {k: info.get(k, SETTING_DEFAULTS[k]) for k in self._setting_keys()}
            self._show_settings(self._saved_settings)
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

    def _same_data(self):
        return bool(self.image) and self._stored_crc() == self.image[3]

    def _same_opt(self):
        """Fortior: the stored option bytes equal the ones entered. Always true for MM32."""
        if self.family != "fortior":
            return True
        opt = self._opt_bytes()
        return opt is not None and self.dev_info.get("opt", "").upper() == opt.hex().upper()

    def _update_match(self):
        if not self.link.is_open or not self.image or self._stored_crc() is None:
            self.match_lbl.configure(text="", bg=C["card"])
        elif self._same_data() and self._same_opt():
            self.match_lbl.configure(text="✓  Same as the file selected below", bg=C["ok_soft"], fg=C["ok_text"])
        elif self._same_data():
            self.match_lbl.configure(text="⚠  Same file, but different option bytes — press Apply options",
                                     bg=C["warn_soft"], fg=C["warn"])
        else:
            self.match_lbl.configure(text="⚠  Different from the file selected below — upload to replace it",
                                     bg=C["warn_soft"], fg=C["warn"])
        self._update_upload_button()

    def _update_upload_button(self):
        if self._same_data() and self._same_opt():
            self.upload_btn.configure(text="Upload again", style="TButton")
        elif self._same_data():
            self.upload_btn.configure(text="Apply options", style="Accent.TButton")
        else:
            self.upload_btn.configure(text="Upload to programmer", style="Accent.TButton")
        if not self.image or (self.family == "fortior" and self._opt_bytes() is None):
            self.upload_btn.state(["disabled"])
        elif self.link.is_open and not self.device_busy:
            self.upload_btn.state(["!disabled"])

    # ================================================================ image
    def browse(self):
        start = os.path.dirname(self.prefs.get("last_file", "")) or None
        path = filedialog.askopenfilename(
            title="Select firmware", initialdir=start,
            filetypes=[("Firmware", "*.hex *.ihx *.srec *.mot *.s19 *.s28 *.s37 *.bin"), ("All files", "*.*")])
        if path:
            self._load_file(path)

    def _load_file(self, path, quiet=False):
        try:
            base, data, layout = load_image(path, self.family)
        except Exception as e:
            if not quiet:
                messagebox.showerror(APP_TITLE, f"Cannot load image for {FAMILIES[self.family]['name']}:\n{e}")
            return
        crc = zlib.crc32(data)
        self.image = (path, base, data, crc)
        self.image_layout = layout
        self.path_var.set(os.path.basename(path))
        if self.family == "mm32":
            span = f"0x{base:08X} – 0x{base + len(data) - 1:08X}"
        elif self.family == "renesas":
            span = describe_ra_layout(layout)
        else:
            span = f"0x0000 – 0x{len(data) - 1:04X}"
        self.img_lbl.configure(text=f"{len(data):,} bytes   ·   {span}   ·   CRC 0x{crc:08X}   ·   "
                                    f"{os.path.dirname(path)}")
        self.prefs["files"][self.family] = path
        save_prefs(self.prefs)
        self.write_log(f"Selected {os.path.basename(path)} ({len(data):,} bytes, {span})")
        self._update_crc()
        self._update_match()
        self._update_enabled()

    def upload(self):
        if not self.image:
            messagebox.showwarning(APP_TITLE, "Select a firmware file first.")
            return
        if self.dev_info.get("family") != self.family:
            messagebox.showerror(APP_TITLE, "The programmer is set to another chip family. Press Refresh.")
            return
        path, base, data, crc = self.image
        if self.family == "mm32":
            target = self.target_var.get() or "ANY"
            chip = next((c for c in self.chips if c["name"] == target), None)
            if chip and base + len(data) > FLASH_BASE + chip["kb"] * 1024:
                messagebox.showerror(APP_TITLE,
                                     f"Image ({len(data):,} bytes) does not fit {target} ({chip['kb']} KB).")
                return
            self.prefs["target"] = target
            fields = f"0x{base:08X} {target}"
        elif self.family == "renesas":
            fields = f"{self.image_layout['cf']} {self.image_layout['df']}"
        else:
            opt = self._opt_bytes()
            if opt is None:
                messagebox.showerror(APP_TITLE, "Option bytes must be 8 hex digits, e.g. 00F000FF.")
                return
            fields = opt.hex().upper()
            self.prefs["opt"] = fields
            if self._same_data() and not self._same_opt():
                def work():
                    info = self.link.request(f"OPT {fields}")
                    self.ui_q.put(("log", (f"Option bytes set to {fields}", "ok")))
                    self.call(self._apply_device_info, parse_kv(info))
                save_prefs(self.prefs)
                self.run_bg("Apply options", work)
                return
        save_prefs(self.prefs)
        self.run_bg("Upload", self._upload_worker, path, data, fields)

    def _upload_worker(self, path, data, fields, name=None, chips=None):
        """fields: the family part of BEGIN (MM32 "<base> <target>", Fortior "<option bytes>",
        Renesas "<code flash bytes> <data flash bytes>"). name: stored image name (default: file name)."""
        name = name or os.path.basename(path).replace(" ", "_")[:40]
        crc = zlib.crc32(data)
        self.link.request(f"BEGIN {len(data)} 0x{crc:08X} {fields} {name}")
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
                               f"CRC 0x{crc:08X} verified ({fields})", "ok")))
        self.call(self._apply_device_info, parse_kv(info), self.chips if chips is None else chips)

    # ================================================================ settings
    def _setting_keys(self):
        return COMMON_SETTINGS + FAMILIES[self.family]["settings"]

    def _current_settings(self):
        """The active family's settings as shown, in the programmer's text form."""
        out = {}
        for key in self._setting_keys():
            var = self.setting_vars[key]
            if isinstance(var, tk.BooleanVar):
                out[key] = str(int(var.get()))
            elif key in ("mode", "erase"):
                out[key] = var.get()
            else:
                try:
                    out[key] = str(int(var.get()))
                except ValueError:
                    out[key] = SETTING_DEFAULTS[key]
        return out

    def _show_settings(self, st):
        self.suppress_set = True
        try:
            for key, value in st.items():
                var = self.setting_vars[key]
                var.set(value == "1" if isinstance(var, tk.BooleanVar) else value)
        finally:
            self.suppress_set = False
        self._update_settings_state()

    def _changed_settings(self):
        if not self._saved_settings:
            return {}
        cur = self._current_settings()
        return {k: v for k, v in cur.items() if self._saved_settings.get(k) != v}

    def _update_settings_state(self):
        if self.suppress_set:
            return
        self._update_flash_hint()
        changed = self._changed_settings()
        ready = self.link.is_open and not self.device_busy and self._saved_settings is not None
        if not ready:
            self.saved_lbl.configure(text="Connect to view and change the programmer settings.", fg=C["muted"])
        elif changed:
            n = len(changed)
            self.saved_lbl.configure(text=f"●  {n} unsaved change{'s' if n > 1 else ''}", fg=C["warn"])
        else:
            self.saved_lbl.configure(text="✓  Saved on the programmer", fg=C["ok_text"])
        for btn in (self.save_btn, self.revert_btn):
            btn.state(["!disabled"] if ready and changed else ["disabled"])

    def save_settings(self):
        changed = self._changed_settings()
        if not changed:
            return

        def work():
            for key, value in changed.items():
                self.link.request(f"SET {key} {value}")
            info = parse_kv(self.link.request("INFO"))
            self.ui_q.put(("log", ("Settings saved: " + ", ".join(f"{k}={v}" for k, v in changed.items()), "ok")))
            self.call(self._apply_device_info, info, self.chips)

        self.run_bg("Save settings", work)

    def revert_settings(self):
        if self._saved_settings:
            self._show_settings(self._saved_settings)

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
            if "part" in info:  # Renesas
                known = info.get("part") != "UNKNOWN"
                msg = (f"{info.get('part')}  ·  {info.get('flash')} KB code flash  ·  UID {info.get('uid')}  ·  "
                       f"SWD delay {info.get('swdelay')}  ·  via RES: {'yes' if info.get('reset') == '1' else 'no'}")
                self.ui_q.put(("log", (f"Detected {msg}  (access window {info.get('aws')}-{info.get('awe')})",
                                       "ok" if known else "err")))
                self.call(self._set_banner, "Chip detected" if known else "Unknown part number", msg,
                          "ok" if known else "err")
                return
            if "devid" not in info:  # Fortior
                msg = f"{info.get('chip')}  ·  ID {info.get('id')}  ·  FICE timing {info.get('speed')}%"
                self.ui_q.put(("log", (f"Detected {msg}", "ok")))
                self.call(self._set_banner, "Chip detected", msg, "ok")
                return
            self.last_detect = info
            msg = (f"{info.get('chip')}  ·  DEV_ID {info.get('devid')}  ·  UID {info.get('uid', '-')}  ·  "
                   f"{info.get('flash')} KB flash  ·  "
                   f"read-protected: {'yes' if info.get('protected') == '1' else 'no'}  ·  "
                   f"SWD delay {info.get('swdelay')}")
            self.ui_q.put(("log", (f"Detected {msg}", "ok")))
            kind = "err" if info.get("chip") == "UNKNOWN" else "ok"
            self.call(self._set_banner, "Chip detected" if kind == "ok" else "Unknown chip", msg, kind)
            if kind == "err":
                self.call(self._ask_identify, info.get("devid", ""))

        self.run_bg("Detect", work)

    # ---- identify: which firmware is on this board? (reads only, nothing is written)
    def _signature(self, m):
        """What the chip reports for model m: ("crc32", base, length, crc) or ("chipcrc", crc16). Cached."""
        cache = self.__dict__.setdefault("_sig_cache", {})
        key = (m.file, m.file_crc, m.settings)
        if key not in cache:
            base, data, layout = load_image(cat.firmware_path(self.catalog_dir, m), m.family)
            if m.family == "fortior":
                cache[key] = ("chipcrc", chip_crc(data, m.settings_dict().get("opt", OPT_DEFAULT)))
            elif m.family == "renesas":  # code flash only: the firmware may rewrite its data flash
                cache[key] = ("crc32", 0, layout["cf"], zlib.crc32(data[:layout["cf"]])) if layout["cf"] else None
            else:
                cache[key] = ("crc32", base, len(data), zlib.crc32(data))
        return cache[key]

    def _ident_request(self, args):
        try:
            return parse_kv(self.link.request(f"IDENT {args}".strip(), timeout=30))
        except LinkError as e:
            if "unknown command" in str(e):
                raise LinkError("the programmer firmware is too old for Identify. Upload firmware 2.2.0 or "
                                "newer with PlatformIO.") from None
            raise

    def _flash_history(self, uid):
        """flash_log*.csv rows of this chip ID, oldest first."""
        rows = []
        if not uid or uid == "-":
            return rows
        for path in glob.glob(os.path.join(os.path.dirname(LOG_CSV), "flash_log*.csv")):
            try:
                with open(path, newline="") as f:
                    rows += [r for r in csv.DictReader(f) if (r.get("chip_id") or "").upper() == uid.upper()]
            except OSError:
                pass
        return sorted(rows, key=lambda r: r.get("timestamp", ""))

    def identify(self):
        fam = self.family

        def work():
            self.call(self._set_banner, "Identifying board…", "Reading the chip. Nothing is written.", "busy")
            sigs, broken = [], []
            for m in self.models:
                if m.family == fam:
                    try:
                        sig = self._signature(m)
                        if sig:
                            sigs.append((m, sig))
                    except Exception:  # noqa: BLE001 - a missing / changed file must not stop the check
                        broken.append(m.model)
            info, crcs = {}, {}
            if fam == "fortior":
                info = self._ident_request("")
                chip = int(info.get("chipcrc", "-1"), 16)
                matches = [m for m, s in sigs if s[1] == chip]
            else:
                by_base = {}
                for m, s in sigs:
                    by_base.setdefault(s[1], set()).add(s[2])
                if not by_base:
                    by_base = {FLASH_BASE if fam == "mm32" else 0: set()}
                for base, lens in by_base.items():
                    lens = sorted(lens)
                    for i in range(0, max(len(lens), 1), 60):  # keep each command line short
                        part = self._ident_request(f"0x{base:08X} " + " ".join(map(str, lens[i:i + 60])))
                        info = info or part
                        for item in filter(None, part.get("crcs", "").split(",")):
                            n, crc = item.split(":")
                            crcs[(base, int(n))] = int(crc, 16)
                matches = [m for m, s in sigs if crcs.get((s[1], s[2])) == s[3]]
            history = self._flash_history(info.get("uid"))
            self.call(self._show_identity, fam, info, matches, len(sigs), broken, history)

        self.run_bg("Identify", work)

    def _show_identity(self, fam, info, matches, checked, broken, history):
        name = FAMILIES[fam]["name"]
        uid = info.get("uid", "-")
        chip = info.get("chip", name)
        lines = [f"{chip}  ·  UID {uid}" if uid != "-" else f"{chip}  ·  no unique ID"]
        if matches:
            names = ", ".join(f"{m.model} v{m.version}" + (f" ({m.client})" if m.client else "") for m in matches)
            title, kind = f"✓  {matches[0].model}  v{matches[0].version}", "ok"
            lines.append(f"Firmware on the chip: {names}" + ("   (same image)" if len(matches) > 1 else ""))
        elif info.get("protected") == "1":
            title, kind = "Read-protected chip", "idle"
            lines.append("The flash cannot be read back; only the flash log can tell what was programmed.")
        else:
            title, kind = "No catalog firmware matches", "err"
            lines.append(f"The flash matches none of the {checked} {name} models in the catalog "
                         f"(blank chip, another version, or firmware from elsewhere).")
        if history:
            last = history[-1]
            what = f"{last.get('model')} v{last.get('version')}" if last.get("model") else last.get("image", "?")
            lines.append(f"Flash log: {len(history)} entr{'y' if len(history) == 1 else 'ies'}, last "
                         f"{last.get('timestamp', '').replace('T', ' ')}  {last.get('result')}  {what}")
        elif uid != "-":
            lines.append("Flash log: this chip was never flashed at this station.")
        if broken:
            lines.append(f"Not checked (file missing or changed): {', '.join(broken[:5])}"
                         + (" …" if len(broken) > 5 else ""))
        if not matches and history and info.get("protected") == "1":
            title = f"Read-protected  ·  log says {what}"
        self._set_banner(title, "\n".join(lines), kind)
        for i, line in enumerate([title] + lines):
            self.write_log(("Identify: " if i == 0 else "    ") + line, "ok" if kind == "ok" and i < 2 else None)

    def reset_target(self):
        def work():
            self.link.request("RESET", timeout=10)
            self.ui_q.put(("log", ("Target reset" if self.family == "mm32" else
                                   "Target reset and released — the firmware runs", "ok")))  # Fortior, Renesas
        self.run_bg("Reset", work)

    def erase_chip(self):
        question = {"mm32": "Mass-erase the connected target?",
                    "renesas": "Erase the whole code flash and data flash of the connected target?"}.get(
            self.family, "Erase the whole flash of the connected target?")
        if messagebox.askyesno(APP_TITLE, question):
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
        """Send a raw protocol command (e.g. 'READ 0x08000000 16', 'FICE A P D15 S3 R X') and log the reply."""
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

    def _update_header(self):
        f = FAMILIES[self.family]
        self.header_sub.configure(text=f"{f['name']}  ·  {f['iface']} production flasher  ·  ESP8266")

    # ================================================================ product models (catalog)
    def _load_catalog(self):
        try:
            self.models = cat.load(self.catalog_dir)
        except Exception as e:  # noqa: BLE001
            self.models = []
            self.after(300, self.write_log, f"Cannot read the firmware catalog in {self.catalog_dir}: {e}", "err")

    def _fill_model_list(self, models=None):
        shown = sorted(models or self.models, key=cat.sort_key)
        self.model_labels = {m.label(): m for m in shown}  # drop-down text -> model
        self.model_cb["values"] = list(self.model_labels)

    def _filter_models(self, event):
        """Narrow the drop-down to the models matching what was typed (code or description)."""
        if event.keysym in ("Return", "Up", "Down", "Escape", "Tab"):
            return
        text = self.model_var.get().strip().lower()
        key = cat.normalize(text)
        self._fill_model_list([m for m in self.models if key in m.key or text in m.description.lower()
                               or text == m.client.lower()]
                              if text else None)

    def _match_active_model(self):
        info = self.dev_info
        self.active_model = next((m for m in self.models if info.get("img") == "1" and m.family == self.family
                                  and m.image_name() == info.get("name")), None)
        self._show_active_model()

    def _show_active_model(self):
        if not hasattr(self, "model_name"):
            return
        m = self.active_model
        if m:
            self.model_name.configure(text=f"{m.model}   v{m.version}" if m.version else m.model)
            parts = [f"client {m.client}" if m.client else "", m.description, FAMILIES[m.family]["name"]]
            if m.chip and m.chip != "ANY":
                parts.append(m.chip)
            parts.append(m.settings or "default settings")
            self.model_detail.configure(text="   ·   ".join(p for p in parts if p))
        elif not self.models:
            self.model_name.configure(text="No firmware catalog yet")
            self.model_detail.configure(text="Catalog… → Add current setup or Bulk import, or import a catalog "
                                             ".zip from another PC.")
        elif self.link.is_open and self.dev_info.get("img") == "1":
            self.model_name.configure(text="Scan or type a model code")
            self.model_detail.configure(text=f"The programmer holds {self.dev_info.get('name')}, which is not a "
                                             f"catalog model.")
        else:
            self.model_name.configure(text="Scan or type a model code")
            self.model_detail.configure(text=f"{len(self.models)} models in the catalog  ·  {self.catalog_dir}")
        if hasattr(self, "flash_hint"):
            self._update_flash_hint()

    def select_model(self, code=None):
        """Load a product model from the catalog onto the programmer (barcode scan / Enter / pick)."""
        raw = code if code is not None else self.model_var.get()
        picked = getattr(self, "model_labels", {}).get(raw)  # chosen from the drop-down
        text = picked.model if picked else raw.strip()
        if not text:
            self.model_cb.focus_set()
            return
        m = picked or cat.find(self.models, text)
        if not m:
            self._set_banner("Unknown model", f"'{text}' is not in the firmware catalog "
                                              f"({len(self.models)} models).", "err")
            self.write_log(f"Unknown model code '{text}'", "err")
            self.bell()
            self.model_cb.select_range(0, "end")
            return
        if not self.link.is_open:
            messagebox.showwarning(APP_TITLE, "Connect to the programmer first.")
            return
        if self.busy or self.device_busy:
            messagebox.showinfo(APP_TITLE, "Wait for the current operation to finish.")
            return
        try:
            path = cat.firmware_path(self.catalog_dir, m)
            base, data, layout = load_image(path, m.family)
        except Exception as e:  # noqa: BLE001
            self._set_banner("Cannot load model", str(e), "err")
            self.write_log(f"{m.model}: {e}", "err")
            self.bell()
            return
        self.model_var.set("")
        self._fill_model_list()
        self._set_banner(f"Loading {m.model}…", m.description, "busy")
        self.run_bg(f"Load {m.model}", self._model_worker, m, path, base, data, layout)

    def _model_worker(self, m, path, base, data, layout):
        try:
            self._load_model(m, path, base, data, layout)
        except Exception as e:
            self.call(self._set_banner, f"✕  {m.model} not loaded", str(e), "err")
            self.call(self.refresh_device)
            raise

    def _load_model(self, m, path, base, data, layout):
        """(worker) Switch family, upload the image unless already stored, apply the model's settings."""
        if self.dev_info.get("family") != m.family:
            self.link.request(f"SET family {m.family}")
        info = parse_kv(self.link.request("INFO"))
        chips = parse_chips(self.link.request("CHIPS")) if m.family == "mm32" else None
        st = m.settings_dict()
        if m.family == "mm32":
            target = m.chip or "ANY"
            if target != "ANY":
                chip = next((c for c in chips if c["name"] == target), None)
                if not chip:
                    raise LinkError(f"the programmer's chip table has no {target} (model {m.model}).")
                if base + len(data) > FLASH_BASE + chip["kb"] * 1024:
                    raise LinkError(f"the image ({len(data):,} bytes) does not fit {target} ({chip['kb']} KB).")
            fields = f"0x{base:08X} {target}"
        elif m.family == "renesas":
            fields = f"{layout['cf']} {layout['df']}"
        else:
            fields = st.get("opt", OPT_DEFAULT).upper()
        name = m.image_name()
        try:
            stored_crc = int(info.get("crc", ""), 16)
        except ValueError:
            stored_crc = None
        stored = (info.get("img") == "1" and info.get("name") == name and stored_crc == zlib.crc32(data)
                  and (m.family != "mm32" or info.get("target") == fields.split()[1]))
        if not stored:
            self._upload_worker(path, data, fields, name=name, chips=chips)
        elif m.family == "fortior" and info.get("opt", "").upper() != fields:
            self.link.request(f"OPT {fields}")
        changed = []
        for key, value in st.items():
            if key != "opt" and info.get(key) != value:
                self.link.request(f"SET {key} {value}")
                changed.append(f"{key}={value}")
        if changed:
            self.ui_q.put(("log", (f"{m.model} settings applied: {', '.join(changed)}", "ok")))
        info = parse_kv(self.link.request("INFO"))
        self.call(self._apply_device_info, info, chips)
        self.call(self._model_ready, m, path, not stored)

    def _model_ready(self, m, path, uploaded):
        if m.family == "mm32":
            self.target_var.set(m.chip or "ANY")
        elif m.family == "fortior":
            self.opt_var.set(m.settings_dict().get("opt", OPT_DEFAULT).upper())
        self._load_file(path, quiet=True)
        self.active_model = m
        self.prefs["model"] = m.model
        save_prefs(self.prefs)
        self._show_active_model()
        how = "Firmware loaded onto the programmer" if uploaded else "Already on the programmer"
        self._set_banner(f"✓  {m.model} ready", f"{how}.  Place a board and press FLASH.", "ok")
        self.write_log(f"Model {m.model} v{m.version} ready ({m.description})", "ok")
        self.model_cb.focus_set()

    def _model_form(self, parent, title, summary, code="", client="", description="", version=""):
        """Ask for model code, client prefix, description and version. Returns a tuple, or None if cancelled."""
        win = tk.Toplevel(parent)
        win.title(title)
        win.configure(bg=C["card"])
        win.transient(parent)
        win.resizable(False, False)
        body = ttk.Frame(win, style="Card.TFrame", padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body, text=summary, style="Muted.TLabel", justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 12))
        vars_ = []
        for i, (lbl, value, hint) in enumerate((("Model code", code, "as printed on the product label / barcode"),
                                                ("Client prefix", client, "optional, e.g. wt"),
                                                ("Description", description, "e.g. Ceiling fan, teak, white"),
                                                ("Firmware version", version, "e.g. 1.1.3"))):
            ttk.Label(body, text=lbl, style="Card.TLabel").grid(row=2 + 2 * i, column=0, sticky="w", padx=(0, 12))
            var = tk.StringVar(value=value)
            entry = ttk.Entry(body, textvariable=var, width=42)
            entry.grid(row=2 + 2 * i, column=1, sticky="w", pady=(4, 0))
            ttk.Label(body, text=hint, style="Muted.TLabel").grid(row=3 + 2 * i, column=1, sticky="w")
            vars_.append(var)
            if i == 0:
                entry.focus_set()
        result = []

        def ok():
            code_, client_, desc, ver = (v.get().strip() for v in vars_)
            if not code_ or not ver:
                messagebox.showerror(APP_TITLE, "Enter the model code and the firmware version.", parent=win)
                return
            if "_" in client_ or " " in client_:
                messagebox.showerror(APP_TITLE, "The client prefix cannot contain '_' or spaces.", parent=win)
                return
            result.append((code_, client_, desc, ver))
            win.destroy()

        btns = ttk.Frame(body, style="Card.TFrame")
        btns.grid(row=10, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Save model", style="Accent.TButton", command=ok).pack(side="right", padx=6)
        win.bind("<Return>", lambda e: ok())
        win.grab_set()
        self.wait_window(win)
        return result[0] if result else None

    def _bulk_plan(self, paths, fam, chip, settings):
        """Check bulk import files. Returns rows (file, client, model, version, result, kind) and the
        (Model, path) to add. kind: new / update / same / skip / error."""
        rows, best, clash = [], {}, set()
        limit = None
        if fam == "mm32" and chip != "ANY":
            c = next((c for c in self.chips if c["name"] == chip), None)
            limit = FLASH_BASE + c["kb"] * 1024 if c else None
        for path in paths:
            fname = os.path.basename(path)
            parsed = cat.parse_filename(path)
            if not parsed:
                rows.append([fname, "", "", "", "name is not <client>_<model>_v<version>", "error"])
                continue
            client, model, version = parsed
            try:
                base, data, _ = load_image(path, fam)
                if limit and base + len(data) > limit:
                    raise ValueError(f"{len(data):,} bytes do not fit {chip}")
            except Exception as e:  # noqa: BLE001
                rows.append([fname, client, model, version, f"not a valid {FAMILIES[fam]['name']} image: {e}",
                             "error"])
                continue
            row = [fname, client, model, version, "", "new", path]
            rows.append(row)
            key = cat.normalize(model)
            if key in best and best[key][1].lower() != client.lower():
                # Two clients with one model code: a scan could not tell them apart, so import neither.
                row[4], row[5] = f"model code also selected for client {best[key][1]}", "error"
                clash.add(key)
                continue
            if key in best:  # several versions of one model: the newest wins
                old = best[key]
                newer, older = (row, old) if cat.version_key(version) > cat.version_key(old[3]) else (old, row)
                older[4], older[5] = f"skipped: v{newer[3]} of this model is also selected", "skip"
                best[key] = newer
            else:
                best[key] = row
        items = []
        for key, row in best.items():
            fname, client, model, version, _, _, path = row
            if key in clash:
                row[4], row[5] = "model code also selected for another client", "error"
                continue
            old = cat.find(self.models, model)
            crc = f"{cat.file_crc(path):08X}"
            if old and old.client and old.client.lower() != client.lower():
                row[4], row[5] = f"model code already used by client {old.client} (v{old.version})", "error"
                continue
            if old and cat.same_entry(old, client, fam, chip, version, settings, crc):
                row[4], row[5] = "unchanged (already in the catalog)", "same"
                continue
            if old and cat.version_key(version) < cat.version_key(old.version):
                row[4], row[5] = f"skipped: the catalog has newer v{old.version}", "skip"
                continue
            if old:
                change = f"v{old.version} → v{version}" if old.version != version else "same version, new file/settings"
                if old.family != fam:
                    change += f" (was {old.family})"
                row[4], row[5] = f"update: {change}", "update"
            else:
                row[4] = "new model"
            items.append((cat.Model(model=model, client=client, description=old.description if old else "",
                                    family=fam, chip=chip, version=version, settings=settings), path))
        return [r[:6] for r in rows], items

    def _bulk_preview(self, parent, paths, fam, chip, settings, summary, on_done):
        rows, items = self._bulk_plan(paths, fam, chip, settings)
        win = tk.Toplevel(parent)
        win.title("Bulk import")
        win.configure(bg=C["card"])
        win.transient(parent)
        win.geometry("900x520")
        body = ttk.Frame(win, style="Card.TFrame", padding=14)
        body.pack(fill="both", expand=True)
        counts = {k: sum(1 for r in rows if r[5] == k) for k in ("new", "update", "same", "skip", "error")}
        ttk.Label(body, text=f"{len(paths)} files   ·   {counts['new']} new   ·   {counts['update']} updated   ·   "
                             f"{counts['same']} unchanged   ·   {counts['skip']} skipped   ·   {counts['error']} with "
                             f"problems", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text=summary, style="Muted.TLabel", justify="left").pack(anchor="w", pady=(4, 8))
        frame = ttk.Frame(body, style="Card.TFrame")
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=("file", "client", "model", "version", "result"), show="headings")
        for col, txt, w in (("file", "File", 240), ("client", "Client", 60), ("model", "Model code", 140),
                            ("version", "Version", 70), ("result", "Result", 360)):
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="w")
        tree.tag_configure("error", foreground=C["err_text"])
        tree.tag_configure("skip", foreground=C["warn"])
        tree.tag_configure("same", foreground=C["muted"])
        tree.tag_configure("new", foreground=C["ok_text"])
        tree.tag_configure("update", foreground=C["accent_text"])
        order = {"error": 0, "skip": 1, "new": 2, "update": 3, "same": 4}
        for r in sorted(rows, key=lambda r: (order[r[5]], r[1].upper(), r[2].upper())):
            tree.insert("", "end", values=r[:5], tags=(r[5],))
        sb = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def do_import():
            try:
                self.models = cat.add_many(self.catalog_dir, self.models, items)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror(APP_TITLE, f"Import failed: {e}", parent=win)
                return
            self.write_log(f"Catalog bulk import: {counts['new']} new, {counts['update']} updated "
                           f"({FAMILIES[fam]['name']}{', ' + chip if chip else ''}; {settings})", "ok")
            win.destroy()
            on_done()

        btns = ttk.Frame(body, style="Card.TFrame")
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        imp = ttk.Button(btns, text=f"Import {len(items)} models", style="Accent.TButton", command=do_import)
        imp.pack(side="right", padx=6)
        if not items:
            imp.state(["disabled"])
        win.grab_set()

    def catalog_dialog(self):
        """Manage the firmware catalog (add, bulk import, delete, import / export, folder)."""
        win = tk.Toplevel(self)
        win.title("Firmware catalog")
        win.configure(bg=C["card"])
        win.transient(self)
        win.geometry("1180x540")
        body = ttk.Frame(win, style="Card.TFrame", padding=14)
        body.pack(fill="both", expand=True)

        top = ttk.Frame(body, style="Card.TFrame")
        top.pack(fill="x")
        folder_lbl = ttk.Label(top, text="", style="Muted.TLabel")
        folder_lbl.pack(side="left")

        cols = (("model", "Model code", 150), ("client", "Client", 60), ("description", "Description", 200),
                ("family", "Family", 80),
                ("chip", "Chip", 100), ("version", "Version", 70), ("settings", "Settings", 190),
                ("updated", "Updated", 90))
        frame = ttk.Frame(body, style="Card.TFrame")
        frame.pack(fill="both", expand=True, pady=10)
        tree = ttk.Treeview(frame, columns=[c for c, _, _ in cols], show="headings")
        for col, txt, w in cols:
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def fill():
            tree.delete(*tree.get_children())
            for m in sorted(self.models, key=cat.sort_key):
                tree.insert("", "end", iid=m.key, values=(m.model, m.client or "—", m.description, m.family,
                                                          m.chip or "—",
                                                          m.version, m.settings, m.updated))
            folder_lbl.configure(text=f"{len(self.models)} models   ·   {self.catalog_dir}")
            self._fill_model_list()
            self._match_active_model()

        def selected():
            sel = tree.selection()
            return cat.find(self.models, sel[0]) if sel else None

        def guarded(fn):
            def run():
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    messagebox.showerror(APP_TITLE, str(e), parent=win)
            return run

        def current_setup():
            """(family, chip, settings text, summary) from the main window, or None if invalid."""
            fam = self.family
            st = {k: v for k, v in self._current_settings().items() if k in cat.MODEL_SETTINGS[fam]}
            if fam == "fortior":
                opt = self._opt_bytes()
                if opt is None:
                    messagebox.showerror(APP_TITLE, "Option bytes must be 8 hex digits.", parent=win)
                    return None
                st["opt"] = opt.hex().upper()
            chip = (self.target_var.get() or "ANY") if fam == "mm32" else ""
            settings = cat.format_settings(st)
            summary = (f"Family:  {FAMILIES[fam]['name']}" + (f"   ·   For chip: {chip}" if chip else "") +
                       f"\nSettings:  {settings}")
            return fam, chip, settings, summary

        def add_current():
            if not self.image:
                messagebox.showwarning(APP_TITLE, "First set up the model in the main window: choose the chip family, "
                                                  "load its firmware file and set the programmer settings (and "
                                                  "'For chip' / option bytes). Then add it here.", parent=win)
                return
            setup = current_setup()
            if not setup:
                return
            fam, chip, settings, summary = setup
            summary += f"\nFile:  {os.path.basename(self.image[0])}"
            # Pre-fill from a file named <client>_<model>_v<version>, else from the model on the programmer.
            parsed = cat.parse_filename(self.image[0])
            am = self.active_model if self.active_model and self.active_model.family == fam else None
            if parsed:
                client, code, ver = parsed
                old = cat.find(self.models, code)
                prefill = (code, client, old.description if old else "", ver)
            else:
                prefill = (am.model, am.client, am.description, am.version) if am else ()
            form = self._model_form(win, "Add model to catalog", summary, *prefill)
            if not form:
                return
            code, client, desc, ver = form
            old = cat.find(self.models, code)
            if old and not messagebox.askyesno(
                    APP_TITLE, f"{old.model} is already in the catalog (v{old.version}, {old.family}).\n\n"
                               f"Replace it with this setup (v{ver})?", parent=win):
                return
            m = cat.Model(model=code, client=client, description=desc, family=fam, chip=chip, version=ver,
                          settings=settings)
            self.models = cat.add(self.catalog_dir, self.models, m, self.image[0])
            self.write_log(f"Catalog: {'updated' if old else 'added'} {code} v{ver} "
                           f"({os.path.basename(self.image[0])})", "ok")
            fill()
            tree.selection_set(m.key)
            tree.see(m.key)

        def bulk_import():
            setup = current_setup()
            if not setup:
                return
            fam, chip, settings, summary = setup
            if not messagebox.askokcancel(
                    APP_TITLE, "Bulk import adds many models that use the SAME chip and settings:\n\n"
                               f"{summary}\n\nThese are taken from the main window. If they are not right, press "
                               "Cancel and set them there first.\n\nNext, select the firmware files. They must be "
                               "named <client>_<model>_v<version>, e.g. wt_cf36_10001_v1.1.3.hex.", parent=win):
                return
            paths = filedialog.askopenfilenames(
                parent=win, title=f"Firmware files for {FAMILIES[fam]['name']}" + (f" ({chip})" if chip else ""),
                filetypes=[("Firmware", "*.hex *.ihx *.srec *.mot *.s19 *.s28 *.s37 *.bin"), ("All files", "*.*")])
            if paths:
                self._bulk_preview(win, list(paths), fam, chip, settings, summary, fill)

        def delete():
            m = selected()
            if m and messagebox.askyesno(APP_TITLE, f"Delete {m.model} v{m.version} from the catalog?", parent=win):
                self.models = cat.remove(self.catalog_dir, self.models, m)
                self.write_log(f"Catalog: deleted {m.model}", "ok")
                fill()

        def load_selected():
            m = selected()
            if m:
                self.select_model(m.model)

        def do_export():
            if not self.models:
                messagebox.showinfo(APP_TITLE, "The catalog is empty.", parent=win)
                return
            path = filedialog.asksaveasfilename(parent=win, title="Export firmware catalog",
                                                defaultextension=".zip", filetypes=[("Catalog", "*.zip")],
                                                initialfile=f"firmware-catalog-{dt.date.today():%Y-%m-%d}.zip")
            if path:
                n = cat.export_zip(self.catalog_dir, self.models, path)
                self.write_log(f"Catalog exported: {n} models -> {path}", "ok")
                messagebox.showinfo(APP_TITLE, f"Exported {n} models to\n{path}\n\nCopy this file to the other PC "
                                               f"and use Import there.", parent=win)

        def do_import():
            path = filedialog.askopenfilename(parent=win, title="Import firmware catalog",
                                              filetypes=[("Catalog", "*.zip")])
            if not path:
                return
            new, updated, same = cat.compare(self.models, cat.read_zip(path))
            if not new and not updated:
                messagebox.showinfo(APP_TITLE, f"Nothing new: all {len(same)} models are already in this catalog.",
                                    parent=win)
                return
            lines = [f"{len(new)} new models"]
            if updated:
                lines.append(f"{len(updated)} models replaced: " + ", ".join(
                    f"{m.model} v{cat.find(self.models, m.model).version} → v{m.version}" for m in updated[:8]) +
                    (" …" if len(updated) > 8 else ""))
            lines.append(f"{len(same)} unchanged")
            if messagebox.askyesno(APP_TITLE, "Import this catalog?\n\n" + "\n".join(lines), parent=win):
                self.models = cat.import_zip(self.catalog_dir, self.models, path)
                self.write_log(f"Catalog imported from {os.path.basename(path)}: {len(new)} new, "
                               f"{len(updated)} replaced", "ok")
                fill()

        def open_folder():
            os.makedirs(self.catalog_dir, exist_ok=True)
            os.startfile(self.catalog_dir)

        def change_folder():
            path = filedialog.askdirectory(parent=win, title="Firmware catalog folder", initialdir=self.catalog_dir)
            if not path:
                return
            self.models = cat.load(path)
            self.catalog_dir = os.path.normpath(path)
            self.prefs["catalog_dir"] = self.catalog_dir
            save_prefs(self.prefs)
            self.write_log(f"Catalog folder: {self.catalog_dir} ({len(self.models)} models)", "ok")
            fill()

        btns = ttk.Frame(body, style="Card.TFrame")
        btns.pack(fill="x")
        ttk.Button(btns, text="Add current setup…", style="Accent.TButton",
                   command=guarded(add_current)).pack(side="left")
        ttk.Button(btns, text="Bulk import…", command=guarded(bulk_import)).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Load selected", command=guarded(load_selected)).pack(side="left", padx=6)
        ttk.Button(btns, text="Delete", command=guarded(delete)).pack(side="left")
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Change folder…", command=guarded(change_folder)).pack(side="right", padx=(0, 6))
        ttk.Button(btns, text="Open folder", command=guarded(open_folder)).pack(side="right", padx=6)
        ttk.Button(btns, text="Export .zip…", command=guarded(do_export)).pack(side="right")
        ttk.Button(btns, text="Import .zip…", command=guarded(do_import)).pack(side="right", padx=6)
        tree.bind("<Double-1>", lambda e: guarded(load_selected)())
        fill()

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
            text = "Verifying (CRC)" if st == "verify" and self.family == "fortior" else self.STATE_TEXT.get(st, st)
            self._set_banner(text + "…", "", "busy")
        elif ev == "PROGRESS":
            self.prog["value"] = int(parts[3])
            self.result_lbl.configure(text=f"{parts[2].capitalize()}  {parts[3]}%")
        elif ev == "CHIP":
            self.write_log(f"Target: {parts[2]} ({parts[3]})")
        elif ev == "TARGET":
            if parts[2] == "present":
                self.write_log("Board detected (auto mode)", "dim")
                settle = (self._saved_settings or {}).get("settle", "500")
                self._set_banner("Board detected", f"Flashing starts in {settle} ms…", "busy")
            else:
                self.write_log("Board removed — waiting for the next one", "dim")
                self._set_banner("Waiting for board", "Place the next board on the fixture.", "idle")
        elif ev == "RESULT":
            self._on_result(line)
        elif ev == "FAMILY":
            self.write_log(f"Chip family is now {FAMILIES.get(parts[2], {}).get('name', parts[2])}", "dim")
            if self.link.is_open:
                self.refresh_device()
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
        am = self.active_model
        model = [am.model, am.version] if am else ["", ""]
        if parts[2] == "PASS":
            ms, chip, devid = parts[3], parts[4], parts[5]
            self.prog["value"] = 100
            msg = f"{chip} programmed and verified in {int(ms) / 1000:.2f} s"
            self._set_banner("✓  PASS", msg, "ok")
            self.write_log(f"PASS  {msg}", "ok")
            self._csv(["PASS", self.family, chip, devid, ms, "", img] + model)
        else:
            counted = parts[3] == "1"
            reason = " ".join(parts[4:])
            self._set_banner("✕  FAIL", reason + ("" if counted else "   (not counted)"), "err")
            self.write_log(f"FAIL  {reason}" + ("" if counted else "  (not counted)"), "err")
            self._csv(["FAIL" if counted else "NO-TARGET", self.family, "", "", "", reason, img] + model)
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
        header = ["timestamp", "result", "family", "chip", "chip_id", "ms", "message", "image", "model", "version"]
        try:
            if os.path.exists(LOG_CSV):
                with open(LOG_CSV, "r", newline="") as f:
                    old = next(csv.reader(f), [])
                if old != header:  # log from an older UI: keep it under another name
                    os.replace(LOG_CSV, LOG_CSV.replace(".csv", f"_until_{dt.date.today():%Y%m%d}.csv"))
            new = not os.path.exists(LOG_CSV)
            with open(LOG_CSV, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(header)
                w.writerow([dt.datetime.now().isoformat(timespec="seconds")] + row)
        except OSError as e:
            self.write_log(f"Cannot write {LOG_CSV}: {e}", "err")

    def _on_close(self):
        self.link.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
