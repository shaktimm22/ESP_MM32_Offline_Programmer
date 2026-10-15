# FICE protocol findings

These findings come from `captures/fortior/Session0_full_flash_done_with_gnd_connected.csv.xz`: Keil downloading `fan_white_teak.hex` (15,066 bytes, 0x0000–0x3B0B), sampled at 24 MS/s, with channel 0 = FICEK and channel 1 = FICED.

Decode a capture with:

```
python re/fice_decode.py <capture>.csv[.xz] --hex <fw>.hex -o ops.txt
```

**Verified:** rebuilding the image from the decoded writes gives every one of the 15,066 bytes with 0 mismatches. The 16-bit CRC the chip reports equals CRC-16/CCITT-FALSE of the image.

The first captures, made without a proper GND connection, had glitches on about 25% of clock cycles. Always connect the analyzer's GND at the target.

## Physical layer

- **FICEK** (the chip's RSTN pin) is driven by the programmer and idles high.
- **FICED** carries data in both directions.
- Times below are in samples at 24 MHz (1 sample ≈ 41.7 ns).

| Cell | FICEK low | FICEK high | FICED |
|---|---|---|---|
| **Sync** (starts every frame) | 78 (3.25 µs) | about 52 | The programmer sets the first bit about 19 samples (0.8 µs) after FICEK rises |
| **Write bit** | 44 (1.83 µs); slower variants 50 and 60 | about 19 | The programmer changes it about 16 samples (0.7 µs) after FICEK falls; the chip samples it on FICEK's rising edge |
| **Fast bit** | 12–13 (0.5 µs) | 18–30 | Used for reads, acknowledge polling and the first bits of a select. The chip changes FICED within about 1 sample after FICEK rises; the programmer samples it before the next rising edge |

## Frames (all values LSB first)

```
SEL reg   : SYNC, 1, 1, reg[0..8]            (9-bit register number)
WR  byte  : SYNC, 1, 0,0,0, byte[0..7], then fast clocks until the chip drives FICED=1 (ack)
RD        : SYNC, 0, fast clocks: the chip sends 0s, then a 1 (start bit), then byte[0..7]
```

- The acknowledge wait is about 2 clocks for registers 3 and 4, and about 4 for registers 2, 5 and 436.
- Programming a non-zero flash byte takes about 29 clocks (about 50 µs).
- An erase takes up to 207k clocks (0.38 s).
- With no chip connected, reads return all 0s and the acknowledge never comes. That gives "no target" detection.

## Registers

| Reg | Use |
|---|---|
| 0 | Read-only, returns **0x68** (probably the chip ID) |
| 1 | Read-only, returns 0x02 |
| 2 | Command / status. Status 0x83 = idle/ready; 0x9F, 0x98 and 0x0D appear after commands |
| 3 | Control, read/write. Values seen: 0x00 → 0x10 → 0x98 (0x98 before erase and program) |
| 4 | Write-only. Values seen: F9; F1 F5 E5 |
| 5 | Written with 01 before every command |
| 436 (0x1B4) | Data / argument port |

## Command sequences

Every command is preceded by:

```
SEL 3, RD, WR 10 | SEL 5, WR 01 | SEL 2, WR 02, WR 01, RD (=0x83)
```

| Operation | Sequence after the prefix |
|---|---|
| XDATA access | `SEL 2, WR 11 \| SEL 436, WR addrH, addrL, count \| SEL 2, RD (0x9F) \| SEL 436, WR value` or `RD value` |
| SFR write | `SEL 2, WR 22 \| SEL 436, WR sfr, 01 \| SEL 2, RD (0x98) \| SEL 436, WR value` |
| Erase 1 (0.38 s) | `SEL 436, WR 07, RD (0x0D), WR CB, B3, C3, FF` (then wait for the acknowledge) |
| Erase 2 (0.15 s) | `SEL 436, WR 04, RD (0x0D), WR AE, B1, C1, FF` |
| Program | `SEL 436, WR 02, RD (0x0D), WR addrH, addrL, count, data × count` |

Program details:
- One burst per 128-byte sector, aligned to the sector.
- Address runs that contain only zeros are skipped.
- Zero bytes inside a run are still sent, but they acknowledge at once.

## Full download sequence (Keil)

1. **Power-up / entry.** FICEK and FICED are both low until FICEK rises (target powered or reset released?). FICED goes high for 370 µs, then after 58 ms there is one 1.75 µs low pulse on FICEK. Frames start 14.6 ms later. *This needs a capture with VDD on a channel to confirm.*
2. **Identify and setup.**
   - Read registers 0 and 1.
   - SFR write 0x86 = 0x90, then 0x62, then 0x21.
   - XDATA read of 0x400D.
   - Register 3 = 0x98; register 4 = F9.
3. **Erase 1, then Erase 2.**
4. **Setup again.** Registers 3/4 = F1 F5 E5; register 2 = 02 01 44; XDATA writes of 0x4018–0x401E (CCFG). Then program all bursts. The programmer also writes 4 bytes at **0x3FFC–0x3FFF = 00 F0 00 FF**, which are not in the hex and are probably Keil's option settings.
5. **CRC verify** using the chip's CRC unit, through XDATA writes:
   1. 0x4022 = 0x0C
   2. 0x4024 = 0x00 (first sector)
   3. 0x4025 = 0x7F (sector count − 1)
   4. 0x4022 = 0x02 (start)
   5. Read 0x4022 until it returns 0x10
   6. 0x4022 = 0x00, read 0x4023 → CRC low byte
   7. 0x4022 = 0x01, read 0x4023 → CRC high byte

   The result is CRC-16/CCITT-FALSE (initial value 0xFFFF, polynomial 0x1021) over 0x0000–0x3FFF.
6. **Finish:** register 3 = 0x10, register 5 = 0x10 (probably release / run).

## Option bytes (0x3FFC–0x3FFF)

Found with captures in `captures/fortior/with_lv_*`. Each capture decodes with 0 mismatches, and the chip's CRC equals the computed CRC.

| Keil setting | 3FFC | 3FFD | 3FFE | 3FFF | Chip CRC |
|---|---|---|---|---|---|
| LVR 3.8 V | 00 | F0 | 00 | FF | BEBB |
| LVR 3.0 V | 00 | B0 | 00 | FF | A316 |
| LVR 3.8 V + watchdog | 00 | F0 | 20 | FF | B85D |

- Before programming, 0x3FFC, 0x3FFD and 0x3FFE are also written live to XDATA CCFG 0x401C, 0x401D and 0x401E (with the SFR 0x86 wrapper).
- 0x401E is CCFG1: bit 5 = WDT_EN, bit 6 = LVWIE, bit 7 = LVDENB.
- 0x401D is CCFG2: bits 7:6 = LVRSEL, bit 5 = WDTBTEN, bit 4 = WDTRSTEN, bits 1:0 = LVWSEL.
- Keil's "3.0 V" setting gives LVRSEL = `10`, while the FU6862 datasheet table lists `10` as 3.5 V. Trust the captures.

## Entry

- The emulator holds FICEK low (the chip in reset) between sessions. That is why every capture starts low until Download is pressed.
- A session starts with a 42 µs reset pulse on FICEK, then a 12.6–14.8 ms wait before the first frame. Keil re-enters the same way twice after the erase.

## Still open

- **Encryption (code protection).** Download once with *Encrypt Enable* turned on.
- **Purpose of the post-erase block** (`SEL 4: F1 F5 E5`, `SEL 436: A0 AA A2 50 00 FF`). The firmware replays it unchanged.
