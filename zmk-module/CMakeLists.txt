if (CONFIG_AI_AGENT_MACROPAD_HID)
  zephyr_library()
  zephyr_library_sources(src/ai_agent_macropad_hid.c)

  # See src/raw_hid_ble_compat.c: zzeneg/zmk-raw-hid's BLE path needs a
  # function this fork's ZMK core doesn't provide.
  if (CONFIG_ZMK_BLE)
    zephyr_library_sources(src/raw_hid_ble_compat.c)
  endif()
endif()
