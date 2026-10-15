#pragma once
// Fortior FU68xx (8051, 16 KB flash) programming over FICE.
// The command sequences replay what Keil + the Fortior emulator send (re/FINDINGS.md).
#include <Arduino.h>
#include <FS.h>

namespace fu68 {

static constexpr uint32_t FLASH_SIZE = 0x4000;
static constexpr uint32_t OPT_ADDR = 0x3FFC;  // 4 option bytes Keil appends (LVR, WDT, ...)
static constexpr uint8_t CHIP_ID = 0x68;      // FICE register 0

struct Target {
  int id0 = -1, id1 = -1, id2 = -1;  // FICE reg 0, reg 1, XDATA 0x400D
};

typedef void (*ProgressFn)(const char *stage, uint8_t pct);

// All operations return nullptr on success or an error message.
const char *attach(Target &t);  // reset, enter debug mode, read the chip IDs
const char *erase();            // full erase (both erase commands Keil uses)
const char *writeConfig(const uint8_t opt[4]);  // load option bytes into CCFG (0x401C..0x401E)
const char *program(File &img, const uint8_t opt[4], ProgressFn cb);
const char *readCrc(uint16_t *crc);  // the chip's own CRC-16 over the whole 16 KB
void finish(bool run);  // end the session; run: reset and let the firmware start
bool probe();           // quick check: does a FU68xx answer?

uint16_t crc16(const uint8_t *data, size_t len, uint16_t crc = 0xFFFF);  // CRC-16/CCITT-FALSE
// Expected chip CRC for a stored image + option bytes.
uint16_t imageCrc(File &img, const uint8_t opt[4]);

// Raw FICE operations for exploring the protocol, e.g. "A P D15 S3 R W10 S0 R".
String script(const char *ops);

}  // namespace fu68
