"""1-bit audio out on BUZZER_PIN, meant to feed an external RC low-pass
filter -> small amplifier -> speaker.

Not real hardware PDM: this MicroPython build's machine.I2S has no PDM TX
mode (checked directly on-device -- it only exposes MONO/STEREO/TX/RX, no
PDM constant, and TX mode is for talking to an external I2S DAC chip, not
driving a bare RC filter).

tone() (simple beeps) uses plain machine.PWM at a fixed high-frequency
carrier, duty-modulated -- verified stable at 8000Hz on real hardware and
sounds correct as-is, no need to touch it.

play_pcm() (voice clips) uses two things machine.PWM/machine.Timer can't
give us, both native (imu_native.sdm_*/audio_play, see
native/modules/imu_sampler/audio_sdm.c):

1. The ESP32-S3's dedicated hardware Sigma-Delta Modulation peripheral, in
   place of manually stepping machine.PWM's duty cycle to trace the
   waveform.
2. A real ESP-IDF gptimer hardware alarm clocking samples out of a native C
   ISR, in place of machine.Timer + a Python callback.

Voice clips came out garbled through the original PWM+Timer approach.
Switching just the output stage to SDM (#1 alone) did NOT fix it, which
ruled out the modulation scheme and pointed further upstream: reading
MicroPython's own ports/esp32/machine_timer.c shows `machine.Timer` on this
port has no real hard-IRQ mode at all (hard=True explicitly raises "hard
Timers are not implemented") -- even its default "soft" callback only gets
*scheduled* from the timer's hardware ISR (mp_sched_schedule()), then runs
whenever the interpreter next services that queue, not on a real fixed
tick. Fine for a slow LED blink, nowhere near deterministic enough to clock
8000 samples/sec -- and exactly why tone()'s fixed-frequency beeps always
sounded correct (no per-sample timing involved) while voice clips didn't.
audio_play() fixes this by clocking samples from a real hardware timer ISR
in C instead, with no Python/scheduler involvement per sample.

SDM drives the *same* GPIO as BUZZER_PIN (config.py) -- still a 1-bit
digital signal into the same RC-filter-into-amp chain tone() already uses,
so no rewiring, just a different peripheral driving the same pin at
different times. audio.py deinits its machine.PWM object before calling
sdm_init() (and re-creates it after sdm_deinit()) since only one peripheral
can own a GPIO's output routing at a time.
"""

import machine
import utime

import imu_native

from config import (
    BUZZER_PIN,
    AUDIO_CARRIER_HZ,
    AUDIO_SAMPLE_RATE_HZ,
    PAM8304_SHUTDOWN_PIN,
    SDM_CARRIER_HZ,
    VOICE_MAX_DENSITY,
)

_pwm = machine.PWM(machine.Pin(BUZZER_PIN), freq=AUDIO_CARRIER_HZ, duty_u16=0)

# PAM8304 amp: SD (shutdown) pin is active-LOW. Start shut down (silent,
# lowest power) and only enable it for the duration of an actual
# tone()/play_pcm() call -- on a battery-powered board there's no reason to
# keep the amp powered the rest of the time.
_amp_shutdown = machine.Pin(PAM8304_SHUTDOWN_PIN, machine.Pin.OUT)
_amp_shutdown.value(0)
_AMP_SETTLE_MS = 5  # conservative wake-from-shutdown settle time before driving real audio


def _amp_enable():
    _amp_shutdown.value(1)
    utime.sleep_ms(_AMP_SETTLE_MS)


def _amp_disable():
    _amp_shutdown.value(0)


def tone(freq_hz, duration_ms):
    """Blocking single-pitch beep."""
    _amp_enable()
    _pwm.freq(freq_hz)
    _pwm.duty_u16(32768)  # 50% duty square wave
    utime.sleep_ms(duration_ms)
    _pwm.duty_u16(0)
    _amp_disable()


def play_pcm(data, sample_rate_hz=AUDIO_SAMPLE_RATE_HZ):
    """Blocking playback of raw 8-bit unsigned PCM bytes, natively timed and
    modulated (see module docstring for why, not PWM+machine.Timer)."""
    if not data:
        return
    _amp_enable()
    # Free BUZZER_PIN so the SDM peripheral can drive it instead -- only one
    # of PWM/SDM can own a GPIO's output routing at a time.
    _pwm.duty_u16(0)
    _pwm.deinit()
    if not imu_native.sdm_init(BUZZER_PIN, SDM_CARRIER_HZ):
        print("[audio] sdm_init failed, skipping playback")
        _pwm.init(freq=AUDIO_CARRIER_HZ, duty_u16=0)
        _amp_disable()
        return
    imu_native.audio_play(data, sample_rate_hz, VOICE_MAX_DENSITY)  # blocks until the clip finishes
    imu_native.sdm_deinit()
    # Hand BUZZER_PIN back to PWM so tone() keeps working afterward.
    _pwm.init(freq=AUDIO_CARRIER_HZ, duty_u16=0)
    _amp_disable()


_voice_cache = {}


def play_voice(name):
    """Loads and plays voice_<name>.pcm from flash (cached after first read
    -- these clips are small, ~8-13KB each, cheap to keep resident)."""
    data = _voice_cache.get(name)
    if data is None:
        try:
            with open("voice_%s.pcm" % name, "rb") as f:
                data = f.read()
        except OSError:
            print("[audio] voice_%s.pcm not found, skipping" % name)
            return
        _voice_cache[name] = data
    play_pcm(data)
