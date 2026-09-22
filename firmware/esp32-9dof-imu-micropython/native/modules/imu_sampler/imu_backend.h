// Sensor backend interface. Exactly one backend_*.c file provides real
// content for these two functions, selected at compile time via
// -DIMU_BACKEND_L3GD20_LSM303 or -DIMU_BACKEND_BMI270 (see native/README.md).
// Both backend files are always added to the build; each guards its own
// body with #ifdef so only one actually compiles content -- the other
// becomes an empty (but valid) translation unit. This keeps the sampler
// task, queue, and Python-facing API completely unaware of which sensor
// chip is on the other end of the bus.
//
// Called only from imu_sampler_task (sampler_task.c) on the sampler's own
// FreeRTOS task/core -- never from the MicroPython task, never concurrently
// with itself.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "driver/i2c_master.h"

typedef struct {
    uint32_t t_ms;      // filled in by sampler_task.c after a successful read, not by the backend
    float gx, gy, gz;   // deg/s
    float ax, ay, az;   // g
} imu_raw_sample_t;

// Probes and configures the sensor(s) on the given I2C bus. Returns false
// on any WHO_AM_I mismatch or I2C failure -- mirrors the existing
// sensors.py begin() methods' fail-soft behavior (caller decides whether
// to refuse to arm, same as gyro_ok/accel_ok in main.py today).
bool imu_backend_init(i2c_master_bus_handle_t bus);

// Reads one sample. Returns false on I2C failure (caller should skip this
// tick and retry next period, not treat it as fatal -- transient I2C
// hiccups happen). t_ms is not set here; the caller stamps it immediately
// after a successful read.
bool imu_backend_read(imu_raw_sample_t *out);
