"""ESP32-S3 + BMI270 IMU data logger (MicroPython port + native C sampler).

Sensor sampling, the BMI270 driver, and the OLED driver all run as native C
(see native/README.md) -- a real FreeRTOS task on core 1 does 50Hz I2C reads
via a genuine preemptive scheduler, immune to the GIL contention that used
to occasionally corrupt a sample when WiFi polling and sampling shared one
Python thread. Python's job here is state machine, calibration math, CSV
export, audio, and remote control -- it drains whatever the native task has
produced (imu_native.pull()) once per main-loop iteration, rather than
driving the sensor itself.

Button 1 (BTN_START_DISARM_PIN): starts the arm sequence; press again at
  any point to abort/disarm. Disarming pauses sampling but keeps the
  buffer, so you can arm/disarm multiple times within one experiment.
  The arm sequence itself (mousetrap-car oriented, since the board is
  mounted on the underside and needs a moment to be placed):
    1. ARM_DELAY (ARM_PLACEMENT_DELAY_SECONDS, default 10s) -- plays the
       "place the car down" voice cue immediately on button press, then
       LED blinks for the rest of the window. Time to set the car down on
       the starting line.
    2. CALIBRATING (CALIBRATION_SECONDS, default 5s) -- plays the "keep
       still, calibrating" voice cue first, *then* starts the actual 5s
       hold-still window once that clip finishes (not concurrently with
       it -- same reasoning as not overlapping voice cues with sampling
       elsewhere: speaker vibration during calibration would corrupt the
       gravity baseline). LED keeps blinking. Car must be completely
       still. Samples during this window feed only the on-device gravity
       baseline + vibration-noise deadzone diagnostic (printed to Serial
       at the end) -- they are NOT part of the exported CSV (see
       _collect_calibration_samples()); the export starts fresh at real
       recording, not at calibration.
    3. COUNTDOWN -- three low countdown beeps one second apart, then a
       higher-pitched "go" beep. Sampling is deliberately *not* drained
       into the exported buffer during this phase (see
       _flush_native_queue()) -- the beeps' own speaker vibration would
       otherwise show up as the first ~3s of the actual recording, right
       when the car is released and the data matters most. The native
       sampler keeps running underneath regardless (it always does), but
       whatever it produces during this phase is discarded rather than
       exported. Because calibration data is also excluded (see above),
       none of this shows up as a gap in the exported CSV either -- t=0
       there is the first real post-countdown sample, continuous from
       then on.
    4. RECORDING -- LED solid on. Release the car once you hear "go".
       Sampling resumes (fresh, post-countdown) until disarmed or the
       buffer fills. t=0 for the exported CSV is set right here (see
       _start_recording_worker()), not at calibration -- a deliberate
       change from an earlier version, made after real hardware data
       showed the old calibration-then-countdown-gap-then-recording
       layout looked like a confusing, unexplained hole in the middle of
       the exported timeline.
Button 2 (BTN_RESET_PIN): stops recording (if running) and clears the
  buffer, ready for a new experiment.
Button 3 (BTN_EXPORT_PIN): while disarmed and the buffer holds samples,
  uploads the CSV over WiFi (ENABLE_WIFI_EXPORT in config.py). No SD card
  on this board -- the earlier SD-card export path was removed entirely,
  not just disabled, once that was confirmed.

Each sample logs the BMI270's 6 raw axes (accel + gyro; no magnetometer on
this chip) plus a simple complementary-filter roll/pitch estimate --
uncalibrated, see README for its limitations. Yaw and the CSV's mag_x/y/z
columns always read 0 -- kept in the CSV format for compatibility with the
web dashboard's fixed 14-column parser, not because this sensor has one.

WiFi credentials can be set two ways: hardcoded as WIFI_SSID/WIFI_PASSWORD
in config.py, or pushed at runtime over USB serial by the companion app in
wifi_provisioner_app/ (persisted to /wifi.json on the device, which takes
priority over the config.py values -- see serial_provisioning.py).

See README.md for wiring, flashing MicroPython itself, and uploading these
files to the board.
"""

import array
import math

import _thread
import machine
import network
import ujson
import utime

from config import (
    ENABLE_WIFI_EXPORT,
    BTN_START_DISARM_PIN, BTN_RESET_PIN, BTN_EXPORT_PIN, DEBOUNCE_MS,
    STATUS_LED_PIN, LED_BLINK_INTERVAL_MS,
    TONE_DEFAULT_HZ, TONE_LOW_HZ, TONE_GO_HZ,
    ARM_PLACEMENT_DELAY_SECONDS, CALIBRATION_SECONDS, CALIBRATION_DEADZONE_SIGMA,
    I2C_SDA_PIN, I2C_SCL_PIN, I2C_FREQ_HZ, OLED_I2C_ADDR,
    COMPLEMENTARY_FILTER_ALPHA,
    SAMPLE_RATE_HZ, MAX_RECORD_SECONDS, MAX_SAMPLES,
    WIFI_SSID, WIFI_PASSWORD, WIFI_CONNECT_TIMEOUT_MS,
    SERVER_HOST, SERVER_PORT, SERVER_USE_TLS, SERVER_UPLOAD_PATH_PREFIX,
    SERVER_POLL_PATH_PREFIX, POLL_INTERVAL_MS, POLL_WIFI_CONNECT_TIMEOUT_MS,
    RECORDING_POLL_INTERVAL_MS,
    WATCHDOG_TIMEOUT_MS,
)
from device_key import compute_device_key
import audio
import battery
import imu_native
import serial_provisioning
import wifi_store

# Hardware watchdog, created before *anything* else that touches a
# peripheral (BMI270/OLED init below, both of which do real I2C transactions
# and could in principle hang the whole boot if the bus were ever stuck)
# rather than after, like an earlier revision of this file had it. A hang
# here previously meant main() never even started -- the device would be
# stuck from the very first line of boot, with nothing feeding a watchdog
# that didn't exist yet, surviving indefinitely across power cycles if
# whatever caused it recurred on every boot. See config.py's
# WATCHDOG_TIMEOUT_MS comment for the full story (a real observed hang,
# though that one was in the main loop, not boot -- moving this earlier
# closes the boot-time gap in the same protection).
wdt = machine.WDT(timeout=WATCHDOG_TIMEOUT_MS)

# ---------------------------------------------------------------------------
# Sensor + OLED -- both native C now, sharing one I2C bus handle (see
# config.py's I2C section and native/README.md for why). Nothing in Python
# touches the bus directly; imu_native.init() must run before oled_init()
# since the OLED adds itself as a device on the bus init() creates.
# ---------------------------------------------------------------------------
sensor_ok = imu_native.init(I2C_SDA_PIN, I2C_SCL_PIN, I2C_FREQ_HZ)
print("[imu] BMI270 initialized" if sensor_ok else "[imu] BMI270 NOT FOUND (check wiring/address)")

device_key = compute_device_key()
print("[device] key=%s" % device_key)

oled_ok = imu_native.oled_init(OLED_I2C_ADDR)
print("[oled] SSD1306 initialized" if oled_ok else "[oled] SSD1306 NOT FOUND (key only visible on Serial)")


def _battery_line():
    # Own try/except so a battery-reading hiccup (e.g. ADC glitch) can never
    # take down the OLED status line with it -- same fail-soft spirit as
    # sensor_ok/oled_ok above. See battery.py for the STAT1/STAT2 decode and
    # the ADC divider math.
    #
    # Percent is only shown once STAT1/STAT2 say charging has stopped --
    # while actively CHARGING, the terminal voltage includes a real IR-drop
    # term from the charge current (see battery.py's module docstring), so
    # the reading can jump straight to a misleadingly high percentage right
    # when a charger is plugged in. Trust it only when not actively charging.
    try:
        state = battery.read_charge_state()
    except Exception:
        return ""
    if state == battery.CHARGING:
        return "BAT CHARGING"
    if state == battery.CHARGE_DONE:
        try:
            pct = battery.read_percent()
            suffix = " LOW!" if battery.is_low() else ""
            return "BAT %d%%%s" % (pct, suffix)
        except Exception:
            return ""
    return "BAT FAULT"


def update_oled(state_line):
    # No-ops internally on the C side if oled_init() didn't succeed --
    # same fail-soft behavior the old ssd1306.py-based driver had.
    #
    # WiFi is deliberately off entirely during ARM_DELAY/CALIBRATING/
    # COUNTDOWN (see config.py) so it can't disturb the timing-sensitive
    # phases -- isconnected() correctly reports False then too, which is
    # accurate (there really is no connection during that window), not a
    # bug in the icon.
    try:
        wifi_connected = network.WLAN(network.STA_IF).isconnected()
    except Exception:
        wifi_connected = False
    imu_native.oled_update(state_line, device_key, _battery_line(), wifi_connected)


update_oled("idle")

# ---------------------------------------------------------------------------
# Buttons + LED
# ---------------------------------------------------------------------------
class Button:
    def __init__(self, pin_num):
        self.pin = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
        self.last_reading = 1
        self.stable_state = 1
        self.last_change_ms = utime.ticks_ms()

    def pressed_edge(self):
        """Returns True exactly once, on the debounced falling edge (button pressed)."""
        reading = self.pin.value()
        now = utime.ticks_ms()
        if reading != self.last_reading:
            self.last_change_ms = now
            self.last_reading = reading
        pressed = False
        if utime.ticks_diff(now, self.last_change_ms) > DEBOUNCE_MS and self.stable_state != self.last_reading:
            self.stable_state = self.last_reading
            pressed = (self.stable_state == 0)
        return pressed


btn_start_disarm = Button(BTN_START_DISARM_PIN)
btn_reset = Button(BTN_RESET_PIN)
btn_export = Button(BTN_EXPORT_PIN)

led = machine.Pin(STATUS_LED_PIN, machine.Pin.OUT)
led.value(0)

# wdt (the hardware watchdog) is created right at the top of this file, before
# BMI270/OLED init -- see that comment for why. Fed once per main-loop
# iteration further down; feeding is a plain function call from wherever, not
# tied to a specific FreeRTOS task (extmod/machine_wdt.c), so per-iteration
# granularity is exactly right regardless of which thread last touched it.


def blink_led(times, on_ms, off_ms):
    for _ in range(times):
        led.value(1)
        utime.sleep_ms(on_ms)
        led.value(0)
        utime.sleep_ms(off_ms)


def beep(times, on_ms, off_ms, freq_hz=TONE_DEFAULT_HZ):
    for _ in range(times):
        audio.tone(freq_hz, on_ms)
        utime.sleep_ms(off_ms)


def countdown_and_go():
    """Three low beeps one second apart, then a higher-pitched "go" beep --
    played right before release, once calibration stats are already
    computed (see finish_calibration_and_start_recording)."""
    for _ in range(3):
        audio.tone(TONE_LOW_HZ, 150)
        utime.sleep_ms(850)
    audio.tone(TONE_GO_HZ, 300)


# Non-blocking LED blink state, updated once per main-loop iteration while
# armed/calibrating (blink_led() above is a separate *blocking* helper used
# for short one-off feedback patterns like export success/failure).
_led_blink_next_ms = 0
_led_blink_on = False


def start_led_blink(now):
    global _led_blink_next_ms, _led_blink_on
    _led_blink_on = True
    led.value(1)
    _led_blink_next_ms = utime.ticks_add(now, LED_BLINK_INTERVAL_MS)


def update_led_blink(now):
    global _led_blink_next_ms, _led_blink_on
    if utime.ticks_diff(now, _led_blink_next_ms) >= 0:
        _led_blink_on = not _led_blink_on
        led.value(1 if _led_blink_on else 0)
        _led_blink_next_ms = utime.ticks_add(now, LED_BLINK_INTERVAL_MS)


# ---------------------------------------------------------------------------
# Sample buffers (parallel array.array, not lists of floats, to avoid
# per-sample object allocation overhead)
# ---------------------------------------------------------------------------
def _alloc_f():
    return array.array("f", bytes(4 * MAX_SAMPLES))


t_ms_buf = array.array("I", bytes(4 * MAX_SAMPLES))
gx_buf, gy_buf, gz_buf = _alloc_f(), _alloc_f(), _alloc_f()
ax_buf, ay_buf, az_buf = _alloc_f(), _alloc_f(), _alloc_f()
mx_buf, my_buf, mz_buf = _alloc_f(), _alloc_f(), _alloc_f()
roll_buf, pitch_buf, yaw_buf = _alloc_f(), _alloc_f(), _alloc_f()

sample_count = 0

# Small scratch buffers used to pull samples out of the native queue
# without touching the real t_ms_buf/etc. above (or sample_count) --
# shared by _flush_native_queue() (discards everything, during
# STATE_COUNTDOWN) and _collect_calibration_samples() (keeps the ax/ay/az
# values, during STATE_CALIBRATING), never both at once. 300 comfortably
# covers the countdown's ~165-sample worst case in a single pull() call
# (with margin), well under the native queue's own 1000-sample/20s cap
# (QUEUE_LEN in sampler_task.c) either way.
_SCRATCH_LEN = 300
_scratch_t_buf = array.array("I", bytes(4 * _SCRATCH_LEN))
_scratch_gx_buf, _scratch_gy_buf, _scratch_gz_buf = array.array("f", bytes(4 * _SCRATCH_LEN)), array.array("f", bytes(4 * _SCRATCH_LEN)), array.array("f", bytes(4 * _SCRATCH_LEN))
_scratch_ax_buf, _scratch_ay_buf, _scratch_az_buf = array.array("f", bytes(4 * _SCRATCH_LEN)), array.array("f", bytes(4 * _SCRATCH_LEN)), array.array("f", bytes(4 * _SCRATCH_LEN))


def _flush_native_queue():
    """Drains and discards whatever the native sampler produced during
    STATE_COUNTDOWN. The sensor keeps sampling continuously regardless of
    Python state (it always has -- see native/README.md), so without this,
    the beeps' own speaker vibration would silently become the first ~3s
    of the actual recording the moment drain_samples() resumes. Loops
    because a single pull() call only returns up to len(_scratch_t_buf)
    samples -- one call is enough in practice (300 > the countdown's
    ~165-sample worst case), but looping to a real 0 makes that a safety
    margin rather than a hard assumption."""
    while True:
        n = imu_native.pull(
            _scratch_t_buf, _scratch_gx_buf, _scratch_gy_buf, _scratch_gz_buf,
            _scratch_ax_buf, _scratch_ay_buf, _scratch_az_buf,
            0, _SCRATCH_LEN,
        )
        if n == 0:
            break


def _collect_calibration_samples():
    """Pulls whatever the native sampler has produced since the last call
    and appends each (ax, ay, az) reading to calibration_samples --
    deliberately NOT through drain_samples()/sample_count, so
    calibration-phase data (the car sitting still, before the countdown
    even starts) never becomes part of the exported CSV. It used to:
    t0_native_ms was set at the start of calibration, and the countdown's
    own discarded window (_flush_native_queue()) landed in the middle of
    the exported timeline, showing up as a confusing ~3.5s gap between two
    kinds of "real" data (calibration, then the actual recording) instead
    of a clean recording that starts at t=0. Real hardware data across
    several test runs confirmed this gap was exactly the by-design
    countdown discard, misplaced -- not sample loss. calibration_samples
    is still used for the on-device gravity/deadzone diagnostic print in
    finish_calibration_and_start_recording(); it just no longer shares
    storage with the exported buffers."""
    n = imu_native.pull(
        _scratch_t_buf, _scratch_gx_buf, _scratch_gy_buf, _scratch_gz_buf,
        _scratch_ax_buf, _scratch_ay_buf, _scratch_az_buf,
        0, _SCRATCH_LEN,
    )
    for i in range(n):
        calibration_samples.append((_scratch_ax_buf[i], _scratch_ay_buf[i], _scratch_az_buf[i]))

STATE_IDLE = "IDLE"
STATE_ARM_DELAY = "ARM_DELAY"
STATE_CALIBRATING = "CALIBRATING"
STATE_COUNTDOWN = "COUNTDOWN"  # beeps playing, sampling deliberately not drained -- see module docstring
STATE_RECORDING = "RECORDING"
state = STATE_IDLE

record_start_ms = 0  # Python wall-clock ms, used for state-machine/OLED timing only

# t_ms in each native sample is relative to when imu_native.init() was
# called (once, at boot) -- t0_native_ms is that clock's value at the
# moment *this* experiment's calibration began, captured via
# imu_native.now_ms() in start_calibration(). Every sample drain_samples()
# accepts gets its t_ms rewritten as (raw - t0_native_ms), which is what
# actually lands in t_ms_buf/the exported CSV -- same "t=0 is calibration
# start" contract analyze_run.py already expects. Samples with a raw
# timestamp before t0_native_ms (backlog the native task produced before
# this experiment started, since it samples continuously regardless of
# Python's state) are discarded, not just left with a negative timestamp.
t0_native_ms = 0
# None means "next accepted sample seeds orientation via init_orientation()
# instead of integrating via update_orientation()" -- the replacement for
# the old single synchronous accel.read_g() call at the start of
# start_calibration(), which doesn't exist anymore now that all reads go
# through the native queue.
_last_relative_t_ms = None

arm_delay_deadline_ms = 0
calibration_deadline_ms = 0
calibration_samples = []  # (ax, ay, az) tuples collected during CALIBRATING
calib_gravity = (0.0, 0.0, 0.0)   # last computed calibration, informational
calib_deadzone_g = 0.0

next_poll_due_ms = 0  # remote-command polling, STATE_IDLE only

# Remote-stop polling during STATE_RECORDING runs on a separate MicroPython
# thread (via _thread) instead of the main loop, so a blocking HTTP call
# can't stall button handling/state-machine responsiveness -- it just
# checks remote_stop_requested, a plain flag, once per iteration. (Not a
# second CPU core, despite what an earlier version of this comment claimed:
# MicroPython's ESP32 port pins the interpreter task and every _thread it
# creates to core 1 specifically, so WiFi/BT -- which own core 0 entirely --
# are never disturbed. Verified by reading mphalport.h/main.c directly.
# Sampling itself doesn't depend on this thread at all anymore anyway --
# see native/README.md -- it's a real FreeRTOS task with no GIL involvement.)
# polling_thread_active is how the main thread tells the background thread
# to wind down when recording ends (by any means: remote stop, buffer-full,
# or the physical button).
polling_thread_active = False
remote_stop_requested = False

# Incremented each time a new recording actually starts. _start_recording_worker
# captures its own value at spawn time and checks it (alongside `state`) after
# each blocking phase, so a worker "orphaned" by an early disarm (or a second
# recording starting before the first worker finished) notices and bails out
# instead of continuing to run -- see _start_recording_worker's docstring for
# the bug this fixes.
_recording_session_id = 0


def _recording_poll_worker():
    global remote_stop_requested
    while polling_thread_active:
        utime.sleep_ms(RECORDING_POLL_INTERVAL_MS)
        if not polling_thread_active:
            break
        # Also keeps the OLED's WiFi icon live for the whole recording --
        # _start_recording_worker's own update_oled("recording") call runs
        # *before* ensure_wifi_connected() even attempts to reconnect, so
        # that one-shot snapshot always shows disconnected regardless of
        # whether the reconnect succeeds a moment later, and nothing else
        # ever refreshed it again for the rest of the recording. This tick
        # (already running every RECORDING_POLL_INTERVAL_MS regardless)
        # also catches a connection that drops partway through, not just
        # the initial reconnect's outcome.
        update_oled("recording")
        if poll_remote_command() == "stop":
            remote_stop_requested = True
            break


def _start_recording_worker(session_id):
    """Runs on a separate MicroPython thread (same core as the main
    interpreter -- see the note above _recording_poll_worker), in parallel
    with sampling, which is a real FreeRTOS task on the native side and
    genuinely doesn't care what Python is doing on any thread (see
    native/README.md). Used to run this stuff -- countdown beeps, then a
    WiFi reconnect that can take a couple of seconds -- without blocking
    the main loop's drain_samples() calls, so none of it can cost a single
    sample right when the car gets released, the most important moment in
    the whole recording.

    Found via a real captured serial log: a recording could disarm itself
    almost instantly, with the log jumping straight from calibration to
    DISARMED and no "RECORDING -- release it now" line at all -- caused by
    remote_stop_requested only getting reset to False down here, well after
    countdown_and_go() and ensure_wifi_connected() (several real seconds),
    while the main loop starts checking it (`state == STATE_RECORDING and
    remote_stop_requested`) the instant state flips to STATE_RECORDING, well
    before this worker even runs. If a *previous* recording had ended via a
    genuine remote stop, the flag was left True indefinitely (nothing else
    ever reset it) -- so the very next recording attempt would self-disarm
    on its first main-loop iteration, reliably, not just as a rare race.
    remote_stop_requested is now reset before state even flips to
    STATE_RECORDING (see finish_calibration_and_start_recording), which
    closes that window entirely.

    Second bug this fixes: disarm_recording() never cancelled this thread if
    it was already running -- an early disarm just left it as an orphan that
    kept going regardless, still playing its countdown beeps (audibly
    colliding with whatever beeps the main thread's disarm/export path was
    already playing -- consistent with confusing/overlapping beep patterns
    reported on real hardware) and, worse, still setting
    polling_thread_active = True and starting a fresh poll loop even though
    the state machine had already moved back to STATE_IDLE. The session_id
    checks below make it notice and stop instead.

    state starts this function as STATE_COUNTDOWN, not STATE_RECORDING --
    the main loop deliberately does not drain the sensor queue during
    STATE_COUNTDOWN (see that state's definition), so the beeps' own
    speaker vibration can't become part of the exported recording. The
    native sampler keeps producing samples the whole time regardless (it
    always does); _flush_native_queue() discards whatever piled up during
    the beeps right before flipping to STATE_RECORDING, so draining starts
    clean from the "go" moment rather than replaying ~3s of beep-tainted
    data as if it were the start of the run."""
    global state, polling_thread_active, t0_native_ms, _last_relative_t_ms
    countdown_and_go()
    if session_id != _recording_session_id or state != STATE_COUNTDOWN:
        return  # disarmed (or superseded) while the countdown was playing
    _flush_native_queue()
    t0_native_ms = imu_native.now_ms()  # t=0 for exported timestamps -- see start_calibration()'s docstring
    _last_relative_t_ms = None  # re-seed orientation from the first real recording sample, not calibration's last one
    state = STATE_RECORDING
    update_oled("recording")
    print("[state] RECORDING -- release it now")
    ensure_wifi_connected(POLL_WIFI_CONNECT_TIMEOUT_MS)  # best-effort; recording proceeds either way
    if session_id != _recording_session_id or state != STATE_RECORDING:
        return  # disarmed (or superseded) while WiFi was reconnecting
    polling_thread_active = True
    _recording_poll_worker()

# ---------------------------------------------------------------------------
# Orientation estimate (complementary filter)
# ---------------------------------------------------------------------------
roll_deg = 0.0
pitch_deg = 0.0
yaw_deg = 0.0


def compute_heading_deg(mx, my, mz, roll_d, pitch_d):
    roll_rad = math.radians(roll_d)
    pitch_rad = math.radians(pitch_d)
    xh = mx * math.cos(pitch_rad) + mz * math.sin(pitch_rad)
    yh = (mx * math.sin(roll_rad) * math.sin(pitch_rad)
          + my * math.cos(roll_rad)
          - mz * math.sin(roll_rad) * math.cos(pitch_rad))
    heading = math.degrees(math.atan2(yh, xh))
    if heading < 0.0:
        heading += 360.0
    return heading


def init_orientation(ax, ay, az, mx, my, mz, have_mag):
    global roll_deg, pitch_deg, yaw_deg
    roll_deg = math.degrees(math.atan2(ay, az))
    pitch_deg = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
    yaw_deg = compute_heading_deg(mx, my, mz, roll_deg, pitch_deg) if have_mag else 0.0


def update_orientation(ax, ay, az, gx, gy, mx, my, mz, have_mag, dt):
    global roll_deg, pitch_deg, yaw_deg
    accel_roll = math.degrees(math.atan2(ay, az))
    accel_pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
    a = COMPLEMENTARY_FILTER_ALPHA
    roll_deg = a * (roll_deg + gx * dt) + (1.0 - a) * accel_roll
    pitch_deg = a * (pitch_deg + gy * dt) + (1.0 - a) * accel_pitch
    if have_mag:
        yaw_deg = compute_heading_deg(mx, my, mz, roll_deg, pitch_deg)


# ---------------------------------------------------------------------------
# Calibration: gravity baseline + vibration-noise deadzone, derived from the
# accel samples collected during CALIBRATING (car must be completely still).
# Same math analysis/analyze_run.py uses on the exported CSV -- computed
# here too just so it's visible live on Serial without needing the host
# script, and so the operator gets audible confirmation the moment it's
# safe to release the car.
# ---------------------------------------------------------------------------
def compute_calibration_stats(samples, sigma):
    n = len(samples)
    if n == 0:
        return (0.0, 0.0, 0.0), 0.0
    bx = sum(s[0] for s in samples) / n
    by = sum(s[1] for s in samples) / n
    bz = sum(s[2] for s in samples) / n
    residuals = []
    for ax, ay, az in samples:
        cx, cy, cz = ax - bx, ay - by, az - bz
        residuals.append(math.sqrt(cx * cx + cy * cy + cz * cz))
    mean_r = sum(residuals) / n
    var_r = sum((r - mean_r) ** 2 for r in residuals) / n
    deadzone = mean_r + sigma * math.sqrt(var_r)
    return (bx, by, bz), deadzone


# ---------------------------------------------------------------------------
# CSV row formatting (shared by SD + WiFi export)
# ---------------------------------------------------------------------------
CSV_HEADER = (
    "sample_index,timestamp_ms,"
    "gyro_x_dps,gyro_y_dps,gyro_z_dps,"
    "accel_x_g,accel_y_g,accel_z_g,"
    "mag_x_uT,mag_y_uT,mag_z_uT,"
    "roll_deg,pitch_deg,yaw_deg\n"
)


def format_csv_row(i):
    return "%d,%d,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.2f,%.2f,%.2f\n" % (
        i, t_ms_buf[i],
        gx_buf[i], gy_buf[i], gz_buf[i],
        ax_buf[i], ay_buf[i], az_buf[i],
        mx_buf[i], my_buf[i], mz_buf[i],
        roll_buf[i], pitch_buf[i], yaw_buf[i],
    )


# ---------------------------------------------------------------------------
# WiFi connection (shared by export and remote-command polling)
# ---------------------------------------------------------------------------
def ensure_wifi_connected(timeout_ms=WIFI_CONNECT_TIMEOUT_MS):
    ssid, password = wifi_store.get_credentials(WIFI_SSID, WIFI_PASSWORD)
    if not ssid:
        print("[WiFi] no SSID configured (set one via the companion app, or WIFI_SSID in config.py)")
        return False

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return True

    print("[WiFi] connecting", end="")
    wlan.connect(ssid, password)
    start = utime.ticks_ms()
    while not wlan.isconnected():
        if utime.ticks_diff(utime.ticks_ms(), start) > timeout_ms:
            print(" timed out")
            wlan.active(False)
            return False
        wdt.feed()  # this loop can legitimately run for seconds -- keep the (short) watchdog satisfied while it's actively retrying, not just stuck
        utime.sleep_ms(250)
        print(".", end="")
    print(" connected")
    return True


# ---------------------------------------------------------------------------
# Export: WiFi chunked HTTP POST (streams rows, never buffers the whole CSV)
# ---------------------------------------------------------------------------
def _connect_server_socket(addr_info, timeout_s=5):
    """Opens a plain or TLS-wrapped socket to SERVER_HOST/PORT depending on
    SERVER_USE_TLS. server_hostname is required for TLS here, not optional
    -- Vercel's edge terminates TLS for many projects behind shared IPs and
    routes by SNI, so a wrap without it can hand back the wrong cert or
    just fail the handshake. No certificate verification is configured
    (see config.py's SERVER_USE_TLS comment for why that's an accepted
    tradeoff here)."""
    import socket

    sock = socket.socket()
    sock.settimeout(timeout_s)  # was unset -- an unreachable-but-not-immediately-refused
                                 # host could otherwise block here far longer than
                                 # any watchdog timeout should have to cover
    sock.connect(addr_info)
    if SERVER_USE_TLS:
        import ssl
        sock = ssl.wrap_socket(sock, server_hostname=SERVER_HOST)
    return sock


def upload_csv_over_wifi():
    import socket

    if not ensure_wifi_connected():
        return False

    try:
        addr_info = socket.getaddrinfo(SERVER_HOST, SERVER_PORT)[0][-1]
        sock = _connect_server_socket(addr_info)
    except OSError as e:
        print("[WiFi] connection to server failed:", e)
        return False

    def send_chunk(data):
        if isinstance(data, str):
            data = data.encode()
        sock.write(("%x\r\n" % len(data)).encode())
        sock.write(data)
        sock.write(b"\r\n")

    upload_path = "%s/%s" % (SERVER_UPLOAD_PATH_PREFIX, device_key)
    # Sends the gravity baseline + deadzone this experiment's own real,
    # guaranteed-stationary STATE_CALIBRATING window computed (see
    # finish_calibration_and_start_recording()) as headers, not as extra
    # CSV rows/columns -- keeps the CSV body's format untouched so the
    # dashboard's existing parser doesn't need to change, and lets the
    # server treat this as pure metadata about the run rather than data
    # to plot. Without this, the dashboard has to re-derive a baseline
    # from the first few seconds of the recording itself, which silently
    # produces a useless deadzone for any car that launches almost
    # immediately (confirmed on real classroom data: a 0.68g deadzone
    # from a "calibration window" that wasn't actually stationary).
    bx, by, bz = calib_gravity
    request_headers = (
        "POST %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Content-Type: text/csv\r\n"
        "X-Calib-Gravity: %.6f,%.6f,%.6f\r\n"
        "X-Calib-Deadzone: %.6f\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n\r\n"
    ) % (upload_path, SERVER_HOST, bx, by, bz, calib_deadzone_g)

    try:
        sock.write(request_headers.encode())
        send_chunk(CSV_HEADER)
        for i in range(sample_count):
            send_chunk(format_csv_row(i))
            if i % 100 == 0:
                wdt.feed()  # a full 6000-row upload can legitimately take a
                            # few seconds -- keep the (now much shorter)
                            # watchdog satisfied while actively sending, not
                            # just stuck
        sock.write(b"0\r\n\r\n")  # terminate chunked body
    except OSError as e:
        # New failure mode since sock.settimeout(5) was added above -- a
        # stalled send now raises here instead of blocking forever, same
        # "export failed, buffer kept" outcome as any other connection
        # failure, just via a different code path than the connect()
        # failure a few lines up.
        print("[WiFi] upload send failed:", e)
        sock.close()
        return False

    status_line = ""
    try:
        # No settimeout() call here -- it's already set on the underlying
        # socket by _connect_server_socket() before the TLS wrap, and an
        # SSLSocket doesn't implement settimeout() at all (AttributeError,
        # not OSError, so it wasn't even caught below -- crashed the whole
        # program out to the REPL instead of just failing this export).
        raw = sock.readline()
        if raw:
            status_line = raw.decode()
    except OSError:
        pass
    sock.close()

    ok = "200" in status_line
    print("[WiFi] upload %s (%s)" % ("succeeded" if ok else "failed", status_line.strip()))
    return ok


# ---------------------------------------------------------------------------
# Remote control: poll the dashboard for a queued command. Called from the
# main loop while STATE_IDLE, and from the _recording_poll_worker thread
# throughout STATE_RECORDING -- WiFi is only fully off during
# ARM_DELAY/CALIBRATING, where placement/calibration timing matters most.
# ---------------------------------------------------------------------------
_server_addr_info = None  # cached across calls -- SERVER_HOST/PORT never change at runtime


def http_post_json(path, payload):
    """Tiny one-shot HTTP POST with a Content-Length body (not chunked --
    these payloads are a few bytes). Returns the parsed JSON response body,
    or None on any connection/timeout/parse failure."""
    import socket

    global _server_addr_info
    try:
        if _server_addr_info is None:
            _server_addr_info = socket.getaddrinfo(SERVER_HOST, SERVER_PORT)[0][-1]
        sock = _connect_server_socket(_server_addr_info)
    except OSError as e:
        print("[http] connection failed:", e)
        return None

    body = ujson.dumps(payload)
    request = (
        "POST %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "Connection: close\r\n\r\n"
        "%s"
    ) % (path, SERVER_HOST, len(body), body)

    response = b""
    try:
        sock.write(request.encode())
        while True:
            # .read(), not .recv() -- an SSLSocket only implements the
            # stream protocol (read/readinto/write), no raw recv/send.
            # Works the same on a plain (non-TLS) socket too.
            chunk = sock.read(512)
            if not chunk:
                break
            response += chunk
    except OSError as e:
        print("[http] request failed:", e)
        sock.close()
        return None
    sock.close()

    parts = response.split(b"\r\n\r\n", 1)
    if len(parts) != 2:
        return None
    headers, body = parts
    if b"chunked" in headers.lower():
        body = _dechunk(body)
    try:
        return ujson.loads(body)
    except ValueError:
        return None


def _dechunk(data):
    """Next.js sends even small JSON responses with Transfer-Encoding:
    chunked -- de-frame <hex-size>\\r\\n<data>\\r\\n...0\\r\\n\\r\\n into the
    concatenated raw body."""
    result = b""
    pos = 0
    while pos < len(data):
        line_end = data.find(b"\r\n", pos)
        if line_end == -1:
            break
        try:
            size = int(data[pos:line_end], 16)
        except ValueError:
            break
        if size == 0:
            break
        chunk_start = line_end + 2
        result += data[chunk_start:chunk_start + size]
        pos = chunk_start + size + 2
    return result


def poll_remote_command():
    if not ensure_wifi_connected(POLL_WIFI_CONNECT_TIMEOUT_MS):
        return None
    path = "%s/%s/poll" % (SERVER_POLL_PATH_PREFIX, device_key)
    result = http_post_json(path, {"state": state.lower()})
    if result is None:
        return None
    return result.get("command")


# ---------------------------------------------------------------------------
# Export dispatch
# ---------------------------------------------------------------------------
def export_data():
    if sample_count == 0:
        print("[export] buffer is empty, nothing to export")
        return False

    any_success = False
    if ENABLE_WIFI_EXPORT:
        any_success = upload_csv_over_wifi() or any_success

    if any_success:
        blink_led(3, 150, 150)
        beep(3, 150, 150)
        update_oled("exported!")
    else:
        blink_led(6, 80, 80)
        beep(6, 80, 80)
        update_oled("export failed")
    return any_success


# ---------------------------------------------------------------------------
# Recording control
# ---------------------------------------------------------------------------
def begin_arm_sequence():
    """Start of the arm sequence: ARM_DELAY -> CALIBRATING -> RECORDING.
    See the module docstring for the full sequence/timing."""
    global state, arm_delay_deadline_ms

    if not sensor_ok:
        print("[arm] BMI270 not initialized, refusing to start")
        blink_led(6, 80, 80)
        beep(6, 80, 80)
        return

    # Refuse to silently paper over a failed export. sample_count only
    # resets after a *successful* export (see disarm_recording()) -- if a
    # prior export failed, the buffer is deliberately kept rather than
    # discarded, but arming again without exporting/resetting first would
    # start this run with the buffer already full (MAX_SAMPLES - sample_count
    # == 0), so drain_samples() couldn't add a single real sample. That run
    # would still go through the motions (countdown, calibration, an
    # immediate "buffer full" auto-disarm) and re-export the *old* run's
    # data as if it were new -- confirmed on real hardware: a second arm
    # right after a failed export looked like a normal cycle but collected
    # zero new samples.
    if sample_count > 0:
        print("[arm] buffer still holds %d un-exported samples -- export or reset first" % sample_count)
        blink_led(6, 80, 80)
        beep(6, 80, 80)
        return

    # Refuse to start a recording on a battery too weak to safely absorb a
    # WiFi TX current burst -- arming always leads to WiFi activity
    # eventually (export, if nothing else), and this is the exact scenario
    # behind a real observed hang (see config.py's BATTERY_CRITICAL_VOLTAGE
    # comment and native/README.md): a low cell sagging under load glitched
    # something without a clean brownout reset, leaving the device silently
    # unresponsive until the new watchdog forced a reboot. Own try/except so
    # a battery-read hiccup can't itself block arming.
    try:
        battery_critical = battery.is_critical()
    except Exception:
        battery_critical = False
    if battery_critical:
        print("[arm] battery critically low (%.2fV) -- recharge before recording" % battery.read_voltage())
        update_oled("battery low!")
        blink_led(6, 80, 80)
        beep(6, 80, 80)
        return

    # Stop remote-command polling before the timing-sensitive phase begins.
    # (Briefly removed this call while chasing a ~3.5s gap in every
    # exported recording, suspecting a cold WiFi/TLS reconnect was
    # starving the sampler. It wasn't -- the gap persisted identically
    # with this line removed, proving WiFi timing was never the cause; see
    # start_calibration()/finish_calibration_and_start_recording() for the
    # real fix. Restored, since there's no reason left to take on an
    # unproven RF-interference-during-calibration risk.)
    network.WLAN(network.STA_IF).active(False)

    state = STATE_ARM_DELAY
    led.value(1)
    update_oled("place it")
    print("[state] ARMED -- playing instructions")
    # "place" plays immediately on button press -- "calibrate" now plays
    # separately, right before the calibration window itself starts (see
    # start_calibration()), not bundled in here. Still played up front
    # relative to ARM_PLACEMENT_DELAY_SECONDS's own countdown, well before
    # any sampling window it could vibrate-contaminate.
    audio.play_voice("place")

    now = utime.ticks_ms()
    arm_delay_deadline_ms = utime.ticks_add(now, int(ARM_PLACEMENT_DELAY_SECONDS * 1000))
    start_led_blink(now)
    print("[state] place it now (%.1fs)" % ARM_PLACEMENT_DELAY_SECONDS)


def start_calibration():
    """ARM_DELAY -> CALIBRATING. Plays the "calibrate" voice cue first,
    *then* starts the actual hold-still window -- not concurrently with
    the clip, since speaker vibration during calibration would corrupt
    the gravity baseline (same reasoning as never overlapping a voice cue
    with a sampling window that matters). Calibration-phase samples feed
    only the on-device gravity/deadzone diagnostic (calibration_samples,
    via _collect_calibration_samples()) -- t0_native_ms (t=0 for exported
    timestamps) isn't set until real recording actually starts, in
    _start_recording_worker(), so the exported CSV contains only real
    (post-countdown) recording data, gap-free from t=0.

    _flush_native_queue() right before this fresh start matters here too,
    not just at the COUNTDOWN->RECORDING handoff: the native sampler never
    stops producing samples, including through the entire idle period
    before arming, the "place" voice, the whole ARM_PLACEMENT_DELAY_SECONDS
    window, and the "calibrate" voice just played above -- none of that is
    drained anywhere, so without a flush here, calibration would start by
    consuming whatever stale backlog happens to be sitting in the queue
    (up to its own cap) rather than genuinely fresh samples. That backlog
    could include real vibration from any of those sources, including a
    previous experiment's disarm beep if the queue was still holding it
    from before this idle period even began."""
    global state, record_start_ms, calibration_deadline_ms, calibration_samples

    state = STATE_CALIBRATING
    update_oled("hold still")
    audio.play_voice("calibrate")
    _flush_native_queue()

    now = utime.ticks_ms()
    record_start_ms = now
    calibration_deadline_ms = utime.ticks_add(now, int(CALIBRATION_SECONDS * 1000))
    calibration_samples = []
    print("[state] CALIBRATING, hold still (%.1fs)" % CALIBRATION_SECONDS)


def finish_calibration_and_start_recording():
    """CALIBRATING -> COUNTDOWN. Computes + prints the calibration summary,
    then hands off to the countdown beeps -- real STATE_RECORDING (and the
    sample draining that comes with it) doesn't start until those finish;
    see _start_recording_worker and STATE_COUNTDOWN's definition for why.
    Sampling itself needs nothing done here at all -- it's a native FreeRTOS
    task that's been running continuously and uninterrupted since boot,
    completely independent of this state transition or anything else Python
    does. The countdown/WiFi/polling handoff runs on a separate thread
    (_start_recording_worker) so it doesn't stall button handling, same
    reasoning as _recording_poll_worker.

    Correction to a claim this docstring used to make: moving that handoff
    to its own thread does NOT, by itself, prevent a gap in the exported
    CSV around the calibration/recording boundary -- real hardware data
    (several separate test runs, even after quadrupling the native
    sampler's queue depth) kept showing an identical ~3.5s hole right
    there regardless. The actual cause was unrelated to threading or
    blocking: calibration-phase samples were being kept as part of the
    exported buffer (t0_native_ms was set at calibration's start), while
    STATE_COUNTDOWN's samples are deliberately discarded (see
    _flush_native_queue()) -- so the export always contained real
    calibration data, then a real discarded window, back to back, and the
    discarded window is what looked like unexplained missing data. Fixed
    by excluding calibration-phase samples from the export entirely (see
    start_calibration()/_collect_calibration_samples()) and setting
    t0_native_ms here instead, once real recording starts -- the export
    now begins at the actual "go" moment with nothing before it to create
    a visible gap."""
    global state, calib_gravity, calib_deadzone_g, remote_stop_requested, _recording_session_id

    calib_gravity, calib_deadzone_g = compute_calibration_stats(calibration_samples, CALIBRATION_DEADZONE_SIGMA)
    bx, by, bz = calib_gravity
    print("[calibration] gravity=(%.4f, %.4f, %.4f)g |gravity|=%.4fg deadzone=%.4fg (%d samples)" % (
        bx, by, bz, math.sqrt(bx * bx + by * by + bz * bz), calib_deadzone_g, len(calibration_samples)))

    # Reset before state flips away from CALIBRATING, not inside the worker
    # thread -- see _start_recording_worker's docstring for the bug this
    # closes (a stale True here from a prior remote-stopped recording could
    # otherwise make the main loop self-disarm the very next recording
    # almost instantly, before the worker thread ever got to reset it).
    remote_stop_requested = False
    _recording_session_id += 1
    state = STATE_COUNTDOWN
    update_oled("counting down")
    led.value(1)
    _thread.start_new_thread(_start_recording_worker, (_recording_session_id,))


def disarm_recording():
    global state, polling_thread_active
    state = STATE_IDLE
    polling_thread_active = False  # tells _recording_poll_worker to wind down, if it's running
    led.value(0)
    beep(1, 400, 0)
    update_oled("idle (%d)" % sample_count)
    print("[state] DISARMED (buffer holds %d samples)" % sample_count)

    if sample_count > 0:
        print("[state] auto-exporting...")
        if export_data():
            reset_experiment()
        else:
            print("[state] auto-export failed, buffer kept -- retry Export manually")


def reset_experiment():
    global state, sample_count
    state = STATE_IDLE
    sample_count = 0
    led.value(0)
    update_oled("idle")
    print("[state] experiment RESET, buffer cleared")


def drain_samples():
    """Pulls whatever the native sampler task has produced since the last
    call, straight into t_ms_buf/gx_buf/.../az_buf (imu_native.pull() writes
    those array.array buffers directly -- no intermediate copy), then fills
    in what the native side doesn't know about: the per-experiment-relative
    timestamp, the orientation filter, and the always-zero mag columns (see
    the module docstring for why they stay in the CSV format). Returns how
    many samples were actually accepted into the buffer this call -- may be
    0 (nothing new since last call, the common case at this call rate),
    and is not guaranteed to equal what pull() reported if any were stale
    backlog (see t0_native_ms) and got discarded instead of counted.

    Caller is responsible for the MAX_SAMPLES bounds check first."""
    global sample_count, _last_relative_t_ms

    remaining = MAX_SAMPLES - sample_count
    if remaining <= 0:
        return 0

    n = imu_native.pull(t_ms_buf, gx_buf, gy_buf, gz_buf, ax_buf, ay_buf, az_buf, sample_count, remaining)
    accepted = 0
    for i in range(sample_count, sample_count + n):
        rel_t = utime.ticks_diff(t_ms_buf[i], t0_native_ms)
        if rel_t < 0:
            # Backlog the native task produced before this experiment's t0
            # (it samples continuously regardless of Python's state) --
            # discard rather than export a negative timestamp.
            continue

        write_idx = sample_count + accepted
        if write_idx != i:
            gx_buf[write_idx], gy_buf[write_idx], gz_buf[write_idx] = gx_buf[i], gy_buf[i], gz_buf[i]
            ax_buf[write_idx], ay_buf[write_idx], az_buf[write_idx] = ax_buf[i], ay_buf[i], az_buf[i]
        t_ms_buf[write_idx] = rel_t
        mx_buf[write_idx] = my_buf[write_idx] = mz_buf[write_idx] = 0.0

        ax, ay, az = ax_buf[write_idx], ay_buf[write_idx], az_buf[write_idx]
        gx, gy = gx_buf[write_idx], gy_buf[write_idx]
        if _last_relative_t_ms is None:
            init_orientation(ax, ay, az, 0.0, 0.0, 0.0, False)
        else:
            dt = max(0.0, (rel_t - _last_relative_t_ms) / 1000.0)
            update_orientation(ax, ay, az, gx, gy, 0.0, 0.0, 0.0, False, dt)
        roll_buf[write_idx], pitch_buf[write_idx], yaw_buf[write_idx] = roll_deg, pitch_deg, yaw_deg

        _last_relative_t_ms = rel_t
        accepted += 1

    sample_count += accepted
    return accepted


serial_provisioning.set_csv_provider(
    lambda: CSV_HEADER,
    lambda: sample_count,
    format_csv_row,
)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    global sample_count, calibration_samples, next_poll_due_ms

    print("Ready. Sample rate %dHz, max %ds (%d samples) per experiment." % (
        SAMPLE_RATE_HZ, MAX_RECORD_SECONDS, MAX_SAMPLES))
    print("Arm sequence: %.1fs placement delay -> %.1fs calibration -> recording." % (
        ARM_PLACEMENT_DELAY_SECONDS, CALIBRATION_SECONDS))

    while True:
        serial_provisioning.poll()
        now = utime.ticks_ms()

        if btn_start_disarm.pressed_edge():
            if state == STATE_IDLE:
                begin_arm_sequence()
            else:
                disarm_recording()

        if btn_reset.pressed_edge():
            reset_experiment()

        if btn_export.pressed_edge():
            if state != STATE_IDLE:
                print("[export] disarm before exporting")
            else:
                export_data()

        if state == STATE_IDLE:
            if utime.ticks_diff(now, next_poll_due_ms) >= 0:
                next_poll_due_ms = utime.ticks_add(utime.ticks_ms(), POLL_INTERVAL_MS)
                cmd = poll_remote_command()
                if cmd == "arm":
                    begin_arm_sequence()
                elif cmd == "reset":
                    reset_experiment()
                elif cmd == "export":
                    export_data()
                else:
                    # Nothing else refreshes the OLED while idle (it's
                    # otherwise only redrawn on state transitions/button
                    # presses), so the battery line would otherwise look
                    # frozen indefinitely -- piggyback on this existing
                    # periodic tick instead of adding a second timer.
                    update_oled("idle")

        elif state == STATE_ARM_DELAY:
            update_led_blink(now)
            if utime.ticks_diff(now, arm_delay_deadline_ms) >= 0:
                start_calibration()

        elif state == STATE_CALIBRATING:
            update_led_blink(now)
            if utime.ticks_diff(now, calibration_deadline_ms) >= 0:
                finish_calibration_and_start_recording()
            else:
                _collect_calibration_samples()

        elif state == STATE_COUNTDOWN:
            # Deliberately not draining here -- see STATE_COUNTDOWN's
            # definition and _start_recording_worker's docstring. The
            # native sampler keeps queuing samples regardless (up to its
            # own ~5s buffer, comfortably more than the ~3.3s countdown
            # takes); _start_recording_worker discards them via
            # _flush_native_queue() once the beeps finish, right before
            # flipping to STATE_RECORDING.
            pass

        elif state == STATE_RECORDING:
            drain_samples()
            if sample_count >= MAX_SAMPLES:
                print("[state] buffer full, auto-disarming")
                disarm_recording()

            # Cheap flag check -- the actual polling/network I/O for this
            # runs on a separate thread (_recording_poll_worker), so this
            # never blocks drain_samples() above.
            if state == STATE_RECORDING and remote_stop_requested:
                disarm_recording()

        wdt.feed()
        utime.sleep_ms(1)


main()
