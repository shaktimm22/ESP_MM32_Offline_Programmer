#include "fu68.h"

#include "fice.h"
#include "pins.h"

namespace fu68 {

// FICE registers.
static constexpr uint16_t REG_ID0 = 0;
static constexpr uint16_t REG_ID1 = 1;
static constexpr uint16_t REG_CMD = 2;   // command / status
static constexpr uint16_t REG_CTRL = 3;
static constexpr uint16_t REG_KEY = 4;
static constexpr uint16_t REG_MODE = 5;
static constexpr uint16_t REG_DATA = 436;

// Status values seen in every capture.
static constexpr int ST_READY = 0x83;
static constexpr int ST_XDATA = 0x9F;
static constexpr int ST_SFR = 0x98;
static constexpr int ST_FLASH = 0x0D;

// Acknowledge waits, in poll clocks (~1.3 us each).
static constexpr uint32_t ACK_BYTE = 2000;      // programming one byte takes ~30
static constexpr uint32_t ACK_OPTION = 20000;   // option write took ~380
static constexpr uint32_t ACK_ERASE = 1500000;  // the long erase took ~207000 (0.38 s)

static constexpr uint16_t BURST = 128;  // bytes per program command (Keil uses <= 0x80)

static uint16_t s_reg = 0;
static char s_msg[96];

static const char *fail(const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(s_msg, sizeof(s_msg), fmt, ap);
  va_end(ap);
  return s_msg;
}

#define TRY(x)                        \
  do {                                \
    if (const char *e_ = (x)) return e_; \
  } while (0)

// ---------------------------------------------------------------- primitives

static void S(uint16_t reg) {
  s_reg = reg;
  fice::sel(reg);
}

static const char *W(uint8_t v, uint32_t maxCells = 4000) {
  if (!fice::wr(v, maxCells)) return fail("No acknowledge writing 0x%02X to FICE reg %u", v, s_reg);
  return nullptr;
}

static const char *R(int *out = nullptr, int expect = -1) {
  int v = fice::rd();
  if (v < 0) return fail("No answer reading FICE reg %u", s_reg);
  if (expect >= 0 && v != expect) return fail("FICE reg %u read 0x%02X, expected 0x%02X", s_reg, v, expect);
  if (out) *out = v;
  return nullptr;
}

// Sent before every debug command.
static const char *prefix() {
  S(REG_CTRL);
  TRY(R());
  TRY(W(0x10));
  S(REG_MODE);
  TRY(W(0x01));
  S(REG_CMD);
  TRY(W(0x02));
  TRY(W(0x01));
  return R(nullptr, ST_READY);
}

static const char *sfrWrite(uint8_t sfr, uint8_t v) {
  TRY(prefix());
  S(REG_CMD);
  TRY(W(0x22));
  S(REG_DATA);
  TRY(W(sfr));
  TRY(W(0x01));
  S(REG_CMD);
  TRY(R(nullptr, ST_SFR));
  S(REG_DATA);
  TRY(W(v));
  S(REG_CMD);
  return R(nullptr, ST_READY);
}

static const char *xdataSetup(uint16_t addr) {
  TRY(prefix());
  S(REG_CMD);
  TRY(W(0x11));
  S(REG_DATA);
  TRY(W(addr >> 8));
  TRY(W(addr & 0xFF));
  TRY(W(0x01));
  S(REG_CMD);
  TRY(R(nullptr, ST_XDATA));
  S(REG_DATA);
  return nullptr;
}

static const char *xdataWrite(uint16_t addr, uint8_t v) {
  TRY(xdataSetup(addr));
  TRY(W(v));
  S(REG_CMD);
  return R(nullptr, ST_READY);
}

static const char *xdataRead(uint16_t addr, int *v) {
  TRY(xdataSetup(addr));
  TRY(R(v));
  S(REG_CMD);
  return R(nullptr, ST_READY);
}

// Keil wraps XDATA accesses outside of programming with SFR 0x86 writes.
// refresh=false right after erase(), which already ended with the same CTRL/MODE accesses.
static const char *xdataBegin(bool refresh = true) {
  if (refresh) {
    S(REG_CTRL);
    TRY(R());
    TRY(W(0x10));
    S(REG_MODE);
    TRY(R());
  }
  TRY(sfrWrite(0x86, 0x90));
  return sfrWrite(0x86, 0x62);
}

static const char *xdataEnd() {
  S(REG_CTRL);
  TRY(R());
  TRY(W(0x10));
  TRY(sfrWrite(0x86, 0x21));
  delay(3);
  return nullptr;
}

static const char *ctrlKey(uint8_t ctrl, bool readFirst) {
  S(REG_CTRL);
  if (readFirst) TRY(R());
  TRY(W(ctrl));
  S(REG_KEY);
  return W(0xF9);
}

static const char *eraseCmd(uint8_t cmd, uint8_t k1, uint8_t k2, uint8_t k3) {
  S(REG_DATA);
  TRY(W(cmd));
  TRY(R(nullptr, ST_FLASH));
  TRY(W(k1));
  TRY(W(k2));
  TRY(W(k3));
  TRY(W(0xFF, ACK_ERASE));
  S(REG_CMD);
  return R(nullptr, ST_READY);
}

// Reset + entry used before every session (emulator waits 12.6..14.8 ms after the pulse).
static void enter() {
  fice::attachLines();
  fice::resetPulse();
  delay(15);
}

// Sequence the emulator sends after the erase; exact purpose unknown (option area handling).
static const char *postEraseBlock(bool eraseAgain) {
  enter();
  TRY(prefix());
  S(REG_KEY);
  TRY(W(0xF1));
  TRY(W(0xF5));
  TRY(W(0xE5));
  TRY(R());
  S(REG_CMD);
  TRY(W(0x02));
  TRY(W(0x01));
  TRY(W(0x44));
  TRY(R(nullptr, ST_READY));
  S(REG_DATA);
  for (uint8_t b : {0xA0, 0xAA, 0xA2, 0x50, 0x00}) TRY(W(b));
  S(REG_CMD);
  TRY(R());
  S(REG_DATA);
  TRY(W(0xFF, ACK_OPTION));
  S(REG_CMD);
  TRY(R(nullptr, ST_READY));
  S(REG_CTRL);
  TRY(R());
  S(REG_KEY);
  TRY(W(0xFF));
  TRY(R());
  if (eraseAgain) {
    delay(2);
    TRY(eraseCmd(0x04, 0xAE, 0xB1, 0xC1));
    S(REG_ID1);
    TRY(R());
  }
  return nullptr;
}

static const char *burst(uint16_t addr, const uint8_t *buf, uint16_t n) {
  TRY(prefix());
  S(REG_DATA);
  TRY(W(0x02));
  TRY(R(nullptr, ST_FLASH));
  TRY(W(addr >> 8));
  TRY(W(addr & 0xFF));
  TRY(W(n & 0xFF));
  for (uint16_t i = 0; i < n; i++) {
    if (!fice::wr(buf[i], ACK_BYTE)) return fail("Program error at 0x%04X", addr + i);
  }
  S(REG_CMD);
  return R(nullptr, ST_READY);
}

// ---------------------------------------------------------------- operations

const char *attach(Target &t) {
  t = Target();
  enter();
  S(REG_CTRL);
  if (fice::rd() < 0) return fail("No target: no answer on FICE (check wiring, GND and power)");
  TRY(W(0x10));
  S(REG_KEY);
  TRY(W(0xF9));
  delay(10);
  S(REG_ID0);
  TRY(R(&t.id0));
  delay(3);
  S(REG_ID1);
  TRY(R(&t.id1));
  delay(3);
  TRY(xdataBegin());
  TRY(xdataRead(0x400D, &t.id2));
  TRY(xdataEnd());
  if (t.id0 != CHIP_ID) return fail("Unexpected chip ID 0x%02X (expected 0x%02X)", t.id0, CHIP_ID);
  return nullptr;
}

const char *erase() {
  S(REG_CTRL);
  TRY(W(0x98));
  delay(17);
  TRY(ctrlKey(0x98, true));
  delay(3);
  TRY(ctrlKey(0x98, true));
  S(REG_CTRL);
  TRY(R());
  TRY(W(0x98));
  S(REG_MODE);
  TRY(W(0x01));
  S(REG_CMD);
  TRY(W(0x02));
  TRY(W(0x01));
  TRY(R(nullptr, ST_READY));
  TRY(eraseCmd(0x07, 0xCB, 0xB3, 0xC3));
  S(REG_CTRL);
  TRY(R());
  TRY(eraseCmd(0x04, 0xAE, 0xB1, 0xC1));
  S(REG_ID1);
  TRY(R());
  delay(2);
  TRY(postEraseBlock(true));
  delay(2);
  TRY(postEraseBlock(false));
  delay(6);
  enter();
  S(REG_CTRL);
  TRY(R());
  TRY(W(0x10));
  S(REG_MODE);
  TRY(R());
  S(REG_KEY);
  return W(0xF9);
}

const char *writeConfig(const uint8_t opt[4]) {
  for (int i = 0; i < 3; i++) {  // 0x3FFC..0x3FFE are mirrored into CCFG 0x401C..0x401E
    TRY(xdataBegin(i > 0));
    TRY(xdataWrite(0x401C + i, opt[i]));
    TRY(xdataEnd());
  }
  TRY(ctrlKey(0x10, true));
  delay(7);
  return nullptr;
}

const char *program(File &img, const uint8_t opt[4], ProgressFn cb) {
  static uint8_t buf[BURST];
  for (uint32_t addr = 0; addr < OPT_ADDR; addr += BURST) {
    uint16_t n = min<uint32_t>(BURST, OPT_ADDR - addr);
    img.seek(addr);
    size_t got = addr < img.size() ? img.read(buf, n) : 0;
    memset(buf + got, 0, n - got);  // past the end of the image = erased (0x00)
    bool blank = true;  // erased flash reads 0x00; skip blocks with nothing to write
    for (uint16_t i = 0; i < n && blank; i++) blank = buf[i] == 0;
    if (!blank) TRY(burst(addr, buf, n));
    if (cb) cb("program", addr * 100 / OPT_ADDR);
  }
  TRY(burst(OPT_ADDR, opt, 4));
  if (cb) cb("program", 100);
  return nullptr;
}

const char *readCrc(uint16_t *crc) {
  delay(5);
  TRY(xdataWrite(0x4022, 0x0C));
  TRY(xdataWrite(0x4024, 0x00));                    // first sector
  TRY(xdataWrite(0x4025, FLASH_SIZE / 128 - 1));    // sector count - 1
  TRY(xdataWrite(0x4022, 0x02));                    // start
  int st = 0;
  for (int i = 0; i < 20; i++) {
    delay(2);
    TRY(xdataRead(0x4022, &st));
    if (st & 0x10) break;  // CRCDONE
  }
  if (!(st & 0x10)) return fail("CRC unit did not finish (CRC_CR=0x%02X)", st);
  int lo, hi;
  TRY(xdataWrite(0x4022, 0x00));
  TRY(xdataRead(0x4023, &lo));
  TRY(xdataWrite(0x4022, 0x01));
  TRY(xdataRead(0x4023, &hi));
  *crc = (hi << 8) | lo;
  return nullptr;
}

void finish(bool run) {
  delay(2);
  S(REG_CTRL);
  fice::wr(0x10);
  delay(2);
  S(REG_MODE);
  fice::wr(0x10);
  if (run) {
    fice::resetPulse();
    fice::release();  // RSTN pull-up takes the chip out of reset; the firmware starts
  } else {
    digitalWrite(PIN_FICEK, LOW);  // hold the chip in reset, like the emulator does
  }
}

bool probe() {
  enter();
  S(REG_CTRL);
  if (fice::rd() < 0) {
    fice::release();
    return false;
  }
  bool ok = fice::wr(0x10);
  S(REG_KEY);
  ok = ok && fice::wr(0xF9);
  delay(10);
  S(REG_ID0);
  ok = ok && fice::rd() == CHIP_ID;
  fice::release();
  return ok;
}

uint16_t crc16(const uint8_t *data, size_t len, uint16_t crc) {
  while (len--) {
    crc ^= (uint16_t)*data++ << 8;
    for (int k = 0; k < 8; k++) crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}

uint16_t imageCrc(File &img, const uint8_t opt[4]) {
  static uint8_t buf[256];
  uint16_t crc = 0xFFFF;
  img.seek(0);
  for (uint32_t addr = 0; addr < OPT_ADDR; addr += sizeof(buf)) {
    size_t n = min<uint32_t>(sizeof(buf), OPT_ADDR - addr);
    size_t got = img.read(buf, n);
    memset(buf + got, 0, n - got);
    crc = crc16(buf, n, crc);
  }
  return crc16(opt, 4, crc);
}

String script(const char *ops) {
  String out;
  char buf[160];
  strlcpy(buf, ops, sizeof(buf));
  char *save;
  for (char *tok = strtok_r(buf, " ", &save); tok; tok = strtok_r(nullptr, " ", &save)) {
    char op = toupper(tok[0]);
    const char *arg = tok + 1;
    String r;
    switch (op) {
      case 'A': fice::attachLines(); break;
      case 'X': fice::release(); break;
      case 'P': fice::resetPulse(); break;
      case 'D': delay(strtoul(arg, nullptr, 0)); break;
      case 'S': S(strtoul(arg, nullptr, 0)); break;
      case 'W': {
        char *end;
        uint8_t v = strtoul(arg, &end, 16);
        uint32_t cells = *end == ',' ? strtoul(end + 1, nullptr, 0) : 4000;
        r = fice::wr(v, cells) ? "ok" : "noack";
        break;
      }
      case 'R': {
        int v = fice::rd();
        char h[8];
        snprintf(h, sizeof(h), "%02X", v);
        r = v < 0 ? String("--") : String(h);
        break;
      }
      default: r = "?";
    }
    if (r.length()) {
      if (out.length()) out += ' ';
      out += tok;
      out += '=';
      out += r;
    }
  }
  return out;
}

}  // namespace fu68
