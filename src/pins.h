#pragma once
// Hardware pin map (NodeMCU / Wemos D1 mini style ESP8266 boards).
// All families use the same two target lines, so one fixture connector serves them all:
//   MM32 (SWD):     VCC, GND, SWCLK, SWDIO (no NRST)
//   Fortior (FICE): VDD5, GND, FICEK (RSTN), FICED (P1.2). The FU68xx I/O runs at VDD5 (5 V while
//                   programming), so put a level shifter (e.g. TXS0102) between the ESP8266 and the target.
//   Renesas (SWD):  VCC, GND, SWCLK (P300), SWDIO (P108), plus RES (optional, recommended).
// Only GPIO0..15 can be used for the target lines (GPIO16 is not on the fast GPIO registers).

#ifndef PIN_SWCLK
#define PIN_SWCLK 14   // D5 -> target SWCLK / FICEK
#endif
#ifndef PIN_SWDIO
#define PIN_SWDIO 12   // D6 <-> target SWDIO / FICED (internal pull-up enabled for SWD)
#endif
#ifndef PIN_FICEK
#define PIN_FICEK PIN_SWCLK
#endif
#ifndef PIN_FICED
#define PIN_FICED PIN_SWDIO
#endif
#ifndef PIN_NRST
#define PIN_NRST 13    // D7 -> Renesas RES (open drain: driven low or released). -1 if not wired.
#endif                 //       Not used by MM32 or Fortior.
#ifndef PIN_BUTTON
#define PIN_BUTTON 0   // D3 = onboard "FLASH" button on NodeMCU, active low
#endif
#ifndef PIN_LED_PASS
#define PIN_LED_PASS 5 // D1 -> green LED (active high)
#endif
#ifndef PIN_LED_FAIL
#define PIN_LED_FAIL 4 // D2 -> red LED (active high)
#endif
#ifndef PIN_LED_BUSY
#define PIN_LED_BUSY 2 // D4 = onboard blue LED (active low)
#endif
#ifndef PIN_BUZZER
#define PIN_BUZZER 15  // D8 -> buzzer via transistor (active high). -1 to disable
#endif

#define FW_VERSION "2.1.0"
