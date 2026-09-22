// Sigma-Delta Modulation audio output -- ESP-IDF's real hardware SDM
// peripheral, used specifically for voice-clip playback (play_pcm() in
// audio.py). tone() (simple beeps) stays on plain machine.PWM -- it
// already sounds correct and doesn't need any of this.
//
// History of why this file looks the way it does (two root causes, found in
// sequence, both confirmed on real hardware):
//
// 1. Voice clips sounded garbled through the original approach: a
//    machine.Timer(mode=PERIODIC) driving a Python callback that stepped
//    machine.PWM's duty cycle once per PCM sample. Switching the *output
//    stage* from manually-stepped PWM duty to this SDM peripheral (real
//    hardware noise-shaping) did NOT fix it -- still garbled. That ruled out
//    the modulation scheme and pointed further upstream, at how samples were
//    being clocked out in the first place.
//
// 2. Root cause: reading MicroPython's own ports/esp32/machine_timer.c
//    confirms `machine.Timer(..., hard=True)` raises
//    "hard Timers are not implemented" on this port, and even in the
//    (default) non-hard mode, the timer's real hardware ISR
//    (machine_timer_isr) does not run the Python callback itself -- it only
//    calls mp_sched_schedule() to queue it for later. The callback actually
//    executes whenever the main interpreter thread next services its
//    scheduled-callback queue (between VM opcodes, or when woken from a
//    blocking HAL wait) -- not on a real fixed hardware tick. That's fine
//    for something like a slow LED blink, but nowhere near deterministic
//    enough for clocking 8000 samples/sec: the actual call times are bursty
//    depending on what else the interpreter is doing, which is exactly what
//    "garbled/warped" audio sounds like. This also explains why tone()'s
//    simple beeps always sounded correct: a beep just holds one fixed
//    frequency/duty for its whole duration and never depends on Timer
//    per-callback precision at all.
//
// Fix: imu_native_audio_play() below clocks samples from a real ESP-IDF
// gptimer hardware alarm, whose ISR (play_alarm_cb) runs in genuine
// interrupt context and touches only the SDM peripheral directly -- no
// Python bytecode, no scheduler, on the per-sample hot path at all. The
// calling Python thread just blocks on a semaphore until playback finishes.
//
// MicroPython doesn't expose ESP-IDF's driver/sdm.h (or a hard-realtime
// timer) at all -- this wraps just enough native functionality for
// audio.py's needs.
//
// Uses the *same* GPIO as BUZZER_PIN (config.py) -- SDM output is still a
// 1-bit digital signal needing the same RC-filter-into-amp chain tone()
// already uses, so no rewiring, just a different peripheral driving the
// same pin at different times. audio.py is responsible for deinit'ing its
// machine.PWM object before calling sdm_init() (and re-creating it after
// sdm_deinit()) since only one peripheral can own a given GPIO's output
// routing at a time -- same class of constraint as the I2C bus/port
// ownership documented in native/README.md, just for a different
// peripheral pairing.
#include "py/runtime.h"
#include "py/mperrno.h"
#include "driver/sdm.h"
#include "driver/gptimer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static sdm_channel_handle_t s_chan = NULL;

static mp_obj_t imu_native_sdm_init(mp_obj_t gpio_obj, mp_obj_t rate_obj) {
    if (s_chan != NULL) {
        sdm_channel_disable(s_chan);
        sdm_del_channel(s_chan);
        s_chan = NULL;
    }

    sdm_config_t config = {
        .gpio_num = mp_obj_get_int(gpio_obj),
        .clk_src = SDM_CLK_SRC_DEFAULT,
        // This is SDM's own hardware oversampling/toggle rate, NOT the PCM
        // audio sample rate -- see config.py's SDM_CARRIER_HZ comment. On
        // the ESP32-S3 anything below roughly (APB clock / 256) is rejected
        // outright by sdm_new_channel() (confirmed on real hardware: passing
        // the 8000Hz PCM rate here failed every time).
        .sample_rate_hz = (uint32_t)mp_obj_get_int(rate_obj),
    };

    if (sdm_new_channel(&config, &s_chan) != ESP_OK) {
        s_chan = NULL;
        return mp_const_false;
    }
    if (sdm_channel_enable(s_chan) != ESP_OK) {
        sdm_del_channel(s_chan);
        s_chan = NULL;
        return mp_const_false;
    }
    return mp_const_true;
}
MP_DEFINE_CONST_FUN_OBJ_2(imu_native_sdm_init_obj, imu_native_sdm_init);

static mp_obj_t imu_native_sdm_deinit(void) {
    if (s_chan != NULL) {
        sdm_channel_disable(s_chan);
        sdm_del_channel(s_chan);
        s_chan = NULL;
    }
    return mp_const_none;
}
MP_DEFINE_CONST_FUN_OBJ_0(imu_native_sdm_deinit_obj, imu_native_sdm_deinit);

// --- Native-timed PCM playback (see file header for why this exists) ---

static const uint8_t *s_play_buf = NULL;
static volatile size_t s_play_len = 0;
static volatile size_t s_play_idx = 0;
static int s_play_max_density = 90;
static SemaphoreHandle_t s_play_done_sem = NULL;

// Runs in real gptimer ISR context -- IRAM-safe candidate, kept tiny and
// allocation-free on purpose (one array index + one SDM register write).
static bool IRAM_ATTR play_alarm_cb(gptimer_handle_t timer, const gptimer_alarm_event_data_t *edata, void *user_ctx) {
    size_t i = s_play_idx;
    if (i >= s_play_len) {
        BaseType_t high_task_woken = pdFALSE;
        xSemaphoreGiveFromISR(s_play_done_sem, &high_task_woken);
        return high_task_woken == pdTRUE;
    }
    // 8-bit unsigned PCM sample (0..255) -> signed SDM pulse density,
    // scaled by s_play_max_density (a caller-supplied volume knob, see
    // audio.py's VOICE_MAX_DENSITY) rather than the full int8 [-128, 127]
    // range -- ESP-IDF's sdm.h doc note recommends capping at [-90, 90] for
    // better randomness, but on real hardware even that full-recommended
    // range clipped the PAM8304/speaker audibly, so this is tunable from
    // Python (config.py) without a firmware rebuild.
    int density = ((int)s_play_buf[i] - 128) * s_play_max_density / 128;
    sdm_channel_set_pulse_density(s_chan, (int8_t)density);
    s_play_idx = i + 1;
    return false;
}

// Blocking: clocks the whole buffer out at sample_rate_hz via a dedicated
// gptimer alarm and only returns once playback completes. sdm_init() must
// already have been called (and its channel still enabled) before this.
// max_density caps the output swing (see play_alarm_cb) -- pass a smaller
// value to turn the volume down without touching the PCM data itself.
static mp_obj_t imu_native_audio_play(mp_obj_t data_obj, mp_obj_t rate_obj, mp_obj_t max_density_obj) {
    if (s_chan == NULL) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("sdm_init() must be called before audio_play()"));
    }

    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(data_obj, &bufinfo, MP_BUFFER_READ);
    if (bufinfo.len == 0) {
        return mp_const_none;
    }

    s_play_max_density = mp_obj_get_int(max_density_obj);

    if (s_play_done_sem == NULL) {
        s_play_done_sem = xSemaphoreCreateBinary();
    }

    s_play_buf = (const uint8_t *)bufinfo.buf;
    s_play_len = bufinfo.len;
    s_play_idx = 0;

    gptimer_handle_t timer = NULL;
    gptimer_config_t timer_config = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1000000, // 1us ticks -- plenty of headroom for audio-rate periods
    };
    if (gptimer_new_timer(&timer_config, &timer) != ESP_OK) {
        mp_raise_OSError(MP_EIO);  // py/mperrno.h's MP_EIO, not errno.h's
    }

    gptimer_event_callbacks_t cbs = { .on_alarm = play_alarm_cb };
    gptimer_register_event_callbacks(timer, &cbs, NULL);
    gptimer_enable(timer);

    mp_int_t rate_hz = mp_obj_get_int(rate_obj);
    gptimer_alarm_config_t alarm_config = {
        .alarm_count = 1000000 / (uint32_t)rate_hz,
        .reload_count = 0,
        .flags.auto_reload_on_alarm = true,
    };
    gptimer_set_alarm_action(timer, &alarm_config);
    gptimer_start(timer);

    xSemaphoreTake(s_play_done_sem, portMAX_DELAY);

    gptimer_stop(timer);
    gptimer_disable(timer);
    gptimer_del_timer(timer);
    s_play_buf = NULL;

    return mp_const_none;
}
MP_DEFINE_CONST_FUN_OBJ_3(imu_native_audio_play_obj, imu_native_audio_play);
