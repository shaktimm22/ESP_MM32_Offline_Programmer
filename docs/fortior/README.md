# Fortior FU68xx family (FICE)

The Fortior back end of the [offline programmer](../../README.md) programs Fortior FU68xx BLDC motor MCUs (8051 core, 16 KB flash, such as the FU6832 and FU6862). It replaces the Fortior emulator + Keil for production flashing.

- The ESP8266 drives the chip's two-wire **FICE** debug interface (FICEK/FICED) in software, on the same pins as MM32's SWCLK/SWDIO.
- The target needs **4 wires: VDD5, GND, FICEK, FICED**, the same 4 pins the Fortior emulator uses. FICEK/FICED **must go through a level shifter** (see the [main README](../../README.md#1-hardware)).
- The FICE protocol is not published. It was reverse-engineered from logic-analyzer captures of the Fortior emulator; see [re/FINDINGS.md](../../re/FINDINGS.md).

Wiring, build, UIs and the common protocol are in the [main README](../../README.md). Source: [src/family_fortior.cpp](../../src/family_fortior.cpp), [src/fu68.cpp](../../src/fu68.cpp), [src/fice.cpp](../../src/fice.cpp).

> **Status: not yet tested on a real chip.**
> - Every command the firmware sends was checked against the Keil captures: the setup and finishing sequences are identical, frame for frame.
> - The full flash sequence was run in a simulator, and the CRC it expects equals the CRC the real chip reported.
> - The electrical timing still has to be confirmed with a logic analyzer on the first board (see the checklist at the end).
> - The Wi-Fi access point now stays on. Each FICE frame runs with interrupts disabled, so Wi-Fi activity can only stretch the gaps between frames. Confirm this on the analyzer during bring-up too.

## Image and settings

- **Image:** the Keil `.hex`. It must end before 0x3FFC, where the option bytes go.
- **Option bytes:** set them to match your Keil *Fortior C51 FICE Driver Setting*:
  - **LVR** and **Watchdog enable** set the bytes for you.
  - Any other setting can be typed as the 8 hex digits written to 0x3FFC–0x3FFF.
  - The UI shows the CRC the chip must report after programming.
  - If only the option bytes changed, the upload button becomes **Apply options**, which changes them without uploading again.
- **FICE timing** (`SET speed`, 50–1000 %, default 100 %): bit timing relative to the Fortior emulator. Raise it (for example to 200 %) for long wires.
- **Verify** compares the chip's own CRC with the expected one.

## Option bytes (0x3FFC–0x3FFF)

Keil does not put these in the `.hex`; the emulator adds them. The firmware also copies the first three into the chip's configuration registers CCFG 0x401C–0x401E.

| Keil setting | Bytes | Verified with a capture |
|---|---|---|
| LVR 3.8 V, watchdog off (default) | `00 F0 00 FF` | yes |
| LVR 3.0 V | `00 B0 00 FF` | yes |
| LVR 3.8 V + watchdog enable | `00 F0 20 FF` | yes |

- Byte 0x3FFD is CCFG2: bits 7:6 = LVR. Byte 0x3FFE is CCFG1: bit 5 = WDT_EN.
- For the other LVR levels (2.8 V and 3.5 V), and for LVW and clock settings, make one Keil capture to find the exact value.
- Keil's "3.0 V" gives bits 7:6 = `10`, while the FU6862 datasheet table lists `10` as 3.5 V. So copy the value from a capture rather than from the datasheet.

## Programming sequence (per board)

1. **Connect:** a 42 µs reset pulse on FICEK, then wait 15 ms. Read the chip ID (FICE register 0 = 0x68).
2. **Erase:** the two erase commands Keil uses (0.38 s and 0.15 s), plus the sequence that follows them.
3. **Configure:** write the option bytes into CCFG 0x401C–0x401E.
4. **Program:** in bursts of 128 bytes. Blocks that are all 0x00 (the erased state) are skipped. The 4 option bytes go to 0x3FFC.
5. **Verify:** the chip's own CRC unit computes CRC-16/CCITT over all 16 KB, and the firmware compares it with the expected value.
6. **Run:** a reset pulse, then FICEK is released so the motor firmware starts.

## Protocol additions

| Command | Reply data |
|---|---|
| `INFO` image fields | `opt= chipcrc=` (the CRC the chip must report) |
| `BEGIN <size> <crc32> <opt> <name>` | start an upload (`opt` = 8 hex digits) |
| `OPT <8 hex>` | change the option bytes of the stored image; replies with the `INFO` string |
| `DETECT` | `chip= id= speed=` |
| `RESET` | reset pulse, then let the firmware run |
| `FICE <ops>` | raw FICE access for experiments (see below) |

`FICE` ops are space-separated:
- `A` takes the lines, `X` releases them, `P` sends a reset pulse, `D<ms>` waits.
- `S<reg>` selects a register, `W<hex>[,<polls>]` writes a byte, `R` reads one.

For example, `FICE A P D15 S3 R W10 S4 WF9 D10 S0 R X` reads the chip ID.

## Reverse-engineering tools

Use these to check a new chip, a Keil option or the programmer itself against a Keil capture.

| Tool | Use |
|---|---|
| [re/fice_decode.py](../../re/fice_decode.py) | Saleae CSV export (also `.csv.xz`) → decoded op log, and a check against the `.hex` |
| [re/fice_summarize.py](../../re/fice_summarize.py) | op log → named operations (erase, program, XDATA, CRC …) |
| [re/sr_inspect.py](../../re/sr_inspect.py) | first look at a sigrok/PulseView `.sr` capture |
| [re/FINDINGS.md](../../re/FINDINGS.md) | protocol description |

## Known limitations

- **Code protection (Keil "Encrypt Enable") is not supported yet.** It needs one capture of a download with encryption turned on.
- **The step after the erase is copied from Keil, not understood.** It contains key bytes F1 F5 E5 and A0 AA A2 50 00; it may handle the option area.
- **Auto-detect resets the board while probing.** It checks for a board every 300 ms by sending a reset and reading the chip ID, so a board sitting in the fixture is reset repeatedly.
- **Only 16 KB parts are handled.** The flash size and the CRC sector count are fixed.

## First bring-up checklist

1. Select the **Fortior FU68xx** chip family in the UI.
2. Wire GND, VDD5, and FICEK/FICED through the level shifter. Connect a logic analyzer to FICEK and FICED as well.
3. In the UI's command box, send `FICE A P D15 S3 R W10 S4 WF9 D10 S0 R X`. You should get `S0 … R=68`.
4. Press **Detect chip**, then **FLASH** a board and compare the analyzer trace with a Keil capture using `re/fice_decode.py`.
5. If reads fail, raise **FICE timing** to 200% (or more) and check the level shifter.
