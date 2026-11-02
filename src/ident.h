#pragma once
// Board identification without writing anything: CRC32 checksums of the target flash, so the PC can
// match a board against the firmware catalog (IDENT command of the SWD families).
#include <Arduino.h>

namespace ident {

// args: byte lengths (multiples of 4) from base, e.g. "1024 16384". Reads the flash once up to the
// largest length and appends "crcs=<len>:<crc32>,..." (zlib CRC32 of base..base+len) to out.
// Lengths above limit (the chip's flash size) are skipped. Returns nullptr or an error.
const char *swdCrcs(uint32_t base, char *args, uint32_t limit, String &out);

// "uid=..." text for a hex ID; "-" when the chip has none or it could not be read.
String uidField(const char *uid);

}  // namespace ident
