#pragma once
// MM32 (Cortex-M0, STM32F1-style FPEC) programming over SWD.
#include <Arduino.h>
#include <FS.h>

namespace mm32 {

struct Chip {
  uint32_t devId;     // DBGMCU_IDCODE @ 0x40013400
  char name[20];
  uint16_t flashKB;
  uint16_t pageSize;  // erase granularity in bytes
};

struct Target {
  uint32_t dpidr = 0;
  uint32_t devId = 0;
  const Chip *chip = nullptr;
  bool readProtected = false;
  uint32_t swdDelay = 0;  // SWD delay that worked for this connection
};

typedef void (*ProgressFn)(const char *stage, uint8_t pct);

static constexpr uint32_t FLASH_BASE = 0x08000000;

// Chip table: built-ins plus user-defined entries (see storage).
void addChip(const Chip &c);
bool removeChip(uint32_t devId);
size_t chipCount();
const Chip &chipAt(size_t i);
const Chip *findChip(uint32_t devId);
const Chip *findChipByName(const char *name);
void clearCustomChips();
size_t builtinCount();
bool isPersisted(size_t i);  // custom entry, or built-in whose DEV_ID was learned

// All operations return nullptr on success or an error message.
const char *attach(Target &t);  // connect, reset-halt, identify (SWDIO/SWCLK only)
const char *massErase();
const char *pageErase(uint32_t addr, uint32_t len, uint16_t pageSize);
const char *program(File &img, uint32_t base, uint32_t size, ProgressFn cb);
const char *verify(File &img, uint32_t base, uint32_t size, ProgressFn cb);
const char *setReadProtection();
const char *removeReadProtection();  // triggers a mass erase; needs power cycle
void resetAndRun();  // software reset (AIRCR.SYSRESETREQ) and let the target run
bool probe();  // quick check: does anything answer on SWD?

}  // namespace mm32
