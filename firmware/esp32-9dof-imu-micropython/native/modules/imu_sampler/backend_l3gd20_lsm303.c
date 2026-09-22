// Direct C port of sensors.py's L3GD20 + LSM303DLHCAccel drivers (same
// register addresses, same values, same sensitivity constants, same
// left-justified 12-bit shift quirk on the accelerometer's high-res
// output) -- Phase 1's job is validating the new FreeRTOS/queue
// concurrency architecture on hardware already in hand, not inventing new
// sensor math. Magnetometer intentionally omitted (see native/README.md /
// the approved plan: slow, low-rate, already-optional, and the BMI270
// backend this is a stand-in for has none anyway).
#include "imu_backend.h"

#ifdef IMU_BACKEND_L3GD20_LSM303

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

#define L3GD20_ADDR 0x6B
#define L3GD20_REG_WHO_AM_I 0x0F
#define L3GD20_REG_CTRL_REG1 0x20
#define L3GD20_REG_CTRL_REG4 0x23
#define L3GD20_REG_OUT_X_L 0x28
#define L3GD20_WHO_AM_I_VALUE 0xD4
#define L3GD20_SCALE_500DPS 0x01
#define L3GD20_SENSITIVITY_500DPS 0.0175f

#define LSM303_ACCEL_ADDR 0x19
#define LSM303_REG_CTRL_REG1_A 0x20
#define LSM303_REG_CTRL_REG4_A 0x23
#define LSM303_REG_OUT_X_L_A 0x28
#define LSM303_SENSITIVITY_G_PER_LSB 0.002f

#define AUTO_INCREMENT 0x80
#define I2C_TIMEOUT_MS 50 // generous for a 400kHz bus; a real hang this task cares about, not silent wait-forever

static const char *TAG = "imu_backend_l3gd20_lsm303";

static i2c_master_dev_handle_t s_gyro_dev = NULL;
static i2c_master_dev_handle_t s_accel_dev = NULL;

static inline int16_t decode_le16(const uint8_t *p) {
    return (int16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static bool write_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t value) {
    uint8_t buf[2] = { reg, value };
    return i2c_master_transmit(dev, buf, sizeof(buf), I2C_TIMEOUT_MS) == ESP_OK;
}

static bool read_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *out) {
    return i2c_master_transmit_receive(dev, &reg, 1, out, 1, I2C_TIMEOUT_MS) == ESP_OK;
}

static bool read_burst6(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t out[6]) {
    uint8_t addr = reg | AUTO_INCREMENT;
    return i2c_master_transmit_receive(dev, &addr, 1, out, 6, I2C_TIMEOUT_MS) == ESP_OK;
}

static bool gyro_init(i2c_master_bus_handle_t bus) {
    i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = L3GD20_ADDR,
        .scl_speed_hz = 400000,
    };
    if (i2c_master_bus_add_device(bus, &dev_config, &s_gyro_dev) != ESP_OK) {
        ESP_LOGE(TAG, "gyro: failed to add I2C device");
        return false;
    }

    uint8_t who = 0;
    if (!read_reg(s_gyro_dev, L3GD20_REG_WHO_AM_I, &who) || who != L3GD20_WHO_AM_I_VALUE) {
        ESP_LOGE(TAG, "gyro: WHO_AM_I mismatch (got 0x%02x, want 0x%02x)", who, L3GD20_WHO_AM_I_VALUE);
        return false;
    }

    // ODR=100Hz, cutoff=12.5Hz, normal power mode, X/Y/Z enabled
    if (!write_reg(s_gyro_dev, L3GD20_REG_CTRL_REG1, 0x0F)) {
        return false;
    }
    // Full-scale select: 500dps
    if (!write_reg(s_gyro_dev, L3GD20_REG_CTRL_REG4, (L3GD20_SCALE_500DPS & 0x03) << 4)) {
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(100));
    return true;
}

static bool accel_init(i2c_master_bus_handle_t bus) {
    i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = LSM303_ACCEL_ADDR,
        .scl_speed_hz = 400000,
    };
    if (i2c_master_bus_add_device(bus, &dev_config, &s_accel_dev) != ESP_OK) {
        ESP_LOGE(TAG, "accel: failed to add I2C device");
        return false;
    }

    // 100Hz ODR, normal power, X/Y/Z enabled -> 0b01010111
    if (!write_reg(s_accel_dev, LSM303_REG_CTRL_REG1_A, 0x57)) {
        return false;
    }
    // No WHO_AM_I on this sensor -- confirm presence by reading back what was just written.
    uint8_t readback = 0;
    if (!read_reg(s_accel_dev, LSM303_REG_CTRL_REG1_A, &readback) || readback != 0x57) {
        ESP_LOGE(TAG, "accel: CTRL_REG1_A readback mismatch (got 0x%02x)", readback);
        return false;
    }
    // High-resolution mode, +/-4g range -> 0b00011000
    if (!write_reg(s_accel_dev, LSM303_REG_CTRL_REG4_A, 0x18)) {
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(10));
    return true;
}

bool imu_backend_init(i2c_master_bus_handle_t bus) {
    return gyro_init(bus) && accel_init(bus);
}

bool imu_backend_read(imu_raw_sample_t *out) {
    uint8_t gbuf[6];
    if (!read_burst6(s_gyro_dev, L3GD20_REG_OUT_X_L, gbuf)) {
        return false;
    }
    uint8_t abuf[6];
    if (!read_burst6(s_accel_dev, LSM303_REG_OUT_X_L_A, abuf)) {
        return false;
    }

    out->gx = decode_le16(&gbuf[0]) * L3GD20_SENSITIVITY_500DPS;
    out->gy = decode_le16(&gbuf[2]) * L3GD20_SENSITIVITY_500DPS;
    out->gz = decode_le16(&gbuf[4]) * L3GD20_SENSITIVITY_500DPS;

    // High-res output is left-justified in the 16-bit register; shift
    // right by 4 to get the 12-bit signed value the sensitivity expects
    // (matches sensors.py's LSM303DLHCAccel.read_g() exactly).
    out->ax = (decode_le16(&abuf[0]) >> 4) * LSM303_SENSITIVITY_G_PER_LSB;
    out->ay = (decode_le16(&abuf[2]) >> 4) * LSM303_SENSITIVITY_G_PER_LSB;
    out->az = (decode_le16(&abuf[4]) >> 4) * LSM303_SENSITIVITY_G_PER_LSB;

    return true;
}

#endif // IMU_BACKEND_L3GD20_LSM303
