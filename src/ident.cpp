#include "ident.h"

#include "storage.h"
#include "swd.h"

namespace ident {

const char *swdCrcs(uint32_t base, char *args, uint32_t limit, String &out) {
  static uint32_t lens[96];
  static uint32_t words[256];
  size_t n = 0;
  char *save;
  for (char *t = strtok_r(args, " ", &save); t && n < 96; t = strtok_r(nullptr, " ", &save)) {
    uint32_t v = strtoul(t, nullptr, 0);
    if (v && v % 4 == 0 && v <= limit) lens[n++] = v;
  }
  // Ascending, without duplicates: one pass over the flash serves every length.
  for (size_t i = 1; i < n; i++)
    for (size_t j = i; j > 0 && lens[j - 1] > lens[j]; j--) std::swap(lens[j - 1], lens[j]);
  out += "crcs=";
  uint32_t crc = 0, off = 0;
  for (size_t i = 0; i < n; i++) {
    if (i && lens[i] == lens[i - 1]) continue;
    while (off < lens[i]) {
      uint32_t chunk = min<uint32_t>(sizeof(words), lens[i] - off);
      if (!swd::readBlock32(base + off, words, chunk / 4)) return "SWD read failed";
      crc = storage::crc32Update(crc, (const uint8_t *)words, chunk);
      off += chunk;
      yield();
    }
    char buf[24];
    snprintf(buf, sizeof(buf), "%s%u:%08X", out.endsWith("=") ? "" : ",", lens[i], crc);
    out += buf;
  }
  return nullptr;
}

String uidField(const char *uid) { return String("uid=") + (uid && uid[0] ? uid : "-"); }

}  // namespace ident
