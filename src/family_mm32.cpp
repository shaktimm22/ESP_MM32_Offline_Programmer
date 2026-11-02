// MM32 family: SWD programming (mm32.cpp / swd.cpp).
#include <LittleFS.h>

#include "family.h"
#include "ident.h"
#include "mm32.h"
#include "swd.h"

using storage::cfg;
using storage::meta;

namespace family {

static void finishSession(bool run) {
  if (run) mm32::resetAndRun();
  swd::release();
  swd::setDelay(cfg.swdDelay);
}

static void select() {
  swd::begin();
  swd::setDelay(cfg.swdDelay);
}

// BEGIN fields: <base> <target>
static const char *parseImage(char *fields, storage::ImageMeta &m, char **name) {
  char *save;
  char *tBase = strtok_r(fields, " ", &save);
  char *tTarget = strtok_r(nullptr, " ", &save);
  if (!tBase || !tTarget) return "usage: BEGIN size crc base target name";
  m.base = strtoul(tBase, nullptr, 0);
  strlcpy(m.target, tTarget, sizeof(m.target));
  *name = strtok_r(nullptr, "", &save);
  return nullptr;
}

static String imageInfo() {
  char buf[64];
  snprintf(buf, sizeof(buf), "base=0x%08X target=%s", meta.base, meta.target);
  return buf;
}

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

static void flash(File &img, ProgressFn cb, Result &r) {
  static char msg[96];
  mm32::Target t;
  evt("STATE connect");
  r.err = mm32::attach(t);
  if (r.err) r.counted = false;
  if (!r.err) r.err = checkTarget(t, msg, sizeof(msg));
  if (!r.err) {
    strlcpy(r.chip, t.chip->name, sizeof(r.chip));
    // The unique ID tells boards apart in the PC log; DEV_ID only names the chip type.
    if (t.uid[0]) strlcpy(r.id, t.uid, sizeof(r.id));
    else snprintf(r.id, sizeof(r.id), "0x%08X", t.devId);
    evt("CHIP %s %s", r.chip, r.id);
    evt("STATE erase");
    r.err = cfg.erase == storage::ERASE_PAGES ? mm32::pageErase(meta.base, meta.size, t.chip->pageSize)
                                              : mm32::massErase();
  }
  if (!r.err) {
    evt("STATE program");
    r.err = mm32::program(img, meta.base, meta.size, cb);
  }
  if (!r.err && cfg.verify) {
    evt("STATE verify");
    r.err = mm32::verify(img, meta.base, meta.size, cb);
  }
  if (!r.err && cfg.protect) {
    evt("STATE protect");
    r.err = mm32::setReadProtection();
  }
  finishSession(!r.err && cfg.run);
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

// Attach, run op on success, reply, end the session. ok is the reply text on success.
template <typename Op>
static void maintenance(const char *c, const char *ok, bool run, Op op) {
  mm32::Target t;
  const char *err = mm32::attach(t);
  if (!err) err = op();
  if (err) replyErr(c, err);
  else reply(c, ok ? String(ok) : String());
  finishSession(run && !err);
}

static bool command(const char *c, char *args) {
  String cmd(c);
  if (cmd == "DETECT") {
    mm32::Target t;
    const char *err = mm32::attach(t);
    if (!err) {
      char buf[160];
      snprintf(buf, sizeof(buf), "dpidr=0x%08X devid=0x%08X chip=%s flash=%u protected=%d swdelay=%u ", t.dpidr,
               t.devId, t.chip ? t.chip->name : "UNKNOWN", t.chip ? t.chip->flashKB : 0, t.readProtected,
               t.swdDelay);
      reply(c, buf + ident::uidField(t.uid));
    } else {
      replyErr(c, err);
    }
    finishSession(!err);
  } else if (cmd == "IDENT") {
    // IDENT <base> <len>...: unique ID and flash CRC32s for identifying the firmware on a board.
    // Nothing is written; the target runs again afterwards.
    char *rest;
    uint32_t base = strtoul(args, &rest, 0);
    mm32::Target t;
    const char *err = mm32::attach(t);
    if (!err) {
      char buf[96];
      snprintf(buf, sizeof(buf), "chip=%s devid=0x%08X protected=%d ", t.chip ? t.chip->name : "UNKNOWN", t.devId,
               t.readProtected);
      String s = String(buf) + ident::uidField(t.uid) + " ";
      // Read protection blocks flash reads from the debugger: only the ID is available then.
      if (!t.readProtected && base >= mm32::FLASH_BASE) {
        uint32_t size = t.chip ? (uint32_t)t.chip->flashKB * 1024 : 16 * 1024;
        err = ident::swdCrcs(base, rest, mm32::FLASH_BASE + size - base, s);
      }
      if (!err) reply(c, s);
    }
    if (err) replyErr(c, err);
    finishSession(!err);
  } else if (cmd == "ERASE") {
    maintenance(c, nullptr, false, [] { return mm32::massErase(); });
  } else if (cmd == "UNLOCK") {
    maintenance(c, "flash erased, power-cycle the target", false, [] { return mm32::removeReadProtection(); });
  } else if (cmd == "PROTECT") {
    maintenance(c, "power-cycle the target to activate", false, [] { return mm32::setReadProtection(); });
  } else if (cmd == "RESET") {
    maintenance(c, nullptr, true, [] { return (const char *)nullptr; });
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
    finishSession(false);
  } else if (cmd == "CHIPS") {
    reply(c, chipsString());
  } else if (cmd == "CHIPADD") {
    // CHIPADD <devid> <name> <flashKB> [pageSize]
    char *save;
    char *tId = strtok_r(args, " ", &save);
    char *tName = strtok_r(nullptr, " ", &save);
    char *tKb = strtok_r(nullptr, " ", &save);
    char *tPage = strtok_r(nullptr, " ", &save);
    if (!tId || !tName || !tKb) {
      replyErr(c, "usage: CHIPADD devid name flashKB [page]");
      return true;
    }
    mm32::Chip ch{};
    ch.devId = strtoul(tId, nullptr, 0);
    strlcpy(ch.name, tName, sizeof(ch.name));
    ch.flashKB = strtoul(tKb, nullptr, 0);
    ch.pageSize = tPage ? strtoul(tPage, nullptr, 0) : 1024;
    if (!ch.devId || !ch.flashKB || !ch.pageSize) {
      replyErr(c, "invalid values");
      return true;
    }
    mm32::addChip(ch);
    storage::saveChips();
    reply(c, chipsString());
  } else if (cmd == "CHIPDEL") {
    if (!mm32::removeChip(strtoul(args, nullptr, 0))) {
      replyErr(c, "not a custom chip");
      return true;
    }
    storage::saveChips();
    reply(c, chipsString());
  } else {
    return false;
  }
  return true;
}

const Family mm32Family = {200, select, parseImage, imageInfo, flash, mm32::probe, command};

}  // namespace family
