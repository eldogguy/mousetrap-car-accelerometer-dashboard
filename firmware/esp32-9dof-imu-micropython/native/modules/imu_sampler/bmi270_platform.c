// I2C read/write/delay glue between Bosch's BMI270_SensorAPI (vendored in
// vendor/bosch_bmi270/) and the same ESP-IDF driver/i2c_master.h API
// backend_l3gd20_lsm303.c uses. intf_ptr is the i2c_master_dev_handle_t
// itself, cast to/from void* -- no extra context struct needed since we
// only ever talk to one BMI270 on one bus.
//
// Only compiled when the BMI270 backend is selected -- see backend_bmi270.c.
#include "imu_backend.h"

#ifdef IMU_BACKEND_BMI270

#include <string.h>
#include "bmi2_defs.h"
#include "driver/i2c_master.h"
#include "esp_rom_sys.h" // esp_rom_delay_us
#include "esp_log.h"

#define I2C_TIMEOUT_MS 100 // config-blob burst writes are larger than a normal register write; generous but not silent-forever

static const char *TAG = "bmi270_platform";

// Bosch chunks any transfer larger than dev->read_write_len into multiple
// write() calls internally (see bmi270_init()'s config-blob load) -- this
// just needs to comfortably hold one chunk plus the register-address byte.
// Set bmi->read_write_len to this same value in backend_bmi270.c.
#define BMI270_READ_WRITE_LEN 32

BMI2_INTF_RETURN_TYPE bmi270_i2c_read(uint8_t reg_addr, uint8_t *reg_data, uint32_t len, void *intf_ptr) {
    i2c_master_dev_handle_t dev = (i2c_master_dev_handle_t)intf_ptr;
    esp_err_t err = i2c_master_transmit_receive(dev, &reg_addr, 1, reg_data, len, I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "read reg=0x%02x len=%lu failed: %d", reg_addr, (unsigned long)len, err);
        return BMI2_E_COM_FAIL;
    }
    return BMI2_OK;
}

BMI2_INTF_RETURN_TYPE bmi270_i2c_write(uint8_t reg_addr, const uint8_t *reg_data, uint32_t len, void *intf_ptr) {
    i2c_master_dev_handle_t dev = (i2c_master_dev_handle_t)intf_ptr;
    if (len > BMI270_READ_WRITE_LEN) {
        // Shouldn't happen given bmi->read_write_len below, but don't
        // silently overflow the stack buffer if it ever does.
        ESP_LOGE(TAG, "write len=%lu exceeds BMI270_READ_WRITE_LEN=%d", (unsigned long)len, BMI270_READ_WRITE_LEN);
        return BMI2_E_COM_FAIL;
    }
    uint8_t buf[BMI270_READ_WRITE_LEN + 1];
    buf[0] = reg_addr;
    memcpy(&buf[1], reg_data, len);
    esp_err_t err = i2c_master_transmit(dev, buf, len + 1, I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "write reg=0x%02x len=%lu failed: %d", reg_addr, (unsigned long)len, err);
        return BMI2_E_COM_FAIL;
    }
    return BMI2_OK;
}

void bmi270_delay_us(uint32_t period, void *intf_ptr) {
    (void)intf_ptr;
    esp_rom_delay_us(period);
}

#endif // IMU_BACKEND_BMI270
