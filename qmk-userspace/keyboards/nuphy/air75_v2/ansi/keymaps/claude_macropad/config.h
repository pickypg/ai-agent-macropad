// Default RGB matrix state for a freshly flashed board (applied at
// eeconfig init only — once VIA or a brightness/color keycode touches
// the config it's persisted to EEPROM and these defaults no longer
// apply). VAL is 255 despite this board's keyboard.json capping
// RGB_MATRIX_MAXIMUM_BRIGHTNESS at 128: unlike the VIA slider and the
// RGB_VAI/RGB_VAD keycodes (which clamp through rgb_matrix_sethsv()),
// this default is written straight into rgb_matrix_config.hsv at boot
// with no clamp, so it actually renders at 255 until first touched.
#pragma once

// ansi/config.h already sets RGB_MATRIX_DEFAULT_MODE (to CYCLE_LEFT_RIGHT) —
// override it here.
#undef RGB_MATRIX_DEFAULT_MODE
#define RGB_MATRIX_DEFAULT_MODE RGB_MATRIX_SOLID_REACTIVE_MULTIWIDE
#define RGB_MATRIX_DEFAULT_HUE  89   // #b1ffb8
#define RGB_MATRIX_DEFAULT_SAT  78   // #b1ffb8
#define RGB_MATRIX_DEFAULT_VAL  255  // #b1ffb8's V=255; exceeds this board's 128 cap until first VIA/keycode touch (see comment above)
