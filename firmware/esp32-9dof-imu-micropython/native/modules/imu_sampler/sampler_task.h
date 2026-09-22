// FreeRTOS-backed IMU sampling. Owns a dedicated I2C bus and a real-time
// task, pinned to core 1 (same core as MicroPython -- see native/README.md
// / the approved plan for why: WiFi/BT own core 0 entirely and contesting
// it directly competes with their own real-time deadlines) at a priority
// above MicroPython's interpreter task, so the FreeRTOS scheduler preempts
// it deterministically on every sample tick regardless of what Python is
// doing -- true preemption, not GIL-cooperative handoff.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "driver/i2c_master.h"

// Starts the I2C bus + backend + sampler task. Idempotent -- a second call
// after a successful first one just returns true without doing anything.
// Returns false if the I2C bus couldn't be created, the backend failed to
// initialize (sensor not found/WHO_AM_I mismatch), or the task/queue
// couldn't be allocated.
bool imu_sampler_start(int sda_pin, int scl_pin, uint32_t freq_hz);

// Returns the bus handle created by imu_sampler_start(), or NULL if it
// hasn't been called yet (or failed before creating the bus). Lets other
// native devices on the same physical bus (e.g. oled_ssd1306.c) add
// themselves via i2c_master_bus_add_device() on the SAME handle, instead of
// each independently calling i2c_new_master_bus() -- ESP-IDF only allows
// one bus handle per physical port, and its internal bus_lock_mux (verified
// by reading i2c_master.c's s_i2c_synchronous_transaction()) is exactly
// what makes sharing one handle between the 50Hz sampler task and
// infrequent OLED updates safe, with no locking of our own needed.
i2c_master_bus_handle_t imu_sampler_get_bus(void);

// Non-blocking drain of whatever samples have queued up since the last
// call. Writes into the caller's flat arrays starting at out_start, up to
// max_count samples. Returns how many were actually written. Meant to be
// called once per MicroPython main-loop iteration; safe to call from the
// MicroPython task only (not from the sampler task itself).
int imu_sampler_drain(uint32_t *t_ms, float *gx, float *gy, float *gz,
    float *ax, float *ay, float *az, int out_start, int max_count);

typedef struct {
    uint32_t produced;         // total samples read from the sensor since start
    uint32_t dropped;          // times a sample couldn't be queued (queue was full -- Python-side drain fell behind)
    uint32_t queue_high_water; // largest number of items ever seen queued at once
} imu_sampler_stats_t;

void imu_sampler_get_stats(imu_sampler_stats_t *out);

// Current value of the sampler's internal clock (same clock t_ms in each
// sample is stamped from), in ms since imu_sampler_start() was called.
// Since that call happens once at boot while main.py wants each recording's
// t=0 to be "when this experiment's calibration began," main.py calls this
// once per experiment and subtracts it from every sample's raw t_ms to get
// a relative, per-experiment timestamp -- see drain_samples() in main.py.
uint32_t imu_sampler_now_ms(void);
