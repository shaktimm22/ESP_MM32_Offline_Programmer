// Fortior FU68xx family: FICE programming (fu68.cpp / fice.cpp).
#include <LittleFS.h>

#include "family.h"
#include "fice.h"
#include "fu68.h"

using storage::cfg;
using storage::meta;

namespace family {

static void select() {
  fice::begin();  // floats both lines: the target runs, its RSTN pull-up keeps it out of reset
  fice::setSpeed(cfg.speed);
}

// BEGIN fields: <option bytes, 8 hex>
static const char *parseImage(char *fields, storage::ImageMeta &m, char **name) {
  char *save;
  char *tOpt = strtok_r(fields, " ", &save);
  if (!tOpt) return "usage: BEGIN size crc opt name";
  if (!storage::parseOpt(tOpt, m.opt)) return "opt must be 8 hex digits";
  if (m.size > fu68::OPT_ADDR) return "image must end before 0x3FFC (option bytes)";
  *name = strtok_r(nullptr, "", &save);
  return nullptr;
}

// CRC the chip must report after programming the stored image and option bytes.
static uint16_t expectedCrc() {
  File img = LittleFS.open(storage::imgPath(), "r");
  return img ? fu68::imageCrc(img, meta.opt) : 0;
}

static String imageInfo() {
  char buf[48];
  snprintf(buf, sizeof(buf), "opt=%s chipcrc=0x%04X", storage::optString(meta.opt).c_str(), expectedCrc());
  return buf;
}

static void flash(File &img, ProgressFn cb, Result &r) {
  static char msg[96];
  fu68::Target t;
  evt("STATE connect");
  r.err = fu68::attach(t);
  if (r.err && t.id0 < 0) r.counted = false;
  if (!r.err) {
    strlcpy(r.chip, "FU68xx", sizeof(r.chip));
    snprintf(r.id, sizeof(r.id), "%02X%02X%02X", t.id0, t.id1, t.id2);
    evt("CHIP %s %s", r.chip, r.id);
    evt("STATE erase");
    r.err = fu68::erase();
  }
  if (!r.err) r.err = fu68::writeConfig(meta.opt);
  if (!r.err) {
    evt("STATE program");
    r.err = fu68::program(img, meta.opt, cb);
  }
  if (!r.err && cfg.verify) {
    evt("STATE verify");
    uint16_t want = fu68::imageCrc(img, meta.opt), crc = 0;
    r.err = fu68::readCrc(&crc);
    if (!r.err && crc != want) {
      snprintf(msg, sizeof(msg), "Verify failed: chip CRC 0x%04X, expected 0x%04X", crc, want);
      r.err = msg;
    }
  }
  fu68::finish(cfg.run);
}

static bool command(const char *c, char *args) {
  String cmd(c);
  if (cmd == "DETECT") {
    fu68::Target t;
    const char *err = fu68::attach(t);
    if (!err) {
      char buf[80];
      snprintf(buf, sizeof(buf), "chip=FU68xx id=%02X%02X%02X speed=%u", t.id0, t.id1, t.id2, fice::speed());
      reply(c, buf);
    } else {
      replyErr(c, err);
    }
    fu68::finish(true);
  } else if (cmd == "ERASE") {
    fu68::Target t;
    const char *err = fu68::attach(t);
    if (!err) err = fu68::erase();
    if (err) replyErr(c, err);
    else reply(c);
    fu68::finish(true);
  } else if (cmd == "RESET") {
    fice::attachLines();
    fice::resetPulse();
    delay(1);
    fice::release();
    reply(c);
  } else if (cmd == "OPT") {
    // OPT <8 hex>: change the option bytes of the stored image
    if (!storage::parseOpt(args, meta.opt)) {
      replyErr(c, "usage: OPT 00F000FF");
      return true;
    }
    storage::saveMeta();
    reply(c, infoString());
  } else if (cmd == "FICE") {
    // FICE <ops>: raw protocol access, e.g. "FICE A P D15 S3 R W10 S4 WF9 D10 S0 R X"
    reply(c, fu68::script(args));
  } else {
    return false;
  }
  return true;
}

const Family fortiorFamily = {300, select, parseImage, imageInfo, flash, fu68::probe, command};

}  // namespace family
