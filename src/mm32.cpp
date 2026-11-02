#include "mm32.h"

#include <stdarg.h>

#include <vector>

#include "swd.h"

namespace mm32 {

// Cortex-M0 debug registers
static constexpr uint32_t DHCSR = 0xE000EDF0;
static constexpr uint32_t DEMCR = 0xE000EDFC;
static constexpr uint32_t AIRCR = 0xE000ED0C;
static constexpr uint32_t DBGKEY = 0xA05F0000;
static constexpr uint32_t C_DEBUGEN = 1u << 0;
static constexpr uint32_t C_HALT = 1u << 1;
static constexpr uint32_t S_HALT = 1u << 17;
static constexpr uint32_t VC_CORERESET = 1u << 0;
static constexpr uint32_t AIRCR_SYSRESET = 0x05FA0004;

// MM32 flash controller (FPEC), same on F0010 / F0020 (UM section 2.6)
static constexpr uint32_t FLASH_R = 0x40022000;
static constexpr uint32_t FLASH_ACR = FLASH_R + 0x00;
static constexpr uint32_t FLASH_KEYR = FLASH_R + 0x04;
static constexpr uint32_t FLASH_OPTKEYR = FLASH_R + 0x08;
static constexpr uint32_t FLASH_SR = FLASH_R + 0x0C;
static constexpr uint32_t FLASH_CR = FLASH_R + 0x10;
static constexpr uint32_t FLASH_AR = FLASH_R + 0x14;
static constexpr uint32_t FLASH_OBR = FLASH_R + 0x1C;
static constexpr uint32_t KEY1 = 0x45670123;
static constexpr uint32_t KEY2 = 0xCDEF89AB;
static constexpr uint32_t SR_BSY = 1u << 0;
static constexpr uint32_t SR_PGERR = 1u << 2;
static constexpr uint32_t SR_WRPRTERR = 1u << 4;
static constexpr uint32_t SR_EOP = 1u << 5;
static constexpr uint32_t CR_PG = 1u << 0;
static constexpr uint32_t CR_PER = 1u << 1;
static constexpr uint32_t CR_MER = 1u << 2;
static constexpr uint32_t CR_OPTPG = 1u << 4;
static constexpr uint32_t CR_OPTER = 1u << 5;
static constexpr uint32_t CR_STRT = 1u << 6;
static constexpr uint32_t CR_LOCK = 1u << 7;
static constexpr uint32_t CR_OPTWRE = 1u << 9;
static constexpr uint32_t OBR_RDPRT = 1u << 1;
static constexpr uint32_t ACR_PRFTBE = 1u << 4;
static constexpr uint32_t OPT_RDP_ADDR = 0x1FFFF800;
static constexpr uint32_t DEV_ID_ADDR = 0x40013400;
static constexpr uint32_t UID_ADDR = 0x1FFFF7E8;  // 96-bit unique device ID

// IDs from the MM32F0010 / MM32F0020 user manuals (DBG chapter).
// devId 0 = flash geometry known from the datasheet, DEV_ID not yet known: the PC UI
// "learns" it from the first real chip (CHIPADD with the same name fills it in).
static const Chip BUILTIN[] = {
    {0xCC4350D1, "MM32F0010", 16, 1024},
    {0x4C50F800, "MM32F0020", 32, 1024},
    {0, "MM32F0140", 64, 1024},     // DS_MM32F0140: 64KB flash, 1KB page
    {0, "MM32SPIN160C", 32, 1024},  // DS_MM32SPIN160C: 32KB flash, 1KB page
};
static std::vector<Chip> s_chips(std::begin(BUILTIN), std::end(BUILTIN));

static char s_err[160];

static const char *fail(const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(s_err, sizeof(s_err), fmt, ap);
  va_end(ap);
  return s_err;
}

// ---------------------------------------------------------------- chip table

size_t builtinCount() { return sizeof(BUILTIN) / sizeof(BUILTIN[0]); }
size_t chipCount() { return s_chips.size(); }
const Chip &chipAt(size_t i) { return s_chips[i]; }

void clearCustomChips() { s_chips.assign(std::begin(BUILTIN), std::end(BUILTIN)); }

// Built-in entry whose DEV_ID was not in the datasheet (filled in at runtime).
static bool isPendingBuiltin(size_t i) { return i < builtinCount() && BUILTIN[i].devId == 0; }

bool isPersisted(size_t i) {
  return i >= builtinCount() || (isPendingBuiltin(i) && s_chips[i].devId != 0);
}

void addChip(const Chip &c) {
  for (auto &e : s_chips) {
    if (e.devId == c.devId) {
      e = c;
      return;
    }
  }
  for (size_t i = 0; i < builtinCount(); i++) {
    if (isPendingBuiltin(i) && strcasecmp(s_chips[i].name, c.name) == 0) {
      s_chips[i] = c;  // learn the DEV_ID of a built-in part
      return;
    }
  }
  s_chips.push_back(c);
}

bool removeChip(uint32_t devId) {
  for (size_t i = 0; i < s_chips.size(); i++) {
    if (!devId || s_chips[i].devId != devId) continue;
    if (i >= builtinCount()) {
      s_chips.erase(s_chips.begin() + i);
      return true;
    }
    if (isPendingBuiltin(i)) {
      s_chips[i] = BUILTIN[i];  // forget the learned ID
      return true;
    }
  }
  return false;
}

const Chip *findChip(uint32_t devId) {
  if (!devId) return nullptr;
  for (auto &e : s_chips)
    if (e.devId == devId) return &e;
  return nullptr;
}

const Chip *findChipByName(const char *name) {
  for (auto &e : s_chips)
    if (strcasecmp(e.name, name) == 0) return &e;
  return nullptr;
}

// ---------------------------------------------------------------- helpers

static const char *waitBusy(uint32_t timeoutMs, uint32_t *srOut = nullptr) {
  uint32_t t0 = millis();
  uint32_t sr;
  for (;;) {
    if (!swd::read32(FLASH_SR, &sr)) return "Lost target while waiting for flash";
    if (!(sr & SR_BSY)) break;
    if (millis() - t0 > timeoutMs) return "Flash operation timed out";
    yield();
  }
  if (srOut) *srOut = sr;
  if (sr & SR_WRPRTERR) return "Flash is write-protected (WRPRTERR)";
  return nullptr;
}

static const char *unlockFlash() {
  uint32_t cr;
  if (!swd::read32(FLASH_CR, &cr)) return "Cannot read FLASH_CR";
  if (cr & CR_LOCK) {
    swd::write32(FLASH_KEYR, KEY1);
    swd::write32(FLASH_KEYR, KEY2);
    if (!swd::read32(FLASH_CR, &cr) || (cr & CR_LOCK)) return "Flash unlock failed";
  }
  if (const char *e = waitBusy(100)) return e;
  swd::write32(FLASH_SR, SR_EOP | SR_WRPRTERR | SR_PGERR);  // clear flags
  return nullptr;
}

static void lockFlash() { swd::write32(FLASH_CR, CR_LOCK); }

static bool waitHalted() {
  for (int i = 0; i < 50; i++) {
    uint32_t v;
    if (swd::read32(DHCSR, &v) && (v & S_HALT)) return true;
    delay(2);
  }
  return false;
}

// ---------------------------------------------------------------- operations

bool probe() {
  // Same speed fallback as attach(): some boards only answer with a slower SWD clock.
  // Start with the speed that worked last time so a present board is seen in one try.
  static uint32_t lastGood = 0;
  const uint32_t base = swd::getDelay();
  const uint32_t tries[] = {max(base, lastGood), base, 4, 16, 64};
  uint32_t id;
  bool ok = false;
  for (uint32_t d : tries) {
    if (d < base) continue;
    swd::setDelay(d);
    if (swd::connect(&id, false)) {
      ok = true;
      lastGood = d;
      break;
    }
  }
  swd::setDelay(base);
  swd::release();
  return ok;
}

const char *attach(Target &t) {
  t = Target();
  // 4-wire connection (no NRST). Start at the configured clock and slow down
  // on failure (long wires, weak 3.3V levels on a 5V target, slow fixtures).
  const uint32_t base = swd::getDelay();
  const uint32_t fallback[] = {base, 4, 16, 64, 250};
  bool ok = false;
  uint8_t ack = 0;
  for (uint32_t d : fallback) {
    if (d < base) continue;
    swd::setDelay(d);
    for (int i = 0; i < 2 && !ok; i++) {
      ok = swd::connect(&t.dpidr);
      if (!ok) {
        ack = swd::lastAck();
        delay(5);
      }
    }
    if (ok) {
      t.swdDelay = d;
      break;
    }
  }
  if (!ok) {
    swd::setDelay(base);
    bool idleHigh = swd::sampleSwdio();
    if (ack == 7 && idleHigh)
      return fail("No SWD response: SWDIO idles high, no reply (target unpowered, no GND, "
                  "or SWDIO/SWCLK swapped)");
    if (ack == 0 && !idleHigh)
      return fail("No SWD response: SWDIO is held LOW (short to GND, or target firmware "
                  "uses PA14 as an output)");
    return fail("Garbled SWD reply (ack=%u, SWDIO idle %c): check 3.3V levels/5V target, wires",
                ack, idleHigh ? (uint32_t)'H' : (uint32_t)'L');
  }

  swd::write32(DHCSR, DBGKEY | C_HALT | C_DEBUGEN);
  swd::write32(DEMCR, VC_CORERESET);  // halt on the reset vector
  // Software system reset so peripherals/clocks are back to defaults (HSI) before flashing.
  swd::write32(AIRCR, AIRCR_SYSRESET);
  delay(20);
  swd::clearErrors();

  if (!waitHalted()) {
    uint32_t id;
    swd::connect(&id);
    swd::write32(DHCSR, DBGKEY | C_HALT | C_DEBUGEN);
    if (!waitHalted()) return "Could not halt the core";
  }
  swd::write32(DEMCR, 0);

  if (!swd::read32(DEV_ID_ADDR, &t.devId)) t.devId = 0;
  t.chip = findChip(t.devId);

  // Factory unique ID (MM32F0010 / F0020 user manuals: 3 words at 0x1FFFF7E8). Erased-looking
  // values mean the part has none or the read did not work.
  uint32_t uid[3];
  if (swd::readBlock32(UID_ADDR, uid, 3) && !(uid[0] == uid[1] && uid[1] == uid[2] &&
                                              (uid[0] == 0 || uid[0] == 0xFFFFFFFF)))
    snprintf(t.uid, sizeof(t.uid), "%08X%08X%08X", uid[2], uid[1], uid[0]);

  uint32_t obr = 0;
  if (swd::read32(FLASH_OBR, &obr) && (obr & OBR_RDPRT)) t.readProtected = true;
  return nullptr;
}

const char *massErase() {
  if (const char *e = unlockFlash()) return e;
  swd::write32(FLASH_CR, CR_MER);
  swd::write32(FLASH_CR, CR_MER | CR_STRT);
  const char *e = waitBusy(3000);
  swd::write32(FLASH_CR, 0);
  lockFlash();
  return e;
}

const char *pageErase(uint32_t addr, uint32_t len, uint16_t pageSize) {
  if (const char *e = unlockFlash()) return e;
  uint32_t first = addr & ~(uint32_t)(pageSize - 1);
  const char *e = nullptr;
  for (uint32_t p = first; p < addr + len && !e; p += pageSize) {
    swd::write32(FLASH_CR, CR_PER);
    swd::write32(FLASH_AR, p);
    swd::write32(FLASH_CR, CR_PER | CR_STRT);
    e = waitBusy(500);
  }
  swd::write32(FLASH_CR, 0);
  lockFlash();
  return e;
}

static const char *programHalf(uint32_t a, uint16_t hw) {
  if (!swd::write16(a, hw)) return fail("SWD write failed at 0x%08X", a);
  // Datasheets: 26us (F0010) .. 154us (F0020) per halfword, so poll BSY.
  uint32_t t0 = micros();
  uint32_t sr;
  for (;;) {
    if (!swd::read32(FLASH_SR, &sr)) return fail("SWD read failed at 0x%08X", a);
    if (!(sr & SR_BSY)) break;
    if (micros() - t0 > 20000) return fail("Program timeout at 0x%08X", a);
  }
  if (sr & SR_PGERR) return fail("Program error at 0x%08X (not erased?)", a);
  if (sr & SR_WRPRTERR) return fail("Write-protected at 0x%08X", a);
  return nullptr;
}

static const char *checkFirstHalf(uint32_t a, uint16_t hw) {
  uint32_t w;
  if (!swd::read32(a & ~3u, &w)) return fail("SWD read failed at 0x%08X", a);
  uint16_t got = (uint16_t)(w >> ((a & 2) * 8));
  if (got != hw)
    return fail("Write did not stick at 0x%08X: wrote 0x%04X, read 0x%04X (SR/CR ok)", a, hw, got);
  return nullptr;
}

const char *program(File &img, uint32_t base, uint32_t size, ProgressFn cb) {
  if (base & 1) return "Image base address must be even";
  if (!swd::supportsHalfword()) return "Target debug port does not support 16-bit writes";
  if (const char *e = unlockFlash()) return e;
  swd::write32(FLASH_CR, CR_PG);
  uint32_t cr = 0;
  if (!swd::read32(FLASH_CR, &cr) || !(cr & CR_PG)) {
    lockFlash();
    return fail("Flash did not enter program mode (FLASH_CR=0x%08X)", cr);
  }

  static uint8_t buf[1024];
  const char *err = nullptr;
  uint32_t checked = 0;  // bit per halfword lane already read back
  uint8_t lastPct = 255;
  img.seek(0);
  for (uint32_t off = 0; off < size && !err; off += sizeof(buf)) {
    uint32_t n = min<uint32_t>(sizeof(buf), size - off);
    if ((uint32_t)img.read(buf, n) != n) {
      err = "Image read error";
      break;
    }
    if (n & 1) buf[n++] = 0xFF;
    for (uint32_t i = 0; i < n && !err; i += 2) {
      uint16_t hw = buf[i] | (buf[i + 1] << 8);
      if (hw == 0xFFFF) continue;  // already erased
      err = programHalf(base + off + i, hw);
      // Read back the first halfword on each lane (addr[1] = 0 and 1) to catch
      // silent write failures right away instead of at verify.
      uint32_t lane = ((base + off + i) >> 1) & 1;
      if (!err && !(checked & (1u << lane))) {
        checked |= 1u << lane;
        err = checkFirstHalf(base + off + i, hw);
      }
    }
    uint8_t pct = (uint8_t)((uint64_t)(off + n) * 100 / size);
    if (cb && pct != lastPct) cb("program", lastPct = pct);
    yield();
  }
  swd::write32(FLASH_CR, 0);
  lockFlash();
  return err;
}

const char *verify(File &img, uint32_t base, uint32_t size, ProgressFn cb) {
  static uint32_t words[256];
  static uint8_t ref[1024];
  uint8_t lastPct = 255;
  // The prefetch buffer may still hold flash contents fetched before erase/program.
  uint32_t acr;
  if (swd::read32(FLASH_ACR, &acr) && (acr & ACR_PRFTBE)) {
    swd::write32(FLASH_ACR, acr & ~ACR_PRFTBE);
    swd::write32(FLASH_ACR, acr);
  }
  img.seek(0);
  for (uint32_t off = 0; off < size; off += sizeof(ref)) {
    uint32_t n = min<uint32_t>(sizeof(ref), size - off);
    if ((uint32_t)img.read(ref, n) != n) return "Image read error";
    uint32_t nw = (n + 3) / 4;
    for (uint32_t i = n; i < nw * 4; i++) ref[i] = 0xFF;
    if (!swd::readBlock32(base + off, words, nw)) return fail("SWD read failed at 0x%08X", base + off);
    if (memcmp(words, ref, nw * 4) != 0) {
      const uint8_t *got = (const uint8_t *)words;
      uint32_t i = 0;
      while (got[i] == ref[i]) i++;
      i &= ~3u;
      uint32_t exp, rd;
      memcpy(&exp, ref + i, 4);
      memcpy(&rd, got + i, 4);
      return fail("Verify mismatch at 0x%08X: read 0x%08X, expected 0x%08X", base + off + i, rd, exp);
    }
    uint8_t pct = (uint8_t)((uint64_t)(off + n) * 100 / size);
    if (cb && pct != lastPct) cb("verify", lastPct = pct);
    yield();
  }
  return nullptr;
}

static const char *writeRdp(uint16_t value, uint32_t timeoutMs) {
  if (const char *e = unlockFlash()) return e;
  swd::write32(FLASH_OPTKEYR, KEY1);
  swd::write32(FLASH_OPTKEYR, KEY2);
  uint32_t cr;
  if (!swd::read32(FLASH_CR, &cr) || !(cr & CR_OPTWRE)) {
    lockFlash();
    return "Option byte unlock failed";
  }
  swd::write32(FLASH_CR, CR_OPTER);
  swd::write32(FLASH_AR, OPT_RDP_ADDR);
  swd::write32(FLASH_CR, CR_OPTER | CR_STRT);
  const char *e = waitBusy(500);
  if (!e) {
    swd::write32(FLASH_CR, CR_OPTPG);
    swd::write16(OPT_RDP_ADDR, value);
    uint32_t sr = 0;
    e = waitBusy(timeoutMs, &sr);
    if (!e && (sr & SR_PGERR)) e = "Option byte program error";
  }
  swd::write32(FLASH_CR, 0);
  lockFlash();
  return e;
}

const char *setReadProtection() { return writeRdp(0x807F, 500); }

const char *removeReadProtection() { return writeRdp(0x5AA5, 5000); }

void resetAndRun() {
  swd::write32(DEMCR, 0);
  swd::write32(DHCSR, DBGKEY);  // clear C_DEBUGEN / C_HALT
  swd::write32(AIRCR, AIRCR_SYSRESET);
  swd::release();
}

}  // namespace mm32
