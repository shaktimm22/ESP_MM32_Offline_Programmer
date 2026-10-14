// MM32 offline programmer for ESP8266.
// The PC UI talks to this over UART with a line protocol (see README.md):
// every reply starts with "@OK" / "@ERR", asynchronous events with "@EVT".
#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <LittleFS.h>

#include "mm32.h"
#include "pins.h"
#include "storage.h"
#include "swd.h"

#ifndef SERIAL_BAUD
#define SERIAL_BAUD 460800
#endif

using storage::cfg;
using storage::meta;
using storage::stats;

// ---------------------------------------------------------------- indicators

static void ledBusy(bool on) { digitalWrite(PIN_LED_BUSY, on ? LOW : HIGH); }

static void ledResult(int result) {  // 1 pass, 0 fail, -1 off
  digitalWrite(PIN_LED_PASS, result == 1);
  digitalWrite(PIN_LED_FAIL, result == 0);
}

static void beep(uint32_t ms, uint8_t times) {
#if PIN_BUZZER >= 0
  for (uint8_t i = 0; i < times; i++) {
    digitalWrite(PIN_BUZZER, HIGH);
    delay(ms);
    digitalWrite(PIN_BUZZER, LOW);
    if (i + 1 < times) delay(ms);
  }
#else
  (void)ms;
  (void)times;
#endif
}

// ---------------------------------------------------------------- events

static void evtCount() { Serial.printf("@EVT COUNT %u %u\n", stats.pass, stats.fail); }

static void evtState(const char *s) { Serial.printf("@EVT STATE %s\n", s); }

static void onProgress(const char *stage, uint8_t pct) {
  static uint32_t lastMs = 0;
  if (pct == 100 || millis() - lastMs > 150) {
    lastMs = millis();
    Serial.printf("@EVT PROGRESS %s %u\n", stage, pct);
  }
  ledBusy((millis() / 100) & 1);
}

// ---------------------------------------------------------------- flashing

static bool s_busy = false;

static const char *checkTarget(const mm32::Target &t, char *buf, size_t len) {
  if (!t.chip) {
    snprintf(buf, len, "Unknown chip ID 0x%08X - add it in the UI (Chips)", t.devId);
    return buf;
  }
  if (strcasecmp(meta.target, "ANY") != 0 && strcasecmp(meta.target, t.chip->name) != 0) {
    snprintf(buf, len, "Chip is %s but image is for %s", t.chip->name, meta.target);
    return buf;
  }
  uint32_t flashEnd = mm32::FLASH_BASE + (uint32_t)t.chip->flashKB * 1024;
  if (meta.base < mm32::FLASH_BASE || meta.base + meta.size > flashEnd) {
    snprintf(buf, len, "Image 0x%08X+%u does not fit %s (%uKB)", meta.base, meta.size, t.chip->name,
             t.chip->flashKB);
    return buf;
  }
  if (t.readProtected) return "Chip is read-protected - use Unlock in the UI first";
  return nullptr;
}

static void runFlash() {
  if (s_busy) return;
  s_busy = true;
  uint32_t t0 = millis();
  ledResult(-1);
  ledBusy(true);

  char msg[96];
  const char *err = nullptr;
  bool counted = true;  // "no target" is not counted as a failed device
  mm32::Target t;

  File img;
  if (!meta.valid) {
    err = "No firmware image stored - upload one first";
    counted = false;
  } else {
    evtState("connect");
    err = mm32::attach(t);
    if (err) counted = false;
  }
  if (!err) err = checkTarget(t, msg, sizeof(msg));
  if (!err) {
    Serial.printf("@EVT CHIP %s 0x%08X\n", t.chip->name, t.devId);
    evtState("erase");
    err = cfg.erase == storage::ERASE_PAGES ? mm32::pageErase(meta.base, meta.size, t.chip->pageSize)
                                            : mm32::massErase();
  }
  if (!err) {
    img = LittleFS.open(storage::IMG_PATH, "r");
    if (!img) err = "Cannot open stored image";
  }
  if (!err) {
    evtState("program");
    err = mm32::program(img, meta.base, meta.size, onProgress);
  }
  if (!err && cfg.verify) {
    evtState("verify");
    err = mm32::verify(img, meta.base, meta.size, onProgress);
  }
  if (img) img.close();
  if (!err && cfg.protect) {
    evtState("protect");
    err = mm32::setReadProtection();
  }
  if (!err && cfg.run) mm32::resetAndRun();
  swd::release();
  swd::setDelay(cfg.swdDelay);

  uint32_t ms = millis() - t0;
  ledBusy(false);
  if (!err) {
    stats.pass++;
    storage::saveStats();
    Serial.printf("@EVT RESULT PASS %u %s 0x%08X\n", ms, t.chip->name, t.devId);
    ledResult(1);
    beep(80, 1);
  } else {
    if (counted) {
      stats.fail++;
      storage::saveStats();
    }
    Serial.printf("@EVT RESULT FAIL %d %s\n", counted ? 1 : 0, err);
    ledResult(0);
    beep(150, 3);
  }
  evtCount();
  evtState("idle");
  s_busy = false;
}

// ---------------------------------------------------------------- auto detect

enum AutoState { AUTO_ARMED, AUTO_SETTLING, AUTO_WAIT_REMOVAL };
static AutoState s_auto = AUTO_ARMED;
static uint32_t s_autoTimer = 0;
static uint8_t s_autoHits = 0;

static void pollAuto() {
  if (cfg.mode != storage::MODE_AUTO || s_busy) return;
  if (millis() - s_autoTimer < 200) return;
  bool present = mm32::probe();

  switch (s_auto) {
    case AUTO_ARMED:
      s_autoHits = present ? s_autoHits + 1 : 0;
      if (s_autoHits >= 2) {
        Serial.println("@EVT TARGET present");
        s_auto = AUTO_SETTLING;
        s_autoTimer = millis();
        return;
      }
      break;
    case AUTO_SETTLING:
      if (millis() - s_autoTimer < cfg.settleMs) return;
      runFlash();
      s_auto = AUTO_WAIT_REMOVAL;
      s_autoHits = 0;
      break;
    case AUTO_WAIT_REMOVAL:
      s_autoHits = present ? 0 : s_autoHits + 1;
      if (s_autoHits >= 3) {  // gone for ~600 ms
        Serial.println("@EVT TARGET absent");
        s_auto = AUTO_ARMED;
        s_autoHits = 0;
      }
      break;
  }
  s_autoTimer = millis();
}

// ---------------------------------------------------------------- button

static void pollButton() {
  static bool last = true;
  static uint32_t changed = 0;
  bool now = digitalRead(PIN_BUTTON);
  if (now != last && millis() - changed > 30) {
    changed = millis();
    last = now;
    if (!now) runFlash();  // pressed (active low)
  }
}

// ---------------------------------------------------------------- upload

static File s_up;
static uint32_t s_upSize = 0, s_upCrc = 0, s_upRecv = 0;
static storage::ImageMeta s_upMeta;

static int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// ---------------------------------------------------------------- commands

static void reply(const char *cmd, const String &s = String()) {
  Serial.printf("@OK %s%s%s\n", cmd, s.length() ? " " : "", s.c_str());
}
static void replyErr(const char *cmd, const char *msg) { Serial.printf("@ERR %s %s\n", cmd, msg); }

static String infoString() {
  String s;
  s += "fw=" FW_VERSION;
  s += " img=" + String(meta.valid);
  s += " size=" + String(meta.size);
  char buf[64];
  snprintf(buf, sizeof(buf), " crc=0x%08X base=0x%08X", meta.crc, meta.base);
  s += buf;
  s += " target=" + String(meta.target);
  s += " name=" + String(meta.name[0] ? meta.name : "-");
  s += " pass=" + String(stats.pass) + " fail=" + String(stats.fail);
  s += " free=" + String(storage::fsFree());
  s += " " + storage::configString();
  return s;
}

static String chipsString() {
  String s;
  for (size_t i = 0; i < mm32::chipCount(); i++) {
    const mm32::Chip &c = mm32::chipAt(i);
    char buf[64];
    snprintf(buf, sizeof(buf), "%s0x%08X:%s:%u:%u:%c", i ? "," : "", c.devId, c.name, c.flashKB,
             c.pageSize, i < mm32::builtinCount() ? 'b' : 'c');
    s += buf;
  }
  return s;
}

static void finishMaintenance(bool run) {
  if (run) mm32::resetAndRun();
  swd::release();
  swd::setDelay(cfg.swdDelay);
}

static void handleLine(char *line) {
  char *args = strchr(line, ' ');
  if (args) *args++ = 0;
  else args = line + strlen(line);
  String cmd(line);
  cmd.toUpperCase();
  const char *c = cmd.c_str();

  if (cmd == "PING") {
    reply(c, "MM32-FLASHER " FW_VERSION);
  } else if (cmd == "INFO") {
    reply(c, infoString());
  } else if (cmd == "CHIPS") {
    reply(c, chipsString());
  } else if (cmd == "BEGIN") {
    // BEGIN <size> <crc32> <base> <target> <name...>
    char *save;
    char *tSize = strtok_r(args, " ", &save);
    char *tCrc = strtok_r(nullptr, " ", &save);
    char *tBase = strtok_r(nullptr, " ", &save);
    char *tTarget = strtok_r(nullptr, " ", &save);
    char *tName = strtok_r(nullptr, "", &save);
    if (!tSize || !tCrc || !tBase || !tTarget) return replyErr(c, "usage: BEGIN size crc base target name");
    s_upMeta = storage::ImageMeta();
    s_upSize = strtoul(tSize, nullptr, 0);
    s_upMeta.size = s_upSize;
    s_upMeta.crc = strtoul(tCrc, nullptr, 0);
    s_upMeta.base = strtoul(tBase, nullptr, 0);
    strlcpy(s_upMeta.target, tTarget, sizeof(s_upMeta.target));
    strlcpy(s_upMeta.name, tName ? tName : "", sizeof(s_upMeta.name));
    for (char *p = s_upMeta.name; *p; p++)
      if (*p == ' ') *p = '_';
    if (s_upSize == 0 || s_upSize + 8192 > storage::fsFree() + (meta.valid ? meta.size : 0))
      return replyErr(c, "image too large for programmer storage");
    if (s_up) s_up.close();
    s_up = LittleFS.open(storage::IMG_TMP, "w");
    if (!s_up) return replyErr(c, "cannot create file");
    s_upRecv = 0;
    s_upCrc = 0;
    reply(c);
  } else if (cmd == "DATA") {
    // DATA <offset> <hex bytes>
    if (!s_up) return replyErr(c, "no upload in progress");
    char *hex = strchr(args, ' ');
    if (!hex) return replyErr(c, "usage: DATA offset hex");
    *hex++ = 0;
    uint32_t off = strtoul(args, nullptr, 0);
    if (off != s_upRecv) {
      char m[48];
      snprintf(m, sizeof(m), "expected offset %u", s_upRecv);
      return replyErr(c, m);
    }
    static uint8_t buf[300];
    size_t n = 0;
    for (; hex[0] && hex[1] && n < sizeof(buf); hex += 2) {
      int hi = hexNibble(hex[0]), lo = hexNibble(hex[1]);
      if (hi < 0 || lo < 0) return replyErr(c, "bad hex");
      buf[n++] = (hi << 4) | lo;
    }
    if (s_upRecv + n > s_upSize) return replyErr(c, "data beyond declared size");
    if (s_up.write(buf, n) != n) return replyErr(c, "filesystem write failed");
    s_upCrc = storage::crc32Update(s_upCrc, buf, n);
    s_upRecv += n;
    reply(c, String(s_upRecv));
  } else if (cmd == "END") {
    if (!s_up) return replyErr(c, "no upload in progress");
    s_up.close();
    if (s_upRecv != s_upSize || s_upCrc != s_upMeta.crc) {
      LittleFS.remove(storage::IMG_TMP);
      return replyErr(c, "size/CRC mismatch, upload discarded");
    }
    LittleFS.remove(storage::IMG_PATH);
    LittleFS.rename(storage::IMG_TMP, storage::IMG_PATH);
    meta = s_upMeta;
    meta.valid = true;
    storage::saveMeta();
    reply(c, infoString());
  } else if (cmd == "ABORT") {
    if (s_up) s_up.close();
    LittleFS.remove(storage::IMG_TMP);
    reply(c);
  } else if (cmd == "FLASH") {
    reply(c);
    runFlash();
  } else if (cmd == "DETECT") {
    mm32::Target t;
    const char *err = mm32::attach(t);
    if (!err) {
      char buf[160];
      snprintf(buf, sizeof(buf), "dpidr=0x%08X devid=0x%08X chip=%s flash=%u protected=%d swdelay=%u", t.dpidr,
               t.devId, t.chip ? t.chip->name : "UNKNOWN", t.chip ? t.chip->flashKB : 0, t.readProtected,
               t.swdDelay);
      reply(c, buf);
    } else {
      replyErr(c, err);
    }
    finishMaintenance(!err);
  } else if (cmd == "ERASE") {
    mm32::Target t;
    const char *err = mm32::attach(t);
    if (!err) err = mm32::massErase();
    if (err) replyErr(c, err);
    else reply(c);
    finishMaintenance(false);
  } else if (cmd == "UNLOCK") {
    mm32::Target t;
    const char *err = mm32::attach(t);
    if (!err) err = mm32::removeReadProtection();
    if (err) replyErr(c, err);
    else reply(c, "flash erased, power-cycle the target");
    finishMaintenance(false);
  } else if (cmd == "PROTECT") {
    mm32::Target t;
    const char *err = mm32::attach(t);
    if (!err) err = mm32::setReadProtection();
    if (err) replyErr(c, err);
    else reply(c, "power-cycle the target to activate");
    finishMaintenance(false);
  } else if (cmd == "RESET") {
    mm32::Target t;
    const char *err = mm32::attach(t);
    if (err) replyErr(c, err);
    else reply(c);
    finishMaintenance(!err);
  } else if (cmd == "READ") {
    // READ <addr> [words<=32]: dump target memory (debug aid)
    char *end;
    uint32_t addr = strtoul(args, &end, 0) & ~3u;
    uint32_t n = strtoul(end, nullptr, 0);
    n = n ? min<uint32_t>(n, 32) : 8;
    mm32::Target t;
    const char *err = mm32::attach(t);
    static uint32_t words[32];
    if (!err && !swd::readBlock32(addr, words, n)) err = "SWD read failed";
    if (err) {
      replyErr(c, err);
    } else {
      String s;
      char buf[16];
      snprintf(buf, sizeof(buf), "0x%08X:", addr);
      s += buf;
      for (uint32_t i = 0; i < n; i++) {
        snprintf(buf, sizeof(buf), " %08X", words[i]);
        s += buf;
      }
      reply(c, s);
    }
    finishMaintenance(false);
  } else if (cmd == "SET") {
    char *val = strchr(args, ' ');
    if (!val) return replyErr(c, "usage: SET key value");
    *val++ = 0;
    if (!storage::setConfig(args, val)) return replyErr(c, "unknown key");
    storage::saveConfig();
    swd::setDelay(cfg.swdDelay);
    s_auto = AUTO_ARMED;
    s_autoHits = 0;
    reply(c, storage::configString());
  } else if (cmd == "RESETCNT") {
    stats = storage::Stats();
    storage::saveStats();
    reply(c);
    evtCount();
  } else if (cmd == "CHIPADD") {
    // CHIPADD <devid> <name> <flashKB> [pageSize]
    char *save;
    char *tId = strtok_r(args, " ", &save);
    char *tName = strtok_r(nullptr, " ", &save);
    char *tKb = strtok_r(nullptr, " ", &save);
    char *tPage = strtok_r(nullptr, " ", &save);
    if (!tId || !tName || !tKb) return replyErr(c, "usage: CHIPADD devid name flashKB [page]");
    mm32::Chip ch{};
    ch.devId = strtoul(tId, nullptr, 0);
    strlcpy(ch.name, tName, sizeof(ch.name));
    ch.flashKB = strtoul(tKb, nullptr, 0);
    ch.pageSize = tPage ? strtoul(tPage, nullptr, 0) : 1024;
    if (!ch.devId || !ch.flashKB || !ch.pageSize) return replyErr(c, "invalid values");
    mm32::addChip(ch);
    storage::saveChips();
    reply(c, chipsString());
  } else if (cmd == "CHIPDEL") {
    if (!mm32::removeChip(strtoul(args, nullptr, 0))) return replyErr(c, "not a custom chip");
    storage::saveChips();
    reply(c, chipsString());
  } else {
    replyErr(c, "unknown command");
  }
}

static void pollSerial() {
  static char line[700];
  static size_t len = 0;
  static bool overflow = false;
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (overflow) replyErr("LINE", "too long");
      else if (len) {
        line[len] = 0;
        handleLine(line);
      }
      len = 0;
      overflow = false;
    } else if (len < sizeof(line) - 1) {
      line[len++] = ch;
    } else {
      overflow = true;
    }
  }
}

// ---------------------------------------------------------------- main

void setup() {
  WiFi.mode(WIFI_OFF);
  WiFi.forceSleepBegin();

  pinMode(PIN_LED_BUSY, OUTPUT);
  pinMode(PIN_LED_PASS, OUTPUT);
  pinMode(PIN_LED_FAIL, OUTPUT);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
#if PIN_BUZZER >= 0
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
#endif
  ledBusy(false);
  ledResult(-1);

  Serial.setRxBufferSize(1024);
  Serial.begin(SERIAL_BAUD);
  Serial.println();

  if (!storage::begin()) Serial.println("@EVT ERROR filesystem mount failed");
  swd::begin();
  swd::setDelay(cfg.swdDelay);

  Serial.println("@EVT BOOT MM32-FLASHER " FW_VERSION);
  evtCount();
}

void loop() {
  pollSerial();
  pollButton();
  pollAuto();
}
