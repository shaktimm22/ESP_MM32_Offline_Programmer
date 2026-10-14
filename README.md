# MM32 Offline Programmer (ESP8266)

A standalone production programmer for MindMotion MM32 Cortex-M0 MCUs, such as the MM32F0010, MM32F0020, MM32SPIN160 and MM32F0144.

- An ESP8266 drives the target's **SWCLK/SWDIO** in software (SWD bit-bang).
- The firmware image lives in the ESP8266's flash (LittleFS), so no PC or Keil is needed while programming.
- Programming starts on a **button press** or **automatically when a target is connected**.
- A Python UI uploads the `.hex`/`.bin` file, changes the settings and shows **PASS / FAIL / TOTAL** counters over the USB-UART link.

```
 PC (tools/mm32_flasher_ui.py) ──USB/UART 460800──> ESP8266 ──SWD──> MM32 target
```

## 1. Hardware

The target needs only **4 wires: VCC, GND, SWDIO, SWCLK**. NRST is not used.

| ESP8266 (NodeMCU) | GPIO | Connect to |
|---|---|---|
| D5 | 14 | Target **SWCLK** (MM32F0010/F0020: PA15) |
| D6 | 12 | Target **SWDIO** (MM32F0010/F0020: PA14) |
| GND | – | Target **GND** (required) |
| 3V3 | – | Target VDD (only if you power the target from the programmer) |
| D3 | 0 | Start button. This is the onboard **FLASH** button on NodeMCU; an external button goes from D3 to GND |
| D1 | 5 | Green PASS LED (with a 330 Ω resistor to GND) |
| D2 | 4 | Red FAIL LED (with a 330 Ω resistor to GND) |
| D4 | 2 | Onboard blue LED = BUSY (blinks while programming) |
| D8 | 15 | Buzzer through an NPN transistor (optional) |

To change the pins, edit [src/pins.h](src/pins.h). You can also override them with `-D PIN_xxx=` in `platformio.ini`. Set `PIN_BUZZER` to `-1` to disable it.

**Voltage:** ESP8266 pins are 3.3 V only. The MM32 parts run from 2.0 V to 5.5 V. If your product runs the MCU at 5 V, use one of these options while programming:
- power the board from the programmer's 3.3 V, or
- add a level shifter on SWCLK and SWDIO.

Wire tip: keep the SWD wires short (under 15 cm) and twist each signal with GND. If you see random failures with long pogo-pin fixtures, raise **SWD delay** in the UI (for example to 2–10).

## 2. Build and flash the ESP8266

This is a PlatformIO project (VS Code → PlatformIO → *Upload*), or from the command line:

```
pio run -t upload
```

The filesystem is formatted automatically on first boot.

## 3. PC application

```
pip install -r tools/requirements.txt
python tools/mm32_flasher_ui.py      (or double-click tools/run_ui.bat)
```

`run_ui.bat` is the easiest way to start the UI. It picks a Python that has tkinter and installs pyserial into it if needed. Do not use PlatformIO's built-in Python, which is first on the PATH in VS Code's PlatformIO terminal: it has no tkinter and fails with `No module named 'tkinter'`.

1. Pick the COM port at **460800** baud and press **Connect**.
2. **Browse** to the Keil `.hex` (or a `.bin`, which is placed at 0x08000000).
3. Choose **Image is for chip**. If you pick a specific chip, the programmer refuses to flash any other chip. This prevents loading F0010 firmware onto an F0020 by mistake. `ANY` disables the check.
4. Press **Upload to programmer**. The image is CRC-checked and stored on the ESP8266, and it survives power-off.
5. Choose the **Start mode**, then press **Apply settings**:
   - **Button press:** press the button on the programmer to flash.
   - **Auto-detect target:** the programmer polls SWD. When a board is connected, it waits *Auto settle* ms and then flashes. It flashes again only after the board has been removed.
6. Watch the PASS/FAIL counters. Every result is also appended to `tools/flash_log.csv`.

Once the image is uploaded, the PC can be unplugged. The programmer works on any 5 V USB supply, and the counters are kept on the device.

### Programming sequence (per target)

1. Connect over SWD (up to 3 attempts).
2. Halt the core and do a software reset (AIRCR.SYSRESETREQ) that stops on the reset vector, so clocks are back to HSI.
3. Read `DEV_ID @ 0x40013400` and look up the chip. Check the image's target chip, check that the image fits in flash, and check read protection.
4. Mass-erase, or erase only the image's pages.
5. Program halfword by halfword, checking `FLASH_SR.BSY`/`PGERR` after each halfword.
6. Verify by reading the flash back.
7. Optionally set read protection (RDP), then reset the target and run it.

Typical time: about 1–2 s for 16 KB. The F0020's flash is slower (up to 154 µs per halfword in the datasheet).

### Counting rules
- **PASS:** the target was programmed and verified.
- **FAIL:** a target answered but something went wrong: unknown or wrong chip, image too big, erase/program/verify error, or read-protected chip.
- **Not counted:** no target responded at all (for example, the button was pressed with nothing connected). The UI still shows the message.

## 4. Chip table

| Chip | DEV_ID | Flash | Page | Source |
|---|---|---|---|---|
| MM32F0010 | `0xCC4350D1` | 16 KB | 1 KB | UM_MM32F0010 §19.3 |
| MM32F0020 | `0x4C50F800` | 32 KB | 1 KB | UM_MM32F0020 §18.6.2 |
| MM32F0140 (F0141/F0144) | learned from the first chip | 64 KB | 1 KB | DS_MM32F0140 |
| MM32SPIN160C | learned from the first chip | 32 KB | 1 KB | DS_MM32SPIN160C |

The MM32F0140 and MM32SPIN160C datasheets give the flash size, but not the DEV_ID (it is only in their user manuals). The programmer therefore learns it from the first real chip:

1. Connect one board, then press **Detect chip** or **FLASH**.
2. A **"New chip found"** dialog appears. Pick MM32F0140 or MM32SPIN160C, then press **Remember this chip**.
3. The ID is saved on the programmer. From then on that chip is recognised, and the "image is for chip" check works for it.

To undo a wrong choice, open **Chip table…** and delete that entry. The chip goes back to "not learned yet".

For **any other MM32 Cortex-M0**, choose *Another chip* in the dialog, or use **Chip table…**. Enter the name, the flash size in KB and the page size from that chip's datasheet.

This works as long as the chip uses the same FPEC flash controller at `0x40022000` (KEYR/SR/CR/AR) as the F0010/F0020. That is true for the MM32F0xx/SPIN0x Cortex-M0 families. Check the user manual of a new family before relying on it.

## 5. Read protection
- **Read-protect (RDP)** writes `0x807F` to the RDP option byte after a successful flash. The chip must be power-cycled before the protection takes effect.
- **Unlock (remove RDP)** erases the option bytes and writes `0x5AA5`. The chip then mass-erases itself, and you must power-cycle it.
- A protected chip is reported as FAIL with *"Chip is read-protected"* until it has been unlocked.
- Option-byte erase also resets the WRP and user option bytes (watchdog/stop/standby reset options) to their defaults.

## 6. UART protocol (for automation / other front-ends)

The link is 460800 8N1 and line-based (`\n`).
- Replies are `@OK <CMD> [data]` or `@ERR <CMD> <message>`.
- Asynchronous events are `@EVT ...`.
- Any line that does not start with `@` is ESP8266 boot noise and should be ignored.

| Command | Reply data |
|---|---|
| `PING` | `MM32-FLASHER <ver>` |
| `INFO` | `fw= img= size= crc= base= target= name= pass= fail= free= mode= erase= verify= protect= run= swdelay= settle=` |
| `CHIPS` | `0xID:NAME:KB:PAGE:b\|c,...` (b = built-in, c = custom) |
| `BEGIN <size> <crc32> <base> <target\|ANY> <name>` | start an upload |
| `DATA <offset> <hex>` | up to 256 bytes per line, offsets must be sequential; replies with the bytes received so far |
| `END` | checks size and CRC32 (zlib), then commits the image; replies with the `INFO` string |
| `ABORT` | discard the upload |
| `FLASH` | replies immediately, then sends events |
| `DETECT` | `dpidr= devid= chip= flash= protected=` |
| `ERASE` / `UNLOCK` / `PROTECT` / `RESET` | maintenance actions |
| `SET <key> <value>` | keys: `mode button\|auto`, `erase chip\|pages`, `verify`, `protect`, `run` (0/1), `swdelay N`, `settle ms` |
| `RESETCNT` | zero the counters |
| `CHIPADD <id> <name> <kb> [page]` / `CHIPDEL <id>` | edit the custom chip table |

Events:
- `@EVT STATE connect|erase|program|verify|protect|idle`
- `@EVT PROGRESS <stage> <pct>`
- `@EVT CHIP <name> <devid>`
- `@EVT RESULT PASS <ms> <chip> <devid>`
- `@EVT RESULT FAIL <counted 0|1> <message>`
- `@EVT COUNT <pass> <fail>`
- `@EVT TARGET present|absent`
- `@EVT BOOT ...`

## 7. Troubleshooting
| Symptom | Fix |
|---|---|
| UI says "programmer did not answer" | Wrong COM port or baud rate. Close the serial monitor or any other program using the port. |
| "No SWD response" | Check GND, target power, and that SWCLK/SWDIO are not swapped. Raise *SWD delay*. |
| "No SWD response" only on boards that were already programmed | The application turns the SWD pins (PA14/PA15 on F0010/F0020) into GPIO or disables SWD. With 4-wire programming there is no NRST to stop it. Keep SWD enabled in the application, or add a ~1 s delay at startup before reconfiguring those pins, and connect the board while it is powering up (auto mode works well for this). |
| "Could not halt the core" | The application enters a low-power mode (stop/standby) right after boot. Same fixes as above. |
| "Unknown chip ID" | Add it through **Chips...** (see section 4). |
| "Program error ... (not erased?)" | You chose *Only image pages* but the page size in the chip table is wrong. Use *Full chip* erase. |
| ESP8266 resets when the UI connects | The UI keeps DTR/RTS released. If your USB-UART board still resets, add a 10 µF capacitor from EN to GND. |
