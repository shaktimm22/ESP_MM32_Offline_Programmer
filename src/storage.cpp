#include "storage.h"

#include <LittleFS.h>

#include "mm32.h"

namespace storage {

ImageMeta meta;
Config cfg;
Stats stats;

static constexpr const char *CFG_PATH = "/config.txt";
static constexpr const char *STATS_PATH = "/stats.txt";
static constexpr const char *CHIPS_PATH = "/chips.txt";

// Per-family image files: /<family>.bin + /<family>.meta
static const char *const IMG_PATHS[FAMILY_COUNT] = {"/mm32.bin", "/fortior.bin"};
static const char *const META_PATHS[FAMILY_COUNT] = {"/mm32.meta", "/fortior.meta"};

const char *imgPath() { return IMG_PATHS[cfg.family]; }

uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t len) {
  crc = ~crc;  // zlib-compatible (matches Python zlib.crc32)
  while (len--) {
    crc ^= *data++;
    for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320 & (0 - (crc & 1)));
  }
  return ~crc;
}

bool parseOpt(const char *hex, uint8_t opt[4]) {
  if (strlen(hex) != 8) return false;
  for (int i = 0; i < 4; i++) {
    char b[3] = {hex[2 * i], hex[2 * i + 1], 0};
    char *end;
    opt[i] = strtoul(b, &end, 16);
    if (*end) return false;
  }
  return true;
}

String optString(const uint8_t opt[4]) {
  char buf[9];
  snprintf(buf, sizeof(buf), "%02X%02X%02X%02X", opt[0], opt[1], opt[2], opt[3]);
  return buf;
}

// Calls fn(key, value) for every "key=value" line of a file.
template <typename F>
static void readKv(const char *path, F fn) {
  File f = LittleFS.open(path, "r");
  if (!f) return;
  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    int eq = line.indexOf('=');
    if (eq <= 0) continue;
    fn(line.substring(0, eq), line.substring(eq + 1));
  }
}

static uint32_t parseNum(const String &s) { return strtoul(s.c_str(), nullptr, 0); }

void loadMeta() {
  meta = ImageMeta();
  readKv(META_PATHS[cfg.family], [](const String &k, const String &v) {
    if (k == "size") meta.size = parseNum(v);
    else if (k == "crc") meta.crc = parseNum(v);
    else if (k == "name") strlcpy(meta.name, v.c_str(), sizeof(meta.name));
    else if (k == "base") meta.base = parseNum(v);
    else if (k == "target") strlcpy(meta.target, v.c_str(), sizeof(meta.target));
    else if (k == "opt") parseOpt(v.c_str(), meta.opt);
  });
  File f = LittleFS.open(imgPath(), "r");
  meta.valid = f && meta.size > 0 && f.size() == meta.size;
}

bool setConfig(const char *key, const char *value) {
  String k(key);
  uint32_t n = strtoul(value, nullptr, 0);
  if (k == "family") {
    for (uint8_t i = 0; i < FAMILY_COUNT; i++) {
      if (strcasecmp(value, FAMILY_NAMES[i]) == 0) {
        cfg.family = i;
        return true;
      }
    }
    return false;
  }
  if (k == "mode") cfg.mode = (strcmp(value, "auto") == 0 || n == 1) ? MODE_AUTO : MODE_BUTTON;
  else if (k == "verify") cfg.verify = n != 0;
  else if (k == "run") cfg.run = n != 0;
  else if (k == "settle") cfg.settleMs = n;
  else if (k == "erase") cfg.erase = (strcmp(value, "pages") == 0 || n == 1) ? ERASE_PAGES : ERASE_CHIP;
  else if (k == "protect") cfg.protect = n != 0;
  else if (k == "swdelay") cfg.swdDelay = n;
  else if (k == "speed") cfg.speed = constrain(n, 50, 1000);
  else return false;
  return true;
}

String configString() {
  String s;
  s += "family=";
  s += FAMILY_NAMES[cfg.family];
  s += " mode=";
  s += cfg.mode == MODE_AUTO ? "auto" : "button";
  s += " verify=" + String(cfg.verify);
  s += " run=" + String(cfg.run);
  s += " settle=" + String(cfg.settleMs);
  s += " erase=";
  s += cfg.erase == ERASE_PAGES ? "pages" : "chip";
  s += " protect=" + String(cfg.protect);
  s += " swdelay=" + String(cfg.swdDelay);
  s += " speed=" + String(cfg.speed);
  return s;
}

static void loadConfig() {
  cfg = Config();
  readKv(CFG_PATH, [](const String &k, const String &v) { setConfig(k.c_str(), v.c_str()); });
}

static void loadStats() {
  stats = Stats();
  readKv(STATS_PATH, [](const String &k, const String &v) {
    if (k == "pass") stats.pass = parseNum(v);
    else if (k == "fail") stats.fail = parseNum(v);
  });
}

// chips.txt lines: 0xDEVID=NAME,FLASH_KB,PAGE_SIZE
static void loadChips() {
  mm32::clearCustomChips();
  readKv(CHIPS_PATH, [](const String &k, const String &v) {
    mm32::Chip c{};
    c.devId = parseNum(k);
    int c1 = v.indexOf(','), c2 = v.indexOf(',', c1 + 1);
    if (c1 <= 0) return;
    strlcpy(c.name, v.substring(0, c1).c_str(), sizeof(c.name));
    c.flashKB = parseNum(v.substring(c1 + 1, c2 > 0 ? c2 : v.length()));
    c.pageSize = c2 > 0 ? parseNum(v.substring(c2 + 1)) : 1024;
    if (c.devId && c.flashKB) mm32::addChip(c);
  });
}

// Firmware 1.x kept a single image in /fw.bin + /fw.meta. Move it to its family's slot
// (a Fortior image's metadata has option bytes) and make that family the active one.
static void migrateSingleImage() {
  if (!LittleFS.exists("/fw.bin")) return;
  bool fortior = false;
  readKv("/fw.meta", [&](const String &k, const String &) { fortior |= k == "opt"; });
  uint8_t fam = fortior ? FAMILY_FORTIOR : FAMILY_MM32;
  LittleFS.remove(IMG_PATHS[fam]);
  LittleFS.remove(META_PATHS[fam]);
  LittleFS.rename("/fw.bin", IMG_PATHS[fam]);
  LittleFS.rename("/fw.meta", META_PATHS[fam]);
  cfg.family = fam;
  saveConfig();
}

bool begin() {
  if (!LittleFS.begin()) {
    LittleFS.format();
    if (!LittleFS.begin()) return false;
  }
  loadConfig();
  migrateSingleImage();
  loadStats();
  loadChips();
  loadMeta();
  return true;
}

void saveMeta() {
  File f = LittleFS.open(META_PATHS[cfg.family], "w");
  f.printf("size=%u\ncrc=0x%08X\nname=%s\nbase=0x%08X\ntarget=%s\nopt=%s\n", meta.size, meta.crc, meta.name,
           meta.base, meta.target, optString(meta.opt).c_str());
}

void saveConfig() {
  File f = LittleFS.open(CFG_PATH, "w");
  String s = configString();
  s.replace(' ', '\n');
  f.println(s);
}

void saveStats() {
  File f = LittleFS.open(STATS_PATH, "w");
  f.printf("pass=%u\nfail=%u\n", stats.pass, stats.fail);
}

void saveChips() {
  File f = LittleFS.open(CHIPS_PATH, "w");
  for (size_t i = 0; i < mm32::chipCount(); i++) {
    if (!mm32::isPersisted(i)) continue;
    const mm32::Chip &c = mm32::chipAt(i);
    f.printf("0x%08X=%s,%u,%u\n", c.devId, c.name, c.flashKB, c.pageSize);
  }
}

uint32_t fsFree() {
  FSInfo info;
  LittleFS.info(info);
  return info.totalBytes - info.usedBytes;
}

}  // namespace storage
