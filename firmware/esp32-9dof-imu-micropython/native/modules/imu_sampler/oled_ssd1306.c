// Native SSD1306 128x64 OLED driver -- direct C port of ssd1306.py (same
// init command sequence, same MONO_VLSB buffer layout). Text rendering
// reuses MicroPython's own built-in font table (extmod/font_petme128_8x8.h,
// the same one framebuf.text() already uses) via the identical
// column-major, LSB-at-top bit convention documented in modframebuf.c's
// framebuf_text() -- verified by reading that function before writing this,
// not guessed -- so output is pixel-identical to what main.py displayed via
// the Python driver before this migration. Flash cost is ~0: that font
// table is already compiled into the firmware for framebuf's own use.
//
// Moved to native C (and off Python's machine.I2C) specifically so it can
// share ONE I2C bus handle with the IMU sampler task instead of needing a
// second physical bus -- see sampler_task.h's imu_sampler_get_bus() and
// native/README.md for why sharing one handle between the 50Hz sampler task
// and infrequent OLED updates is safe (ESP-IDF's i2c_master driver has its
// own internal bus_lock_mux around every transaction, confirmed by reading
// i2c_master.c directly).
#include "oled_ssd1306.h"

#include <string.h>
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "sampler_task.h"
#include "extmod/font_petme128_8x8.h"

#define OLED_WIDTH 128
#define OLED_HEIGHT 64
#define OLED_PAGES (OLED_HEIGHT / 8)
#define OLED_BUF_SIZE (OLED_WIDTH * OLED_PAGES)

#define SET_CONTRAST 0x81
#define SET_ENTIRE_ON 0xA4
#define SET_NORM_INV 0xA6
#define SET_DISP 0xAE
#define SET_MEM_ADDR 0x20
#define SET_COL_ADDR 0x21
#define SET_PAGE_ADDR 0x22
#define SET_DISP_START_LINE 0x40
#define SET_SEG_REMAP 0xA0
#define SET_MUX_RATIO 0xA8
#define SET_COM_OUT_DIR 0xC0
#define SET_DISP_OFFSET 0xD3
#define SET_COM_PIN_CFG 0xDA
#define SET_DISP_CLK_DIV 0xD5
#define SET_PRECHARGE 0xD9
#define SET_VCOM_DESEL 0xDB
#define SET_CHARGE_PUMP 0x8D

#define I2C_TIMEOUT_MS 100

static const char *TAG = "oled_ssd1306";
static i2c_master_dev_handle_t s_dev = NULL;
static bool s_ok = false;
static uint8_t s_buf[OLED_BUF_SIZE];

static bool write_cmd(uint8_t cmd) {
    uint8_t buf[2] = { 0x80, cmd };
    return i2c_master_transmit(s_dev, buf, sizeof(buf), I2C_TIMEOUT_MS) == ESP_OK;
}

static bool write_data(const uint8_t *data, size_t len) {
    // One combined [0x40 control byte, data...] transaction -- the SSD1306
    // keeps auto-incrementing its column/page pointer per byte within a
    // single transaction, same as ssd1306.py's writeto(addr, b"\x40"+buf).
    static uint8_t scratch[OLED_BUF_SIZE + 1];
    scratch[0] = 0x40;
    memcpy(&scratch[1], data, len);
    return i2c_master_transmit(s_dev, scratch, len + 1, I2C_TIMEOUT_MS) == ESP_OK;
}

static bool init_display(void) {
    const uint8_t seq[] = {
        SET_DISP | 0x00,
        SET_MEM_ADDR, 0x00,
        SET_DISP_START_LINE | 0x00,
        SET_SEG_REMAP | 0x01,
        SET_MUX_RATIO, OLED_HEIGHT - 1,
        SET_COM_OUT_DIR | 0x08,
        SET_DISP_OFFSET, 0x00,
        SET_COM_PIN_CFG, (OLED_HEIGHT == 32) ? 0x02 : 0x12,
        SET_DISP_CLK_DIV, 0x80,
        SET_PRECHARGE, 0xF1,
        SET_VCOM_DESEL, 0x30,
        SET_CONTRAST, 0xFF,
        SET_ENTIRE_ON,
        SET_NORM_INV,
        SET_CHARGE_PUMP, 0x14,
        SET_DISP | 0x01,
    };
    for (size_t i = 0; i < sizeof(seq); i++) {
        if (!write_cmd(seq[i])) {
            return false;
        }
    }
    return true;
}

static void set_pixel(int x, int y, int col) {
    if (x < 0 || x >= OLED_WIDTH || y < 0 || y >= OLED_HEIGHT) {
        return;
    }
    int idx = (y / 8) * OLED_WIDTH + x;
    uint8_t bit = 1 << (y % 8);
    if (col) {
        s_buf[idx] |= bit;
    } else {
        s_buf[idx] &= ~bit;
    }
}

// scale=1 for text(), scale=2 for text2x() -- same font, same
// column-major/LSB-at-top decode framebuf_text() uses, blitted at an
// integer pixel multiple instead of framebuf's native 1:1 only.
static void draw_text(const char *str, int x0, int y0, int col, int scale) {
    for (; *str; str++) {
        int chr = (uint8_t)*str;
        if (chr < 32 || chr > 127) {
            chr = 127;
        }
        const uint8_t *chr_data = &font_petme128_8x8[(chr - 32) * 8];
        for (int j = 0; j < 8; j++, x0 += scale) {
            unsigned int vline = chr_data[j];
            for (int y = 0; vline; vline >>= 1, y++) {
                if (vline & 1) {
                    for (int sy = 0; sy < scale; sy++) {
                        for (int sx = 0; sx < scale; sx++) {
                            set_pixel(x0 + sx, y0 + y * scale + sy, col);
                        }
                    }
                }
            }
        }
    }
}

// Small WiFi status icon, top-right corner (x=118-127, y=0-7) -- drawn
// directly via set_pixel() rather than the font table, since
// font_petme128_8x8 is plain ASCII with no WiFi glyph. Connected: three
// ascending bars (a minimal signal-bars icon, bottom-aligned). Not
// connected: an explicit "x" in the same footprint, not just blank space
// -- a blank icon would be ambiguous with "hasn't rendered yet"/a glitch,
// where main.py wants this to be a clear, deliberate "no WiFi" signal.
static void draw_wifi_icon(bool connected) {
    if (connected) {
        for (int y = 6; y <= 7; y++) { set_pixel(118, y, 1); set_pixel(119, y, 1); }
        for (int y = 4; y <= 7; y++) { set_pixel(122, y, 1); set_pixel(123, y, 1); }
        for (int y = 2; y <= 7; y++) { set_pixel(126, y, 1); set_pixel(127, y, 1); }
    } else {
        for (int i = 0; i <= 7; i++) {
            set_pixel(118 + i, i, 1);
            set_pixel(118 + i, 7 - i, 1);
        }
    }
}

bool oled_init(uint8_t i2c_addr) {
    i2c_master_bus_handle_t bus = imu_sampler_get_bus();
    if (bus == NULL) {
        ESP_LOGE(TAG, "no I2C bus yet -- call imu_native.init() first");
        return false;
    }

    i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = i2c_addr,
        .scl_speed_hz = 400000,
    };
    if (i2c_master_bus_add_device(bus, &dev_config, &s_dev) != ESP_OK) {
        ESP_LOGE(TAG, "failed to add I2C device (addr=0x%02x)", i2c_addr);
        s_ok = false;
        return false;
    }

    s_ok = init_display();
    if (!s_ok) {
        ESP_LOGE(TAG, "init command sequence failed");
        return false;
    }

    memset(s_buf, 0, OLED_BUF_SIZE);
    return true;
}

void oled_update(const char *state_line, const char *device_key, const char *battery_line, bool wifi_connected) {
    if (!s_ok) {
        return;
    }
    memset(s_buf, 0, OLED_BUF_SIZE);
    draw_text(state_line, 0, 4, 1, 1);
    draw_text(device_key, 24, 24, 1, 2);
    draw_text(battery_line, 0, 48, 1, 1);
    draw_wifi_icon(wifi_connected);

    if (!write_cmd(SET_COL_ADDR) || !write_cmd(0) || !write_cmd(OLED_WIDTH - 1) ||
        !write_cmd(SET_PAGE_ADDR) || !write_cmd(0) || !write_cmd(OLED_PAGES - 1) ||
        !write_data(s_buf, OLED_BUF_SIZE)) {
        ESP_LOGE(TAG, "show() transaction failed");
    }
}
