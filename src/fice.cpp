#include "fice.h"

#include <Arduino.h>

#include "pins.h"

namespace fice {

static constexpr uint32_t CK = 1UL << PIN_FICEK;
static constexpr uint32_t DT = 1UL << PIN_FICED;

// Cell timing in ns, measured on the Fortior emulator (captures at 24 MS/s).
static constexpr uint32_t NS_GAP = 5000;      // idle (FICEK high) before every frame
static constexpr uint32_t NS_SYNC_LO = 3250;  // frame start: long FICEK low
static constexpr uint32_t NS_SYNC_DSET = 800; // first bit put on FICED this long after FICEK rises
static constexpr uint32_t NS_SYNC_HI = 2170;
static constexpr uint32_t NS_W_DSET = 670;    // write cell: FICED changes this long after FICEK falls
static constexpr uint32_t NS_W_LO = 1830;
static constexpr uint32_t NS_W_HI = 790;
static constexpr uint32_t NS_F_LO = 520;      // fast cell (reads, acknowledge polling)
static constexpr uint32_t NS_F_HI = 800;
static constexpr uint32_t US_RESET = 42;

static constexpr uint8_t MIN_LEAD_ZEROS = 2;  // a read starts with >= 4 zeros; a floating line reads 1
static constexpr uint8_t MAX_LEAD_ZEROS = 16;

static struct {
  uint32_t gap, syncLo, syncDset, syncHi, wDset, wLo, wHi, fLo, fHi;
} c;
static uint16_t s_speed = 100;
static uint16_t s_reg = 0;

static inline uint32_t IRAM_ATTR now() {
  uint32_t v;
  __asm__ __volatile__("rsr %0, ccount" : "=a"(v));
  return v;
}
static inline void IRAM_ATTR until(uint32_t t) {
  while ((int32_t)(now() - t) < 0) {
  }
}
static inline void IRAM_ATTR ckLo() { GPOC = CK; }
static inline void IRAM_ATTR ckHi() { GPOS = CK; }
static inline void IRAM_ATTR dSet(uint32_t b) {
  if (b & 1) GPOS = DT;
  else GPOC = DT;
}
static inline void IRAM_ATTR dDrive() { GPES = DT; }
static inline void IRAM_ATTR dRelease() { GPEC = DT; }
static inline uint32_t IRAM_ATTR dGet() { return (GPI & DT) ? 1 : 0; }

// Interrupts off for the duration of a frame so the cell timing stays exact.
struct IrqLock {
  uint32_t ps;
  IrqLock() { ps = xt_rsil(15); }
  ~IrqLock() { xt_wsr_ps(ps); }
  void breathe() {  // let pending interrupts run during long acknowledge waits
    xt_wsr_ps(ps);
    ps = xt_rsil(15);
  }
};

static uint32_t cyc(uint32_t ns) { return (uint64_t)ns * (F_CPU / 1000000) * s_speed / 100 / 1000; }

void setSpeed(uint16_t pct) {
  s_speed = constrain(pct, 50, 1000);
  c.gap = cyc(NS_GAP);
  c.syncLo = cyc(NS_SYNC_LO);
  c.syncDset = cyc(NS_SYNC_DSET);
  c.syncHi = cyc(NS_SYNC_HI);
  c.wDset = cyc(NS_W_DSET);
  c.wLo = cyc(NS_W_LO);
  c.wHi = cyc(NS_W_HI);
  c.fLo = cyc(NS_F_LO);
  c.fHi = cyc(NS_F_HI);
}

uint16_t speed() { return s_speed; }

// ---------------------------------------------------------------- cells

// Frame start: long FICEK low; the first bit goes on FICED while FICEK is high.
static void IRAM_ATTR sync(uint32_t first) {
  uint32_t t = now() + c.gap;
  until(t);
  ckLo();
  until(t += c.syncLo);
  ckHi();
  until(t += c.syncDset);
  dSet(first);
  dDrive();
  until(t + (c.syncHi - c.syncDset));
}

// Host bit with the long low phase; the target samples it on the rising edge.
static void IRAM_ATTR wcell(uint32_t b) {
  uint32_t t = now();
  ckLo();
  until(t + c.wDset);
  dSet(b);
  until(t += c.wLo);
  ckHi();
  until(t + c.wHi);
}

// Short cell. Returns FICED just before the rising edge: the target changes FICED right
// after a rising edge, so this is the bit it put out after the previous one.
static uint32_t IRAM_ATTR fcell() {
  uint32_t t = now();
  ckLo();
  until(t += c.fLo);
  uint32_t b = dGet();
  ckHi();
  until(t + c.fHi);
  return b;
}

// Registers 0, 1, 3, 4 answer with a fixed 2-clock acknowledge; 2, 5 and 436 with busy/ready.
static bool shortReg(uint16_t reg) { return reg <= 4 && reg != 2; }

// ---------------------------------------------------------------- API

void begin() {
  setSpeed(s_speed);
  release();
}

void attachLines() {
  digitalWrite(PIN_FICEK, HIGH);  // set the level first so FICEK (RSTN) never glitches low
  digitalWrite(PIN_FICED, LOW);
  pinMode(PIN_FICEK, OUTPUT);
  pinMode(PIN_FICED, OUTPUT);
}

void release() {
  pinMode(PIN_FICEK, INPUT);
  pinMode(PIN_FICED, INPUT);
}

void resetPulse() {
  ckLo();
  delayMicroseconds(US_RESET);
  ckHi();
}

void sel(uint16_t reg) {
  IrqLock lock;
  s_reg = reg;
  sync(1);
  fcell();  // target samples the first 1
  fcell();  // second 1: SELECT
  for (int i = 0; i < 9; i++) wcell(reg >> i);
}

bool wr(uint8_t v, uint32_t maxCells) {
  IrqLock lock;
  sync(1);
  fcell();
  wcell(0);
  wcell(0);
  wcell(0);
  for (int i = 0; i < 8; i++) wcell(v >> i);
  dRelease();
  fcell();  // still our last bit
  if (shortReg(s_reg)) return fcell() == 1;
  bool busy = false;  // the target pulls FICED low while busy, then high when done
  for (uint32_t n = 0; n < maxCells; n++) {
    if (fcell()) {
      if (busy) return true;
    } else {
      busy = true;
    }
    if ((n & 255) == 255) {
      lock.breathe();
      if ((n & 0xFFFF) == 0xFFFF) ESP.wdtFeed();
    }
  }
  return false;
}

int rd() {
  IrqLock lock;
  sync(0);
  fcell();
  dRelease();
  uint8_t zeros = 0;
  while (!fcell()) {
    if (++zeros > MAX_LEAD_ZEROS) return -1;
  }
  if (zeros < MIN_LEAD_ZEROS) return -1;  // line is just pulled up: nobody there
  uint8_t v = 0;
  for (int i = 0; i < 8; i++) v |= fcell() << i;
  return v;
}

}  // namespace fice
