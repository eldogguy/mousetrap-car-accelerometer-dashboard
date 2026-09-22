// Native SSD1306 128x64 OLED driver -- see oled_ssd1306.c for why this
// moved to C alongside the IMU sampler (shares one I2C bus handle with it;
// see sampler_task.h's imu_sampler_get_bus()).
#pragma once

#include <stdbool.h>
#include <stdint.h>

// Adds the OLED as a device on the sampler's already-created bus (call
// imu_sampler_start() first) and runs the SSD1306 init sequence. Returns
// false if the bus doesn't exist yet, the device can't be added, or the
// init command sequence fails -- same fail-soft contract as the old
// ssd1306.py: a missing/failed OLED shouldn't stop the device from working,
// just means the key is only visible on Serial.
bool oled_init(uint8_t i2c_addr);

// fill(0) + text(state_line, 0, 4) + text2x(device_key, 24, 24) +
// text(battery_line, 0, 48) + a small WiFi status icon in the top-right
// corner (signal bars if wifi_connected, an "x" if not -- there's no WiFi
// glyph in the font table this reuses, so it's drawn directly) + show(),
// in one call -- this is the one thing main.py's update_oled() ever does,
// so there's no value in exposing fill/text/show as separate Python calls.
// No-op if oled_init() wasn't called or failed. battery_line can be an
// empty string ("") to render just the top two lines plus the icon.
void oled_update(const char *state_line, const char *device_key, const char *battery_line, bool wifi_connected);
