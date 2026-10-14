#pragma once
// Bit-banged ARM Serial Wire Debug (ADIv5 SW-DP + AHB MEM-AP) for ESP8266.
#include <stdint.h>
#include <stddef.h>

namespace swd {

enum : uint8_t { ACK_OK = 1, ACK_WAIT = 2, ACK_FAULT = 4, ACK_PARITY = 8 };

void begin();
void setDelay(uint32_t loops);     // half-period delay; 0 = fastest
uint32_t getDelay();
bool sampleSwdio();                // SWDIO level with the ESP8266 driver off (pull-up on)
void release();                    // float SWDIO, idle clock

// Line reset + JTAG-to-SWD + read DPIDR. With powerUp, also clears errors
// and powers up the debug domain so memory access works.
bool connect(uint32_t *dpidr, bool powerUp = true);
void clearErrors();
uint8_t lastAck();

bool readDP(uint8_t addr, uint32_t *v);
bool writeDP(uint8_t addr, uint32_t v);
bool readAP(uint8_t addr, uint32_t *v);
bool writeAP(uint8_t addr, uint32_t v);

bool read32(uint32_t addr, uint32_t *v);
bool write32(uint32_t addr, uint32_t v);
bool write16(uint32_t addr, uint16_t v);
bool supportsHalfword();  // does the MEM-AP accept CSW.Size = 16 bit?
// Word reads with address auto-increment; handles the 1 KB TAR wrap.
bool readBlock32(uint32_t addr, uint32_t *buf, size_t words);

}  // namespace swd
