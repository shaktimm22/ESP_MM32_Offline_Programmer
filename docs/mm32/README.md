# MM32 family (SWD)

The MM32 back end of the [offline programmer](../../README.md) programs MindMotion MM32 Cortex-M0 MCUs, such as the MM32F0010, MM32F0020, MM32SPIN160 and MM32F0144. The ESP8266 drives the target's **SWCLK/SWDIO** in software (SWD bit-bang). The target needs only **4 wires: VCC, GND, SWDIO, SWCLK**. NRST is not used.

Wiring, build, UIs and the common protocol are in the [main README](../../README.md). Source: [src/family_mm32.cpp](../../src/family_mm32.cpp), [src/mm32.cpp](../../src/mm32.cpp), [src/swd.cpp](../../src/swd.cpp).

## Image and settings

- **Image:** a Keil `.hex`, or a `.bin`, which is placed at 0x08000000.
- **For chip:** if you pick a specific chip, the programmer refuses to flash any other chip. This prevents loading F0010 firmware onto an F0020 by mistake. `ANY` disables the check.
- **Settings:**

| Setting | `SET` key | |
|---|---|---|
| Erase | `erase chip\|pages` | full chip, or only the pages the image uses |
| Read-protect | `protect 0\|1` | set read protection after programming (section 3) |
| SWD clock delay | `swdelay N` | 0 = fastest; raise (2–10) for long wires. Attach and probe slow down further on their own when needed |

## 1. Programming sequence (per target)

1. Connect over SWD (up to 3 attempts).
2. Halt the core and do a software reset (AIRCR.SYSRESETREQ) that stops on the reset vector, so clocks are back to HSI.
3. Read `DEV_ID @ 0x40013400` and look up the chip. Check the image's target chip, check that the image fits in flash, and check read protection.
4. Mass-erase, or erase only the image's pages.
5. Program halfword by halfword, checking `FLASH_SR.BSY`/`PGERR` after each halfword.
6. Verify by reading the flash back.
7. Optionally set read protection (RDP), then reset the target and run it.

Typical time: about 1–2 s for 16 KB. The F0020's flash is slower (up to 154 µs per halfword in the datasheet).

**FAIL** (counted) for MM32 means: unknown or wrong chip, image too big, erase/program/verify error, or a read-protected chip.

## 2. Chip table

| Chip | DEV_ID | Flash | Page | Source |
|---|---|---|---|---|
| MM32F0010 | `0xCC4350D1` | 16 KB | 1 KB | UM_MM32F0010 §19.3 |
| MM32F0020 | `0x4C50F800` | 32 KB | 1 KB | UM_MM32F0020 §18.6.2 |
| MM32F0140 (F0141/F0144) | learned from the first chip | 64 KB | 1 KB | DS_MM32F0140 |
| MM32SPIN160C | learned from the first chip | 32 KB | 1 KB | DS_MM32SPIN160C |

The MM32F0140 and MM32SPIN160C datasheets give the flash size, but not the DEV_ID (it is only in their user manuals). The programmer therefore learns it from the first real chip:

1. Connect one board, then press **Detect chip** or **FLASH**.
2. A **"New chip found"** dialog appears. Pick MM32F0140 or MM32SPIN160C, then press **Remember this chip**.
3. The ID is saved on the programmer. From then on that chip is recognised, and the "For chip" check works for it.

To undo a wrong choice, open **Chip table…** and delete that entry. The chip goes back to "not learned yet".

For **any other MM32 Cortex-M0**, choose *Another chip* in the dialog, or use **Chip table…**. Enter the name, the flash size in KB and the page size from that chip's datasheet.

This works as long as the chip uses the same FPEC flash controller at `0x40022000` (KEYR/SR/CR/AR) as the F0010/F0020. That is true for the MM32F0xx/SPIN0x Cortex-M0 families. Check the user manual of a new family before relying on it.

## 3. Read protection
- **Read-protect (RDP)** writes `0x807F` to the RDP option byte after a successful flash. The chip must be power-cycled before the protection takes effect.
- **Unlock (remove RDP)** erases the option bytes and writes `0x5AA5`. The chip then mass-erases itself, and you must power-cycle it.
- A protected chip is reported as FAIL with *"Chip is read-protected"* until it has been unlocked.
- Option-byte erase also resets the WRP and user option bytes (watchdog/stop/standby reset options) to their defaults.

## 4. Protocol additions

| Command | Reply data |
|---|---|
| `INFO` image fields | `base= target=` |
| `BEGIN <size> <crc32> <base> <target\|ANY> <name>` | start an upload |
| `DETECT` | `dpidr= devid= chip= flash= protected= swdelay=` |
| `UNLOCK` / `PROTECT` | remove / set read protection |
| `READ <addr> [words]` | dump up to 32 words of target memory (debug aid) |
| `CHIPS` | `0xID:NAME:KB:PAGE:b\|c,...` (b = built-in, c = custom) |
| `CHIPADD <id> <name> <kb> [page]` / `CHIPDEL <id>` | edit the custom chip table |

## Troubleshooting
| Symptom | Fix |
|---|---|
| "No SWD response" | Check GND, target power, and that SWCLK/SWDIO are not swapped. Raise *SWD delay*. |
| "No SWD response" only on boards that were already programmed | The application turns the SWD pins (PA14/PA15 on F0010/F0020) into GPIO or disables SWD. With 4-wire programming there is no NRST to stop it. Keep SWD enabled in the application, or add a ~1 s delay at startup before reconfiguring those pins, and connect the board while it is powering up (auto mode works well for this). |
| "Could not halt the core" | The application enters a low-power mode (stop/standby) right after boot. Same fixes as above. |
| "Unknown chip ID" | Add it through **Chip table…** (see section 2). |
| "Program error ... (not erased?)" | You chose *Only image pages* but the page size in the chip table is wrong. Use *Full chip* erase. |
