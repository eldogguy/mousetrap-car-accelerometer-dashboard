// BMI270 sensor backend, built on Bosch's vendored BMI270_SensorAPI (see
// vendor/bosch_bmi270/). Read/write/delay glue lives in bmi270_platform.c;
// this file just does init/config/read, matching Bosch's own
// bmi270_examples/accel_gyro/accel_gyro.c calling sequence (verified
// against the real reference example before writing this, not guessed).
//
// Ranges (±4g, ±500dps) intentionally match the L3GD20/LSM303DLHC backend's
// configuration so main.py's calibration/deadzone math sees the same
// physical dynamic range regardless of which sensor board is attached.
//
// I2C address 0x68 (BMI2_I2C_PRIM_ADDR, SDO tied low) -- if your breakout
// ties SDO high instead, change to 0x69 (BMI2_I2C_SEC_ADDR).
#include "imu_backend.h"

#ifdef IMU_BACKEND_BMI270

#include <string.h>
#include "bmi2.h"
#include "bmi270.h"
#include "driver/i2c_master.h"
#include "esp_log.h"

#define BMI270_I2C_ADDR 0x68
#define BMI270_READ_WRITE_LEN 32 // must match bmi270_platform.c's BMI270_READ_WRITE_LEN

extern BMI2_INTF_RETURN_TYPE bmi270_i2c_read(uint8_t reg_addr, uint8_t *reg_data, uint32_t len, void *intf_ptr);
extern BMI2_INTF_RETURN_TYPE bmi270_i2c_write(uint8_t reg_addr, const uint8_t *reg_data, uint32_t len, void *intf_ptr);
extern void bmi270_delay_us(uint32_t period, void *intf_ptr);

static const char *TAG = "imu_backend_bmi270";
static struct bmi2_dev s_bmi;
static i2c_master_dev_handle_t s_dev = NULL;

static bool set_accel_gyro_config(void) {
    struct bmi2_sens_config config[2];
    config[0].type = BMI2_ACCEL;
    config[1].type = BMI2_GYRO;

    if (bmi2_get_sensor_config(config, 2, &s_bmi) != BMI2_OK) {
        ESP_LOGE(TAG, "bmi2_get_sensor_config failed");
        return false;
    }

    config[0].cfg.acc.odr = BMI2_ACC_ODR_100HZ;
    config[0].cfg.acc.range = BMI2_ACC_RANGE_4G;
    config[0].cfg.acc.bwp = BMI2_ACC_NORMAL_AVG4;
    config[0].cfg.acc.filter_perf = BMI2_PERF_OPT_MODE;

    config[1].cfg.gyr.odr = BMI2_GYR_ODR_100HZ;
    config[1].cfg.gyr.range = BMI2_GYR_RANGE_500;
    config[1].cfg.gyr.bwp = BMI2_GYR_NORMAL_MODE;
    config[1].cfg.gyr.noise_perf = BMI2_PERF_OPT_MODE;
    config[1].cfg.gyr.filter_perf = BMI2_PERF_OPT_MODE;

    if (bmi2_set_sensor_config(config, 2, &s_bmi) != BMI2_OK) {
        ESP_LOGE(TAG, "bmi2_set_sensor_config failed");
        return false;
    }
    return true;
}

bool imu_backend_init(i2c_master_bus_handle_t bus) {
    i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = BMI270_I2C_ADDR,
        .scl_speed_hz = 400000,
    };
    if (i2c_master_bus_add_device(bus, &dev_config, &s_dev) != ESP_OK) {
        ESP_LOGE(TAG, "failed to add I2C device (addr=0x%02x)", BMI270_I2C_ADDR);
        return false;
    }

    memset(&s_bmi, 0, sizeof(s_bmi));
    s_bmi.intf = BMI2_I2C_INTF;
    s_bmi.read = bmi270_i2c_read;
    s_bmi.write = bmi270_i2c_write;
    s_bmi.delay_us = bmi270_delay_us;
    s_bmi.intf_ptr = (void *)s_dev;
    s_bmi.read_write_len = BMI270_READ_WRITE_LEN;
    s_bmi.config_file_ptr = NULL; // NULL -> use the default ~8KB config blob baked into bmi270.c

    // Burst-writes the config blob and validates chip ID -- the one BMI270
    // step with no equivalent in the L3GD20/LSM303 backend (see
    // native/README.md's Phase 2 notes on why this sensor needs it).
    int8_t rslt = bmi270_init(&s_bmi);
    if (rslt != BMI2_OK) {
        ESP_LOGE(TAG, "bmi270_init failed: %d (chip_id=0x%02x)", rslt, s_bmi.chip_id);
        return false;
    }
    ESP_LOGI(TAG, "bmi270_init OK, chip_id=0x%02x", s_bmi.chip_id);

    if (!set_accel_gyro_config()) {
        return false;
    }

    uint8_t sensor_list[2] = { BMI2_ACCEL, BMI2_GYRO };
    if (bmi2_sensor_enable(sensor_list, 2, &s_bmi) != BMI2_OK) {
        ESP_LOGE(TAG, "bmi2_sensor_enable failed");
        return false;
    }

    return true;
}

bool imu_backend_read(imu_raw_sample_t *out) {
    struct bmi2_sens_data data = { { 0 } };
    if (bmi2_get_sensor_data(&data, &s_bmi) != BMI2_OK) {
        return false;
    }
    if (!(data.status & BMI2_DRDY_ACC) || !(data.status & BMI2_DRDY_GYR)) {
        // Not fresh yet -- skip this tick, same contract as the
        // L3GD20/LSM303 backend returning false on a transient I2C hiccup.
        // Shouldn't happen often at 50Hz sampling against a 100Hz sensor ODR.
        return false;
    }

    // 16-bit signed output at the configured range -- same LSB-to-physical
    // conversion Bosch's own accel_gyro.c example uses (half_scale = 2^16/2).
    out->ax = ((float)data.acc.x / 32768.0f) * BMI2_ACC_RANGE_4G_VAL;
    out->ay = ((float)data.acc.y / 32768.0f) * BMI2_ACC_RANGE_4G_VAL;
    out->az = ((float)data.acc.z / 32768.0f) * BMI2_ACC_RANGE_4G_VAL;

    out->gx = ((float)data.gyr.x / 32768.0f) * BMI2_GYR_RANGE_500_VAL;
    out->gy = ((float)data.gyr.y / 32768.0f) * BMI2_GYR_RANGE_500_VAL;
    out->gz = ((float)data.gyr.z / 32768.0f) * BMI2_GYR_RANGE_500_VAL;

    return true;
}

#endif // IMU_BACKEND_BMI270
