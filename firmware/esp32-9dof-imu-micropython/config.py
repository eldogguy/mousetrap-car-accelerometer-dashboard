"""Pin assignments and constants for the ESP32-S3 IMU logger.

Sensor is a BMI270 (6-DoF: accel + gyro, no magnetometer) as of the native
C/FreeRTOS rewrite -- see native/README.md. Pins below reflect the actual
custom board (received and pinned out as of this revision) -- not the
breadboard/dev-board pins used earlier in the rewrite. No SD card slot on
this board, so SD export doesn't exist here at all (see main.py/README for
the earlier SD-card path this board doesn't have).
"""

# ---------------------------------------------------------------------------
# Feature flags.
# ---------------------------------------------------------------------------
ENABLE_WIFI_EXPORT = True

# ---------------------------------------------------------------------------
# Buttons (all wired active-LOW: button between GPIO and GND, using the
# internal pull-up, no external resistor needed)
# ---------------------------------------------------------------------------
BTN_START_DISARM_PIN = 4   # short press: toggle recording on/off
BTN_RESET_PIN = 5          # short press: stop + clear the buffer
BTN_EXPORT_PIN = 6         # short press: write CSV to SD / upload over WiFi
DEBOUNCE_MS = 30

# ---------------------------------------------------------------------------
# Status LED (optional). Any standard LED + ~220-330 ohm resistor to GND.
# ---------------------------------------------------------------------------
STATUS_LED_PIN = 15
LED_BLINK_INTERVAL_MS = 250  # blink rate while waiting to be placed / calibrating

# ---------------------------------------------------------------------------
# Audio out (see audio.py). BUZZER_PIN drives a 1-bit PWM signal, meant to
# feed an external RC low-pass filter -> small amplifier -> speaker. MicroPython's
# machine.I2S on this board doesn't expose a real hardware PDM TX mode (checked
# directly on device: machine.I2S only offers MONO/STEREO/TX/RX, no PDM
# constant) -- PWM duty modulated at AUDIO_SAMPLE_RATE_HZ via a hardware
# Timer is the practical equivalent for driving that same RC-filter-into-amp
# chain, and was verified stable on real hardware at 8000Hz (7999/8000
# actual timer fires measured over 2s with real duty_u16() calls).
# ---------------------------------------------------------------------------
BUZZER_PIN = 16
AUDIO_CARRIER_HZ = 62500     # PWM switching frequency (tone() beeps only) -- well above audible range
AUDIO_SAMPLE_RATE_HZ = 8000  # voice clip playback rate; matches how voice_*.pcm were encoded

# SDM's "sample_rate_hz" (see audio_sdm.c) is the hardware's own pulse/toggle
# rate, NOT the audio sample rate above -- it's the oversampling clock the
# 1-bit output physically switches at, analogous to AUDIO_CARRIER_HZ for the
# old PWM path but with a much higher hard floor. On the ESP32-S3, ESP-IDF's
# SDM driver rejects anything below (APB clock / 256) -- ~312.5kHz off an
# 80MHz APB -- sdm_new_channel() returns ESP_ERR_INVALID_ARG below that
# (confirmed on real hardware: AUDIO_SAMPLE_RATE_HZ, 8000, failed outright).
# 1MHz is comfortably above that floor. Per-PCM-sample clocking itself
# happens entirely in native C now (imu_native.audio_play(), a real gptimer
# hardware ISR) -- not a machine.Timer callback -- see audio.py's module
# docstring for why that mattered.
SDM_CARRIER_HZ = 1000000

# Caps the SDM output swing (imu_native.audio_play()'s max_density arg),
# i.e. voice-clip volume -- separate from the PCM data itself, so this can
# be tuned by re-uploading just this file, no firmware rebuild needed.
# ESP-IDF's sdm.h doc note recommends capping at 90 (of the full int8
# -128..127 range) purely for slightly better dithering "randomness," not
# because higher values are unsafe -- our PCM data's actual peak deviation
# from center (~99 of 128 at the loudest sample) still fits well within
# int8 range even at the full 127, so there's no digital clipping risk in
# pushing past 90 if it turns out to still be needed. See native/README.md
# for the RC filter debugging story this was tangled up with.
VOICE_MAX_DENSITY = 90
TONE_LOW_HZ = 440            # low-pitched countdown beep ("3, 2, 1")
TONE_GO_HZ = 1046            # higher-pitched "go" beep (~C6)
TONE_DEFAULT_HZ = 1000       # generic indicator beeps (arm, disarm, export result)

# The custom board drives the RC-filtered PWM signal above into a real
# amplifier IC (PAM8304, mono Class-D) instead of a bare speaker. Its SD
# (shutdown) pin is active-LOW: pulled low, the amp is in its lowest-power
# state and outputs nothing at all -- audio.py drives this high only while
# actually playing a tone/clip, then back low afterward, so the amp isn't
# needlessly drawing power the rest of the time on a battery-powered board.
PAM8304_SHUTDOWN_PIN = 17  # corrected from an initial wrong guess of 14 -- confirmed against the actual board

# ---------------------------------------------------------------------------
# Arm sequence: press Start/Disarm -> placement delay (time to set the car
# down on the starting line without jostling it) -> automatic calibration
# (car must be completely still) -> "go" signal -> recording, until the
# button is pressed again or the buffer fills.
# ---------------------------------------------------------------------------
ARM_PLACEMENT_DELAY_SECONDS = 10.0
CALIBRATION_SECONDS = 5.0
CALIBRATION_DEADZONE_SIGMA = 3.0  # matches analysis/analyze_run.py's default

# ---------------------------------------------------------------------------
# I2C bus, owned entirely by native C code (imu_native module) -- the BMI270
# and the SSD1306 OLED both live on this one bus, added as two separate
# devices on one shared bus handle rather than needing a bus each. Verified
# safe by reading ESP-IDF's i2c_master.c directly: every transaction goes
# through a real mutex (bus_lock_mux), so the 50Hz sampler task and
# infrequent OLED updates can't corrupt each other. See native/README.md.
# Nothing in Python touches this bus directly anymore -- no more
# machine.I2C(0, ...) here -- since only one i2c_master_bus_handle_t can own
# a physical port at a time, and the native side needs to be that one owner.
# ---------------------------------------------------------------------------
I2C_SDA_PIN = 9
I2C_SCL_PIN = 10
I2C_FREQ_HZ = 400000

# BMI270's I2C address (0x68, SDO tied low) is compiled into
# native/modules/imu_sampler/backend_bmi270.c, not configurable from here --
# edit that file (and rebuild the native firmware) if your board ties SDO
# high instead (0x69).

# SSD1306 128x64 OLED, same I2C bus as the BMI270 above, no new GPIO pins
# needed. Displays the device's pairing key + current state.
OLED_I2C_ADDR = 0x3C  # 0x3C is the common default; some modules are 0x3D

# Complementary filter blend factor for the on-device roll/pitch estimate:
# closer to 1.0 trusts the (drift-free but noisy) gyro integration more,
# closer to 0.0 trusts the (noisy but drift-free) accelerometer more.
COMPLEMENTARY_FILTER_ALPHA = 0.98

# ---------------------------------------------------------------------------
# Sampling
#
# RAM budget: each sample is 13 float32 values + 1 uint32 timestamp, stored
# in parallel array.array buffers (52 bytes/sample). This board's MicroPython
# heap lives in its 2MB quad PSRAM, not the ESP32-S3's much smaller internal
# SRAM (confirmed on real hardware: gc.mem_free() reports ~2MB free at
# boot), so per-sample RAM is not a tight constraint here the way an earlier
# revision of this comment assumed -- 120s at 50Hz = 6000 samples = ~305KB,
# comfortably under 20% of the free heap, leaving plenty of margin for
# everything else (calibration_samples, WiFi buffers, GC overhead).
#
# Sampling itself is a native FreeRTOS task now (see native/README.md), not
# a plain Python loop, so it isn't the limiting factor on sample rate either
# -- SAMPLE_RATE_HZ is set for the sensor/analysis needs, not to work around
# MicroPython interpreter speed the way an earlier revision of this comment
# assumed.
# ---------------------------------------------------------------------------
SAMPLE_RATE_HZ = 50
MAX_RECORD_SECONDS = 120
MAX_SAMPLES = SAMPLE_RATE_HZ * MAX_RECORD_SECONDS

# ---------------------------------------------------------------------------
# Battery + charging (BQ25185 linear Li-ion/LiPo charger). STAT1/STAT2 are
# the charger's open-drain status outputs -- read together as a pair to
# decode charge state (charging/done/fault/no-battery), not individually.
# Decode logic lives in battery.py, using the exact STAT1/STAT2 state table
# from TI's datasheet (SLUSF65B, Table 6-2) -- not guessed at.
#
# Pin numbers and polarity confirmed directly against the schematic
# (ACCELEROMETER V2.1.sch), not assumed: STAT1 -> GPIO47, STAT2 -> GPIO21
# (the two were swapped in an earlier version of this file -- fixed here).
# Both pins pull up 1k to the same +3.3V rail the ESP32 itself runs on (via
# the BGOOD/CHRG status LEDs' pull-up resistors, R6/R7) with no
# transistor/level-shift stage in between, so the ESP32 reads the
# datasheet's STAT1/STAT2 polarity directly -- no inversion needed.
BQ25185_STAT1_PIN = 47
BQ25185_STAT2_PIN = 21

# BATTERY_LEVEL_PIN sits at the midpoint of a 1M/1M divider (R14/R15, per
# the schematic) from the raw battery rail down to GND, so the ADC reads
# exactly VBAT/2 -- BATTERY_DIVIDER_RATIO (battery_voltage / adc_pin_voltage)
# is therefore exactly 2.0, not a guess. This also lands the ADC's actual
# input voltage in its well-behaved range (~1.5-2.1V) across a LiPo's normal
# 3.0-4.2V swing.
BATTERY_LEVEL_PIN = 7
BATTERY_DIVIDER_RATIO = 2.0

# Two thresholds, both in volts, checked against battery.read_voltage()
# regardless of charge state (a low cell is a real risk even with a
# charger attached, if the power path isn't actually topping it off for
# some reason -- safer to key off the cell voltage directly than to trust
# "not charging" to mean "has stable external power").
#
# BATTERY_LOW_VOLTAGE: shown as a warning on the OLED, nothing blocked yet.
# BATTERY_CRITICAL_VOLTAGE: begin_arm_sequence() refuses to start a new
# recording below this -- motivated by a real hang observed on hardware
# (see native/README.md and WATCHDOG_TIMEOUT_MS below): a low/depleted
# battery under a WiFi TX current burst can sag enough to glitch a
# peripheral without a clean brownout reset, leaving the device silently
# unresponsive. Arming always leads to WiFi activity at some point
# (export, if nothing else), so refusing to arm when the cell is already
# this low directly avoids the highest-risk scenario rather than just
# recovering from it after the fact (which the watchdog now also does).
BATTERY_LOW_VOLTAGE = 3.5
BATTERY_CRITICAL_VOLTAGE = 3.4

# ---------------------------------------------------------------------------
# WiFi + upload target. Fill these in before enabling ENABLE_WIFI_EXPORT.
# The firmware POSTs the CSV, chunked, to
# https://SERVER_HOST:SERVER_PORT{SERVER_UPLOAD_PATH_PREFIX}/{device_key}
# -- i.e. the device-dashboard web app's per-device upload route, now the
# live Vercel deployment rather than a laptop on the same LAN. SERVER_HOST
# is a hostname here (SNI + the HTTP Host header both need it, and Vercel's
# edge network routes by hostname, not by IP) -- update it if the project's
# domain ever changes.
# ---------------------------------------------------------------------------
WIFI_SSID = "your-wifi-ssid"
WIFI_PASSWORD = "your-wifi-password"
WIFI_CONNECT_TIMEOUT_MS = 15000

SERVER_HOST = "mousetrap-car-accelerometer-dashboa.vercel.app"
SERVER_PORT = 443
# TLS via ssl.wrap_socket() with no certificate verification (no CA bundle
# on the device) -- this stops passive eavesdropping on the WiFi link but
# not a deliberate on-path attacker with a forged cert; acceptable for a
# classroom telemetry device with nothing sensitive in the payload. Set to
# False only for pointing back at a plain-HTTP local dev server.
SERVER_USE_TLS = True
SERVER_UPLOAD_PATH_PREFIX = "/api/runs"

# Remote control: while STATE_IDLE, WiFi is off the rest of the time so it
# can't disturb the sampling loop -- the device POSTs its state to
# {SERVER_POLL_PATH_PREFIX}/{device_key}/poll every POLL_INTERVAL_MS and
# executes whatever command (if any) the dashboard's Start button queued.
# Uses a shorter WiFi-connect timeout than a real export, since a failed
# poll just retries on the next interval -- no need to block button-
# handling for as long while idle and offline.
SERVER_POLL_PATH_PREFIX = "/api/devices"
POLL_INTERVAL_MS = 3000
POLL_WIFI_CONNECT_TIMEOUT_MS = 5000

# WiFi also comes back on during RECORDING specifically (off again during
# ARM_DELAY/CALIBRATING) so a remote Stop can reach the device once it's
# actually collecting data -- see finish_calibration_and_start_recording()
# in main.py. This used to need to be conservative (2000ms) because the
# poll's socket I/O ran on a Python thread sharing the GIL with the main
# sampling loop, occasionally glitching an in-flight I2C read. Sampling is
# now a real FreeRTOS task with no GIL involvement at all (see
# native/README.md), so that risk is gone -- back to a more responsive
# 1000ms for remote Stop's worst-case latency.
RECORDING_POLL_INTERVAL_MS = 1000

# ---------------------------------------------------------------------------
# Hardware watchdog. main.py's loop feeds this once per iteration -- if it
# ever stops running (observed once on real hardware: a low/depleted
# battery running on WiFi for a long stretch left the device fully
# unresponsive to any button, with every inspectable Python-level value
# still looking normal -- consistent with a brief power-supply glitch
# during a WiFi TX burst corrupting something below the level a clean
# Python exception could catch, rather than a logic bug), this forces a
# full automatic reboot instead of requiring someone to physically find
# and reset the device.
#
# The timeout only needs to comfortably exceed the longest stretch between
# two wdt.feed() calls -- not the longest operation overall. The two
# genuinely long operations (ensure_wifi_connected()'s connect-retry loop,
# up to WIFI_CONNECT_TIMEOUT_MS; and upload_csv_over_wifi()'s chunked send
# of up to 6000 CSV rows) both feed the watchdog from inside their own
# loops now specifically so they don't need to be covered by this timeout
# at all -- a legitimately slow-but-progressing WiFi connect or export no
# longer risks getting killed by a short watchdog. Every individual
# blocking socket call is itself bounded to 5s (sock.settimeout(5),
# including upload_csv_over_wifi()'s own connect(), which previously had no
# timeout at all and could in principle have blocked far longer than any
# watchdog timeout should have had to cover). What's left uncovered by a
# feed() is just: boot (BMI270/OLED init, normally well under a second),
# and the ~2-3s of voice-clip playback in begin_arm_sequence(). 10s gives
# real margin above both while recovering far faster than the original,
# very conservative 45s if something genuinely hangs.
# ---------------------------------------------------------------------------
WATCHDOG_TIMEOUT_MS = 10000
