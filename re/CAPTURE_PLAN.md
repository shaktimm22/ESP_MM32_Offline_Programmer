# FICE capture plan

Goal: record what the Fortior emulator sends on FICEK/FICED while Keil performs each operation, so the protocol can be decoded and re-implemented on the ESP8266.

## Safety first

Many fan controller boards are **not isolated from AC mains**. The logic analyzer's ground is tied to your PC's USB ground. **Never probe a board that is powered from AC.**
- Power the board only from the emulator, or from a bench supply, exactly as you do when flashing in Keil.
- Do not connect the motor.

## Setup

- Logic analyzer: any 8-channel 24 MHz "Saleae clone" with **PulseView** (sigrok). Driver: `fx2lafw`.
- Connect **GND** of the analyzer to target GND.

| Analyzer | Signal |
|---|---|
| D0 | FICEK (RSTN pin of the MCU) |
| D1 | FICED (P1.2) |
| D2 | VDD5 (most cheap analyzers accept 5 V; check yours) |
| D3 | VCC / supply input of the board, if the emulator switches it |

- In PulseView, rename the channels to FICEK, FICED, VDD5 and VCC.
- Sample rate **24 MHz**. Set the sample count long enough to cover the whole operation (for example 100 M samples ≈ 4 s; use more for downloads).
- Start the capture first, then click in Keil.
- Save each capture as **`.sr`** with the name from the table below.

## Keil test programs

1. Make a blank FU68xx Keil project in the same way you make your normal ones.
2. Replace `main.c` with [keil_test/test_a.c](keil_test/test_a.c) and build. Copy the `.hex` to `test_a.hex`.
3. Do the same with [keil_test/test_b.c](keil_test/test_b.c) to get `test_b.hex`.

The two images differ in exactly one byte. Comparing their captures shows how data is encoded.

- Leave the Fortior driver settings (Debug → Settings) the same for every capture unless a step says otherwise.
- **Untick all Cache Options (Data / XData / Code).** Otherwise Keil may show cached values instead of reading the chip.

## Captures to make

Make one capture per row, in this order, using one chip.

| File | What to do in Keil |
|---|---|
| `01_connect.sr` | With Utilities → "Update Target before Debugging" **unticked**, start a debug session (Ctrl+F5). Stop capturing once it is in debug, then end the session. |
| `02_erase.sr` | Flash → **Erase** (skip if greyed out). |
| `03_download_a.sr` | Flash → **Download** with `test_a.hex`. |
| `04_download_b.sr` | Flash → **Download** with `test_b.hex`. |
| `05_download_a_again.sr` | Download `test_a.hex` again. This shows whether anything changes between runs (random or challenge values). |
| `06_read_code.sr` | In a debug session, open a Memory window and type `C:0x0000`. Stop capturing. Then type the address of `pattern` (shown in the `.m51` map file) and capture again as `06b_read_pattern.sr`. |
| `07_read_xdata.sr` | Memory window: `X:0x0000`. |
| `08_write_xdata.sr` | In the `X:0x0000` memory window, change one byte to `55`. |
| `09_reset.sr` | Debug → Reset CPU. |
| `10_run_halt.sr` | Run (F5), wait one second, then Stop. |
| `11_download_encrypt.sr` | Debug Settings: **Encrypt Enable** (Full). Download `test_a.hex`. |
| `12_connect_encrypted.sr` | Start a debug session on the encrypted chip. |
| `13_erase_encrypted.sr` | Flash → Erase on the encrypted chip, to see how protection is removed. |
| `14_download_wdt.sr` | Turn encryption off again and tick **Watch dog Enable** only. Download `test_a.hex`. Comparing with 03 shows where the config (CCFG) bytes go. |
| `15_download_lvr.sr` | Settings back to normal, but **LVR Config** set to a different voltage. Download `test_a.hex`. |

If a step fails or looks odd, still save the capture and write down what Keil showed.

## What to send

- All `.sr` files.
- `test_a.hex`, `test_b.hex` and the `.m51` map file.
- A screenshot of the Fortior driver settings page.
- The exact chip part number (for example FU6832L or FU6862Q).

## Quick check after the first capture

```
python re/sr_inspect.py 01_connect.sr
```

If it prints `WARNING: under 4 samples per half-period`, the clock is too fast for 24 MHz. Tell me before making the rest of the captures.
