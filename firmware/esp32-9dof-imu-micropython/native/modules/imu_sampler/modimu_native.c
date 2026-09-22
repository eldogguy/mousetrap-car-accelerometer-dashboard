// Python-facing surface of the native IMU sampler (Phase 1). Replaces the
// Phase 0 scaffold (imu_native.selftest()) with the real module: init(),
// pull(), stats(). See sampler_task.h/.c for the FreeRTOS task/queue this
// wraps, and imu_backend.h for the swappable sensor backend interface.
//
// pull() is meant to be called once per main-loop iteration in main.py's
// STATE_CALIBRATING/STATE_RECORDING branches, in place of the old
// gyro.read_dps()/accel.read_g() calls -- it writes directly into the same
// array.array buffers main.py already allocates (t_ms_buf, gx_buf, ...),
// via mp_get_buffer_raise(), so there's no extra copy or allocation on the
// hot path. (Python-visible name is "pull", not "drain" -- see the qstr
// collision note on the globals table entry below.)

#include "py/runtime.h"
#include "sampler_task.h"
#include "oled_ssd1306.h"

static mp_obj_t imu_native_init(mp_obj_t sda_obj, mp_obj_t scl_obj, mp_obj_t freq_obj) {
    int sda = mp_obj_get_int(sda_obj);
    int scl = mp_obj_get_int(scl_obj);
    mp_int_t freq = mp_obj_get_int(freq_obj);
    bool ok = imu_sampler_start(sda, scl, (uint32_t)freq);
    return mp_obj_new_bool(ok);
}
static MP_DEFINE_CONST_FUN_OBJ_3(imu_native_init_obj, imu_native_init);

// Fills one array.array buffer's raw pointer, checked against the
// (out_start + max_count) float/uint32-element bound the caller asked for
// -- a too-small buffer raises here rather than writing past its end.
static void *get_writable_buf(mp_obj_t obj, size_t need_elems) {
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(obj, &bufinfo, MP_BUFFER_WRITE);
    if (bufinfo.len < need_elems * 4) {
        mp_raise_ValueError(MP_ERROR_TEXT("buffer too small for start_idx+max_count"));
    }
    return bufinfo.buf;
}

static mp_obj_t imu_native_drain(size_t n_args, const mp_obj_t *args) {
    // args: t_buf, gx_buf, gy_buf, gz_buf, ax_buf, ay_buf, az_buf, start_idx, max_count
    (void)n_args; // fixed at 9 via MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(9, 9, ...) below
    int start_idx = mp_obj_get_int(args[7]);
    int max_count = mp_obj_get_int(args[8]);
    size_t need = (size_t)(start_idx + max_count);

    uint32_t *t_ms = (uint32_t *)get_writable_buf(args[0], need);
    float *gx = (float *)get_writable_buf(args[1], need);
    float *gy = (float *)get_writable_buf(args[2], need);
    float *gz = (float *)get_writable_buf(args[3], need);
    float *ax = (float *)get_writable_buf(args[4], need);
    float *ay = (float *)get_writable_buf(args[5], need);
    float *az = (float *)get_writable_buf(args[6], need);

    int n = imu_sampler_drain(t_ms, gx, gy, gz, ax, ay, az, start_idx, max_count);
    return mp_obj_new_int(n);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(imu_native_drain_obj, 9, 9, imu_native_drain);

// OLED now shares the sampler's I2C bus (see oled_ssd1306.c / sampler_task.h's
// imu_sampler_get_bus()) -- call imu_native.init() before oled_init().
static mp_obj_t imu_native_oled_init(mp_obj_t addr_obj) {
    uint8_t addr = (uint8_t)mp_obj_get_int(addr_obj);
    return mp_obj_new_bool(oled_init(addr));
}
static MP_DEFINE_CONST_FUN_OBJ_1(imu_native_oled_init_obj, imu_native_oled_init);

static mp_obj_t imu_native_oled_update(size_t n_args, const mp_obj_t *args) {
    // args: state_line, device_key, battery_line, wifi_connected. Fixed at
    // 4 via MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(4, 4, ...) below -- plain
    // MP_DEFINE_CONST_FUN_OBJ_3 only goes up to 3 fixed args on this
    // MicroPython version (see py/obj.h), one short of what this needs
    // now that the WiFi status icon was added.
    (void)n_args;
    const char *state_line = mp_obj_str_get_str(args[0]);
    const char *device_key = mp_obj_str_get_str(args[1]);
    const char *battery_line = mp_obj_str_get_str(args[2]);
    bool wifi_connected = mp_obj_is_true(args[3]);
    oled_update(state_line, device_key, battery_line, wifi_connected);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(imu_native_oled_update_obj, 4, 4, imu_native_oled_update);

static mp_obj_t imu_native_now_ms(void) {
    return mp_obj_new_int_from_uint(imu_sampler_now_ms());
}
static MP_DEFINE_CONST_FUN_OBJ_0(imu_native_now_ms_obj, imu_native_now_ms);

// Sigma-Delta Modulation audio output + native-timed playback (see
// audio_sdm.c) -- used by audio.py's play_pcm() for voice clips, in place of
// manually stepping machine.PWM's duty cycle from a machine.Timer callback,
// which sounded garbled on real hardware (root cause: machine.Timer's
// callback isn't a real hardware ISR on this port -- see audio_sdm.c's file
// header). These are MP_DEFINE_CONST_FUN_OBJ_2/0 (fixed-arity), so their
// object type is mp_obj_fun_builtin_fixed_t, not the _var_t used by
// imu_native_drain_obj above (which is VAR_BETWEEN).
extern const mp_obj_fun_builtin_fixed_t imu_native_sdm_init_obj;
extern const mp_obj_fun_builtin_fixed_t imu_native_sdm_deinit_obj;
extern const mp_obj_fun_builtin_fixed_t imu_native_audio_play_obj;

static mp_obj_t imu_native_stats(void) {
    imu_sampler_stats_t s;
    imu_sampler_get_stats(&s);
    mp_obj_t items[3] = {
        mp_obj_new_int_from_uint(s.produced),
        mp_obj_new_int_from_uint(s.dropped),
        mp_obj_new_int_from_uint(s.queue_high_water),
    };
    return mp_obj_new_tuple(3, items);
}
static MP_DEFINE_CONST_FUN_OBJ_0(imu_native_stats_obj, imu_native_stats);

static const mp_rom_map_elem_t imu_native_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_imu_native) },
    { MP_ROM_QSTR(MP_QSTR_init), MP_ROM_PTR(&imu_native_init_obj) },
    // Python-visible name is "pull", not "drain" -- MP_QSTR_drain collides
    // with an existing qstr elsewhere in the firmware (same class of issue
    // as the ping/selftest rename in Phase 0; verified "pull" has zero
    // matches in frozen_content.c before picking it).
    { MP_ROM_QSTR(MP_QSTR_pull), MP_ROM_PTR(&imu_native_drain_obj) },
    { MP_ROM_QSTR(MP_QSTR_stats), MP_ROM_PTR(&imu_native_stats_obj) },
    { MP_ROM_QSTR(MP_QSTR_now_ms), MP_ROM_PTR(&imu_native_now_ms_obj) },
    { MP_ROM_QSTR(MP_QSTR_oled_init), MP_ROM_PTR(&imu_native_oled_init_obj) },
    { MP_ROM_QSTR(MP_QSTR_oled_update), MP_ROM_PTR(&imu_native_oled_update_obj) },
    { MP_ROM_QSTR(MP_QSTR_sdm_init), MP_ROM_PTR(&imu_native_sdm_init_obj) },
    { MP_ROM_QSTR(MP_QSTR_sdm_deinit), MP_ROM_PTR(&imu_native_sdm_deinit_obj) },
    { MP_ROM_QSTR(MP_QSTR_audio_play), MP_ROM_PTR(&imu_native_audio_play_obj) },
};
static MP_DEFINE_CONST_DICT(imu_native_module_globals, imu_native_module_globals_table);

const mp_obj_module_t imu_native_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&imu_native_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_imu_native, imu_native_user_cmodule);
