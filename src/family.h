#pragma once
// Programming back ends, one per chip family. main.cpp owns the protocol, the image upload,
// the button / auto-detect and the result reporting; it hands the target work to the family
// selected with "SET family <name>" (storage::cfg.family).
#include <Arduino.h>
#include <FS.h>

#include "storage.h"

namespace family {

typedef void (*ProgressFn)(const char *stage, uint8_t pct);

struct Result {
  const char *err = nullptr;  // nullptr = PASS
  bool counted = true;        // false: no target answered, so it is not counted as a failed device
  char chip[24] = "";         // chip name and ID for the RESULT / CHIP events
  char id[36] = "";           // up to the 128-bit Renesas unique ID as hex
};

struct Family {
  uint16_t probeMs;  // auto-detect polling interval
  // Became active, or a setting changed: apply the settings, put the target lines in their idle state.
  void (*select)();
  // BEGIN fields between <crc32> and <name>; fills the family part of m (size and crc are set).
  // Returns nullptr or an error; *name is left pointing at the rest of the line.
  const char *(*parseImage)(char *fields, storage::ImageMeta &m, char **name);
  String (*imageInfo)();  // family part of INFO for the stored image
  void (*flash)(File &img, ProgressFn cb, Result &r);  // one full programming cycle
  bool (*probe)();                                     // quick check for auto-detect: is a target there?
  // Target commands of this family (DETECT, ERASE, RESET, ...); false = not one of them.
  bool (*command)(const char *cmd, char *args);
};

extern const Family mm32Family;
extern const Family fortiorFamily;
extern const Family renesasFamily;

inline const Family &active() {
  static const Family *const all[storage::FAMILY_COUNT] = {&mm32Family, &fortiorFamily, &renesasFamily};
  return *all[storage::cfg.family];
}

// Provided by main.cpp.
void reply(const char *cmd, const String &s = String());
void replyErr(const char *cmd, const char *msg);
void evt(const char *fmt, ...);
String infoString();

}  // namespace family
