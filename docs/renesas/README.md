# Renesas family (SWD)

The Renesas back end of the [offline programmer](../../README.md) programs the **Renesas RA2E1** (Cortex-M23: R7FA2E1A9 / A7 / A5, 128 / 64 / 32 KB code flash). It does the same job as the E2 emulator Lite with Renesas Flash Programmer: erase, program, verify and run, over SWD.

It was merged in from the separate `flasher_renesas` project. It shares the SWD driver with the MM32 family.

Wiring, build, UIs and the common protocol are in the [main README](../../README.md). Source: [src/family_renesas.cpp](../../src/family_renesas.cpp), [src/ra.cpp](../../src/ra.cpp), [src/swd.cpp](../../src/swd.cpp).

## Wiring

| ESP8266 (NodeMCU) | GPIO | RA2E1 | |
|---|---|---|---|
| D5 | 14 | **SWCLK** (P300) | |
| D6 | 12 | **SWDIO** (P108) | |
| D7 | 13 | **RES** | Recommended. Open drain: the ESP8266 only pulls it low |
| GND | – | **VSS** | Required |
| 3V3 | – | VCC | Only if the programmer powers the target |

- These are the same pins the E2 Lite uses on its 10-pin connector: SWCLK, SWDIO, RES, VCC, GND.
- **MD (P201) must stay high** (single-chip mode), as it is on a normal board. If MD is low, the chip starts in its serial boot mode.
- **RES** is optional, but wire it if possible. With it, the programmer connects **under reset**, so firmware that turns the SWD pins into GPIO, or enters software standby, cannot lock it out. Without it, untick **Use RES pin**, or build with `-D PIN_NRST=-1`.
- RES on D7 is used only by this family. MM32 and Fortior boards leave D7 unconnected.
- **Voltage:** a 3.3 V board connects directly. A **5 V board needs a level shifter** on SWCLK/SWDIO, for example a TXS0102 with the target side at the board's VCC. RES needs no shifter because it is only ever pulled low.

## Image and settings

- **Image:** the e2 studio output, `.srec` / `.mot` (S-record) or `.hex` (Intel HEX), found in the project's `Debug` / `Release` folder. A `.bin` is placed at address 0.
- **What gets programmed:**

| Region | Address | Handling |
|---|---|---|
| Code flash | `0x00000000`, up to 128 KB | Programmed. This includes the OFS0/OFS1 option settings at `0x400`, which FSP puts in the image |
| Data flash | `0x40100000`, 4 KB | Programmed if the file has data there |
| ID code / access window (extra area) | `0x01010000` – `0x0101003F` | FSP always emits 0xFF here (unlocked). That is accepted and left as is. A file that **sets** an ID code is refused; this is not supported yet |

- **Settings:**

| Setting | `SET` key | |
|---|---|---|
| Erase | `erase chip\|blocks` | **Full chip**: every code and data flash block, like a fresh chip. **Only image blocks**: only the 2 KB code blocks (and 1 KB data blocks) the image covers. Data flash contents outside the image are kept |
| Use RES pin | `reset 0\|1` | connect under reset, and use RES for the final reset |
| SWD clock delay | `swdelay N` | 0 = fastest. Attach and probe slow down further on their own when a board does not answer |

## 1. Programming sequence (per target)

This all runs over SWD with the core halted. It follows the FSP `r_flash_lp` driver (FSP 6.3.1, MF4 flash).

1. **Connect:** under RES if enabled. Then halt on the reset vector after a system reset, so no firmware runs. Read the part number (`PNR`, `0x01001C10`) and the 128-bit unique ID (`UIDR`, `0x01001C00`). The part number gives the code flash size: A5 = 32 KB, A7 = 64 KB, A9 = 128 KB.
2. **Check:** the image must fit the detected part's code flash.
3. **Clock:** after reset, ICLK is MOCO/16 = 0.5 MHz, which is below the flash sequencer's 1 MHz minimum. The programmer switches to MOCO/1 = 8 MHz and sets `FISR.PCKA` to match.
4. **Erase:** 2 KB code blocks in code flash P/E mode (`FENTRYR=0xAA01`), and 1 KB data blocks in data flash P/E mode (`0xAA80`).
5. **Program:** 4 bytes per command for code flash, 1 byte for data flash. Erased words (all 0xFF) are skipped.
6. **Verify:** block reads compared with the stored image.
7. **Run:** clear the debug state, release the debug power request and reset.

The chip's **unique ID** is reported as the chip ID in the `CHIP` / `RESULT` events, so `tools/flash_log.csv` records which board got which result.

## 2. Protocol additions

| Command / field | |
|---|---|
| `SET family renesas` | select this family |
| `BEGIN <size> <crc32> <cf> <df> <name>` | `cf` code flash bytes from 0x0 (multiple of 4), then `df` data flash bytes from 0x40100000. `size` must equal `cf + df` |
| `INFO` | image fields `cf= df=` |
| `DETECT` | `dpidr= part= flash=<KB> uid= aws= awe= swdelay= reset=` |
| `ERASE` | erase the whole code flash and data flash |
| `RESET` | reset and run the target |
| `READ <addr> [words]` | dump up to 32 words of target memory |

HTTP upload: `POST /api/upload?size=&crc=&fields=<cf> <df>&name=`.

## First bring-up checklist

This back end was written from the RA2E1 SVD and the FSP flash driver. Check it on a known-good board before production:

1. **Detect chip.** Expect `part=R7FA2E1A…`, the right `flash=` size and a UID. If you get `part=UNKNOWN` with a valid DPIDR, the part-number register reads differently from what is expected. Send the output of `READ 0x01001C10 4` (Command box in the UI).
2. **FLASH** a known image with *Verify* on, and check that the board runs.
3. Check that the board's own settings in data flash are kept or erased, depending on the *Erase* setting you chose.
4. Compare the timing with the E2 Lite. Typical: roughly 0.1–0.2 s per KB of code, since each 4-byte write is about 20 SWD transactions.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No SWD response: SWDIO idles high" | Target unpowered, GND missing, or SWCLK/SWDIO swapped |
| "SWDIO is held LOW" | Firmware drives P108. Wire RES and tick *Use RES pin* |
| "Garbled SWD reply" | Signal levels (a 5 V board without a level shifter) or long wires: raise *SWD clock delay* |
| "Could not halt the core" | ID code protection is set on that chip. The programmer does not support ID authentication |
| "Erase failed … illegal command" | Blocks protected by an access window (see `aws`/`awe` in DETECT) |
| "Image needs N KB of code flash" | The image does not fit the detected part, for example a 64 KB image on an A5 |
