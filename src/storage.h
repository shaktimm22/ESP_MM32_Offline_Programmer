#pragma once
// LittleFS-backed persistence: firmware image, its metadata, settings, counters, custom chips.
#include <Arduino.h>

namespace storage {

static constexpr const char *IMG_PATH = "/fw.bin";
static constexpr const char *IMG_TMP = "/fw.tmp";

struct ImageMeta {
  bool valid = false;
  uint32_t size = 0;
  uint32_t crc = 0;
  uint32_t base = 0x08000000;
  char target[20] = "ANY";  // chip name the image is meant for, or ANY
  char name[48] = "";
};

enum Mode : uint8_t { MODE_BUTTON = 0, MODE_AUTO = 1 };
enum Erase : uint8_t { ERASE_CHIP = 0, ERASE_PAGES = 1 };

struct Config {
  uint8_t mode = MODE_BUTTON;
  uint8_t erase = ERASE_CHIP;
  bool verify = true;
  bool protect = false;  // enable read-out protection after programming
  bool run = true;       // reset target and run after programming
  uint32_t swdDelay = 4;  // 0 is too fast for some boards; attach/probe slow down further if needed
  uint32_t settleMs = 500;  // auto mode: wait after target detected
};

struct Stats {
  uint32_t pass = 0;
  uint32_t fail = 0;
};

extern ImageMeta meta;
extern Config cfg;
extern Stats stats;

bool begin();
void saveMeta();
void saveConfig();
void saveStats();
void saveChips();
uint32_t fsFree();

// key/value config setter used by the SET command; returns false on unknown key
bool setConfig(const char *key, const char *value);
String configString();

uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t len);

}  // namespace storage
