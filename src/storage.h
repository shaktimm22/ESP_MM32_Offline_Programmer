#pragma once
// LittleFS-backed persistence: one firmware image (+ metadata) per chip family, settings,
// counters, custom MM32 chips.
#include <Arduino.h>

namespace storage {

// Chip families; the order matches family::all() and the names used by "SET family".
enum Family : uint8_t { FAMILY_MM32 = 0, FAMILY_FORTIOR = 1, FAMILY_COUNT };
static constexpr const char *FAMILY_NAMES[FAMILY_COUNT] = {"mm32", "fortior"};

static constexpr const char *IMG_TMP = "/upload.tmp";

struct ImageMeta {
  bool valid = false;
  uint32_t size = 0;
  uint32_t crc = 0;  // CRC-32 (zlib) of the uploaded file
  char name[48] = "";
  // MM32
  uint32_t base = 0x08000000;
  char target[20] = "ANY";  // chip name the image is meant for, or ANY
  // Fortior: option bytes at 0x3FFC..0x3FFF (LVR, WDT, ...)
  uint8_t opt[4] = {0x00, 0xF0, 0x00, 0xFF};
};

enum Mode : uint8_t { MODE_BUTTON = 0, MODE_AUTO = 1 };
enum Erase : uint8_t { ERASE_CHIP = 0, ERASE_PAGES = 1 };

struct Config {
  uint8_t family = FAMILY_MM32;
  uint8_t mode = MODE_BUTTON;
  bool verify = true;
  bool run = true;          // reset target and run after programming
  uint32_t settleMs = 500;  // auto mode: wait after target detected
  // MM32
  uint8_t erase = ERASE_CHIP;
  bool protect = false;   // enable read-out protection after programming
  uint32_t swdDelay = 4;  // 0 is too fast for some boards; attach/probe slow down further if needed
  // Fortior
  uint16_t speed = 100;  // FICE timing scale in %, 100 = Fortior emulator timing
};

struct Stats {
  uint32_t pass = 0;
  uint32_t fail = 0;
};

extern ImageMeta meta;  // image of the active family (cfg.family)
extern Config cfg;
extern Stats stats;

bool begin();
void loadMeta();  // (re)load meta for cfg.family
void saveMeta();
void saveConfig();
void saveStats();
void saveChips();
uint32_t fsFree();
const char *imgPath();  // stored image of the active family

// key/value config setter used by the SET command; returns false on unknown key or value
bool setConfig(const char *key, const char *value);
String configString();

bool parseOpt(const char *hex, uint8_t opt[4]);  // "00F000FF" -> bytes
String optString(const uint8_t opt[4]);

uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t len);

}  // namespace storage
