#pragma once
// Bit-banged Fortior FICE two-wire debug link (FICEK = clock / RSTN, FICED = data).
// Reverse-engineered from captures of the Fortior emulator + Keil; see re/FINDINGS.md.
//
// Frames (bits LSB first, the target samples FICED on the FICEK rising edge):
//   SEL reg : SYNC, 1, 1, reg[9]
//   WR  v   : SYNC, 1, 0,0,0, v[8], then clock until the target acknowledges
//   RD      : SYNC, 0, then the target sends 0s, a 1 (start bit) and v[8]
#include <stdint.h>

namespace fice {

void begin();
void setSpeed(uint16_t pct);  // timing scale: 100 = same timing as the Fortior emulator, 200 = half speed
uint16_t speed();

void attachLines();  // drive FICEK high, FICED low
void release();      // float both lines (target runs, its RSTN pull-up keeps it out of reset)
void resetPulse();   // 42 us low pulse on FICEK (RSTN), as the emulator does before every session

void sel(uint16_t reg);
// Writes one byte to the selected register. maxCells bounds the acknowledge wait
// (each poll clock is about 1.3 us; flash program ~30, erase up to ~300000).
bool wr(uint8_t v, uint32_t maxCells = 4000);
int rd();  // byte, or -1 if the target did not answer

}  // namespace fice
