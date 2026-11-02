# Offline Programmer (ESP8266)

A standalone production programmer built on an ESP8266 (NodeMCU). One firmware programs three chip families, and you choose which one from the PC or web UI:

| Family | Chips | Interface | Details |
|---|---|---|---|
| **MM32** | MindMotion Cortex-M0: MM32F0010, F0020, F0140, SPIN160C … | SWD (bit-banged) | [docs/mm32/README.md](docs/mm32/README.md) |
| **Fortior** | Fortior FU68xx 8051 BLDC MCUs, 16 KB flash: FU6832, FU6862 … | FICE (reverse-engineered) | [docs/fortior/README.md](docs/fortior/README.md) |
| **Renesas** | Renesas RA2E1 Cortex-M23: R7FA2E1A9 / A7 / A5 (128 / 64 / 32 KB) | SWD (bit-banged), + RES | [docs/renesas/README.md](docs/renesas/README.md) |

- The firmware image lives in the ESP8266's flash (LittleFS), so no PC or Keil is needed while programming. Each family has its own stored image, so switching families does not require uploading again.
- Programming starts on a **button press** or **automatically when a target is connected**.
- A Python UI (USB) or the built-in web page (Wi-Fi) selects the family, uploads the `.hex` / `.srec`, changes the settings and shows **PASS / FAIL / TOTAL**.

```
 PC (tools/flasher_ui.py) ──USB/UART 460800──> ESP8266 ──SWD / FICE──> MM32 / FU68xx / RA2E1 target
 Phone / laptop browser ──────Wi-Fi AP + HTTP─────────┘
```

## 1. Hardware

All families use the same two target lines, so one fixture connector serves them all. Renesas can also use RES on D7:

| ESP8266 (NodeMCU) | GPIO | MM32 | Fortior FU68xx | Renesas RA2E1 |
|---|---|---|---|---|
| D5 | 14 | **SWCLK** (F0010/F0020: PA15) | **FICEK** (RSTN pin) | **SWCLK** (P300) |
| D6 | 12 | **SWDIO** (F0010/F0020: PA14) | **FICED** (P1.2) | **SWDIO** (P108) |
| D7 | 13 | – | – | **RES** (optional, recommended; open drain) |
| GND | – | **GND** (required) | **GND** (required, close to the FICE pins) | **VSS** (required) |
| 3V3 | – | VDD, only if the programmer powers the target | – (the FU68xx needs 5 V VDD5 to program) | VCC, only if the programmer powers the target |
| D3 | 0 | Start button. This is the onboard **FLASH** button; an external button goes from D3 to GND | | |
| D1 | 5 | Green PASS LED (with 330 Ω to GND) | | |
| D2 | 4 | Red FAIL LED (with 330 Ω to GND) | | |
| D4 | 2 | Onboard blue LED = BUSY (blinks while programming) | | |
| D8 | 15 | Buzzer through an NPN transistor (optional) | | |

To change the pins, edit [src/pins.h](src/pins.h), or override them with `-D PIN_xxx=` in `platformio.ini`. Set `PIN_BUZZER` (or `PIN_NRST`, the Renesas RES line) to `-1` to disable it.

**Voltage:** the ESP8266 pins are 3.3 V only.
- **MM32** parts run from 2.0 V to 5.5 V. A 5 V board needs either power from the programmer's 3.3 V while programming, or a level shifter.
- **FU68xx** I/O runs at VDD5, which must be **5–5.5 V while the flash is written**. A **bidirectional level shifter is required**, for example a **TXS0102** (two channels, auto-direction): 3.3 V on the ESP8266 side, the target's VDD on the other.
- **RA2E1:** a 3.3 V board connects directly; a 5 V board needs a level shifter on SWCLK/SWDIO. RES needs none (it is only pulled low).
- A TXS0102 powered from the target's VDD works for all families, so a fixture can keep it permanently.

Keep the wires short (under 15 cm) and run a GND wire next to them. With long pogo-pin fixtures, raise **SWD clock delay** (MM32, Renesas) or **FICE timing** (Fortior).

## 2. Build and flash the ESP8266

This is a PlatformIO project (VS Code → PlatformIO → *Upload*), or from the command line:

```
pio run -t upload
```

- The filesystem is formatted automatically on first boot.
- Upgrading from the 1.x single-family firmware keeps the stored image and selects its family.
- The ESP8266 runs at 160 MHz for exact bit timing.

## 3. PC application

```
pip install -r tools/requirements.txt
python tools/flasher_ui.py      (or double-click tools/run_ui.bat)
```

`run_ui.bat` is the easiest way to start the UI. It picks a Python that has tkinter and installs pyserial into it if needed. Do not use PlatformIO's built-in Python, which is first on the PATH in VS Code's PlatformIO terminal: it has no tkinter and fails with `No module named 'tkinter'`.

1. Pick the COM port at **460800** baud and press **Connect**.
2. Choose the **Chip family** (MM32, Fortior or Renesas RA). This switches the programmer straight away: the button, auto-detect and the web UI all use it.
3. **Browse** to the firmware: the Keil `.hex` (MM32, Fortior) or the e2 studio `.srec` / `.mot` / `.hex` (Renesas). The UI remembers the last file for each family.
4. Set the family-specific image options:
   - **MM32:** *For chip* (see [docs/mm32](docs/mm32/README.md)).
   - **Fortior:** *LVR / Watchdog / option bytes* (see [docs/fortior](docs/fortior/README.md)).
   - **Renesas:** none; the code flash / data flash layout comes from the file (see [docs/renesas](docs/renesas/README.md)).
5. Press **Upload to programmer**. The image is CRC-checked and stored on the ESP8266, and it survives power-off.
6. Choose the **Start mode** and the other settings, then press **Save to programmer**:
   - **Button press:** press the button on the programmer to flash.
   - **Auto-detect board:** the programmer polls the target. When a board is connected, it waits *Auto-detect settle* ms and then flashes. It flashes again only after the board has been removed.
7. Press **FLASH** (F5) or the button on the programmer. Every result is also appended to `tools/flash_log.csv`.

Once the image is uploaded, the PC can be unplugged. The programmer works on any 5 V USB supply, and the counters are kept on the device.

### Web UI (Wi-Fi)

The programmer also runs its own Wi-Fi network, with the same UI in a browser. Use it from a phone or laptop, with no USB cable or Python needed.

1. Join the Wi-Fi network **`MCU-Flasher-XXXX`** (XXXX = the end of the MAC address). It is open by default.
2. The phone or laptop should open the page by itself (captive portal). If it does not, browse to **http://mcu-flasher.local** or **http://192.168.4.1**.

The page has the same cards as the desktop app, including the chip family selector.
- The USB UI and the web UI can be used at the same time. Each follows the family the other one selects.
- While a flash is running, the web page only shows status.
- **Download results (.csv)** saves the PASS/FAIL results seen while the page was open.

To set a password, SSID or hostname, uncomment the `WIFI_AP_*` / `MDNS_HOSTNAME` flags in `platformio.ini` (the password needs 8–63 characters).

The page source is [web/index.html](web/index.html). Every build gzips it into `src/web_page.h` (via `tools/embed_web.py`), so no `uploadfs` is needed. Do not run `uploadfs`: it would erase the stored images and settings.

## 4. UART protocol (for automation / other front-ends)

The link is 460800 8N1 and line-based (`\n`).
- Replies are `@OK <CMD> [data]` or `@ERR <CMD> <message>`.
- Asynchronous events are `@EVT ...`.
- Any line that does not start with `@` is ESP8266 boot noise and should be ignored.

All commands act on the **active family** (`SET family`). The family-specific commands are listed in each family's guide.

| Command | Reply data |
|---|---|
| `PING` | `FLASHER <ver>` |
| `INFO` | `fw= img= size= crc= name=` + the family's image fields + `pass= fail= free=` + all settings (see `SET`) |
| `SET family mm32\|fortior\|renesas` | switch the active family (also sends `@EVT FAMILY <name>`) |
| `SET <key> <value>` | common keys: `mode button\|auto`, `verify 0\|1`, `run 0\|1`, `settle <ms>`; family keys in the family guides |
| `BEGIN <size> <crc32> <fields> <name>` | start an upload into the active family's slot. MM32 fields: `<base> <target\|ANY>`. Fortior fields: `<option bytes, 8 hex>`. Renesas fields: `<cf bytes> <df bytes>` |
| `DATA <offset> <hex>` | up to 256 bytes per line, offsets must be sequential; replies with the bytes received so far |
| `END` | checks size and CRC32 (zlib), then commits the image; replies with the `INFO` string |
| `ABORT` | discard the upload |
| `FLASH` | replies immediately, then sends events |
| `DETECT` / `ERASE` / `RESET` | identify, erase, or reset and run the connected target |
| `RESETCNT` | zero the counters (shared by all families) |

Events:
- `@EVT STATE connect|erase|program|verify|protect|idle`
- `@EVT PROGRESS <stage> <pct>`
- `@EVT CHIP <name> <id>`
- `@EVT RESULT PASS <ms> <chip> <id>`
- `@EVT RESULT FAIL <counted 0|1> <message>`
- `@EVT COUNT <pass> <fail>`
- `@EVT TARGET present|absent`
- `@EVT FAMILY mm32|fortior|renesas`
- `@EVT BOOT FLASHER <ver> family=<name>`
- `@EVT WIFI ssid=<name> ip=<ip> url=<url> open|secured`

HTTP API (same line protocol):

| Route | |
|---|---|
| `POST /api/cmd` `c=<command>` | runs one command, returns `@OK ...` / `@ERR ...` |
| `GET /api/events?since=<seq>` | first line `<seq> <busy>`, then the `@EVT` lines after `<seq>` |
| `POST /api/upload?size=&crc=&fields=&name=` | multipart file with the binary image (`fields` as in `BEGIN`); returns the `END` reply |

**Counting rules:**
- **PASS:** the target was programmed (and verified, if *Verify* is on).
- **FAIL:** a target answered but something went wrong.
- **Not counted:** no target responded at all, for example when the button was pressed with nothing connected. The UI still shows the message.

## 5. Layout

```
src/main.cpp              protocol, upload, button / auto-detect, results
src/family.h              interface each chip family implements
src/family_mm32.cpp       MM32 back end      -> mm32.cpp (flash controller), swd.cpp (SWD bit-bang)
src/family_fortior.cpp    FU68xx back end    -> fu68.cpp (command sequences), fice.cpp (FICE bit-bang)
src/family_renesas.cpp    RA2E1 back end     -> ra.cpp (MF4 flash sequencer), swd.cpp (shared with MM32)
src/storage.cpp           LittleFS: one image per family, settings, counters, MM32 chip table
src/web.cpp               Wi-Fi AP, captive portal, HTTP API;  web/index.html = the page
tools/flasher_ui.py       PC UI
docs/<family>/            family guide and datasheets
re/, captures/fortior/    FICE reverse engineering; captures as .sal sessions or xz-compressed CSV exports
```

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| UI says "programmer did not answer" | Wrong COM port or baud rate. Close the serial monitor or any other program using the port. |
| UI says "the programmer runs old firmware" | The ESP8266 still has the 1.x single-family firmware. Upload this one with PlatformIO. |
| Every board fails with "No target" / "No SWD response" | Check that the right **chip family** is selected, then check GND, target power and the wiring. |
| ESP8266 resets when the UI connects | The UI keeps DTR/RTS released. If your USB-UART board still resets, add a 10 µF capacitor from EN to GND. |

Family-specific problems are covered in [docs/mm32](docs/mm32/README.md#troubleshooting), [docs/fortior](docs/fortior/README.md#first-bring-up-checklist) and [docs/renesas](docs/renesas/README.md#troubleshooting).
