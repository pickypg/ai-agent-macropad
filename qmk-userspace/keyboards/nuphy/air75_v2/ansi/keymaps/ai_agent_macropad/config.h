// Default RGB matrix pattern for a freshly flashed board (applied at
// eeconfig init only — once VIA touches the mode it's persisted to
// EEPROM and this default no longer applies). Hue/sat/val defaults
// are deliberately NOT set here: NuPhy's own keyboard_post_init_kb()
// (londing_eeprom_data() in ansi.c, and device_reset_init() in
// side.c) unconditionally hardcodes hue=255/sat=255 on first boot,
// running after and overwriting whatever RGB_MATRIX_DEFAULT_HUE/SAT/
// VAL would have set — so those macros have no real effect on this
// board. Users pick their own color via VIA instead.
#pragma once

// ansi/config.h already sets RGB_MATRIX_DEFAULT_MODE (to CYCLE_LEFT_RIGHT) —
// override it here.
#undef RGB_MATRIX_DEFAULT_MODE
#define RGB_MATRIX_DEFAULT_MODE RGB_MATRIX_SOLID_REACTIVE_MULTIWIDE
