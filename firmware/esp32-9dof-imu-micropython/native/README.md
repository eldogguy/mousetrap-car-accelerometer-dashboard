# Native module build notes (Phase 0-2 of the BMI270/FreeRTOS rewrite)

See the approved plan for full context/architecture:
`/Users/maxpena/.claude/plans/majestic-frolicking-puppy.md`

## One-time environment setup (already done on this machine)

```bash
mkdir -p ~/esp && cd ~/esp
git clone -b v5.5.1 --recursive --depth 1 --shallow-submodules https://github.com/espressif/esp-idf.git
git clone --recurse-submodules --depth 1 --shallow-submodules -b v1.28.0 https://github.com/micropython/micropython.git
```

**Gotcha**: this machine has a project-local pyenv virtualenv (`mousetrap`, pinned via
`~/.python-version`) that ESP-IDF's installer refuses to run inside
("called from a virtual environment, can not create a virtual environment again").
Bypass it for every ESP-IDF/build command with `PYENV_VERSION=3.14.5` (the plain,
non-virtualenv interpreter). `mpremote` lives in the *other* direction --
it's only installed inside the `mousetrap` virtualenv, so device-side commands
need `PYENV_VERSION=mousetrap` instead. Don't mix these up.

```bash
cd ~/esp/esp-idf
PYENV_VERSION=3.14.5 ./install.sh esp32
PYENV_VERSION=3.14.5 python3 tools/idf_tools.py install cmake ninja   # not installed by default
```

```bash
cd ~/esp/micropython
PYENV_VERSION=3.14.5 make -C mpy-cross
cd ports/esp32
PYENV_VERSION=3.14.5 bash -c "source ~/esp/esp-idf/export.sh && make submodules"
```

## Build

**Two boards, two different build variants -- check which one before building.**
The dev/breadboard board used through Phase 0-3 is an N16R8 (8MB **octal**
SPIRAM); the actual custom board is an **N16R2** (2MB **quad** SPIRAM, confirmed
independently both from the part number and from `esptool`'s own probe:
"Embedded PSRAM 2MB (AP_3v3)"). These need genuinely different firmware images
-- flashing the wrong one is exactly the class of bug that caused the Phase 0
bootloop. Quad and octal PSRAM aren't interchangeable at the firmware level.

```bash
cd ~/esp/micropython/ports/esp32

# N16R8 (octal) -- the original dev board:
PYENV_VERSION=3.14.5 bash -c "source ~/esp/esp-idf/export.sh && \
  make BOARD=ESP32_GENERIC_S3 BOARD_VARIANT=SPIRAM_OCT \
       USER_C_MODULES=$REPO/firmware/esp32-9dof-imu-micropython/native/modules/micropython.cmake \
       CFLAGS_EXTRA=-DIMU_BACKEND_BMI270"

# N16R2 (quad) -- the actual custom board -- note: no BOARD_VARIANT at all,
# the plain/default GENERIC_S3 target auto-detects quad SPIRAM at boot:
PYENV_VERSION=3.14.5 bash -c "source ~/esp/esp-idf/export.sh && \
  make BOARD=ESP32_GENERIC_S3 \
       USER_C_MODULES=$REPO/firmware/esp32-9dof-imu-micropython/native/modules/micropython.cmake \
       CFLAGS_EXTRA=-DIMU_BACKEND_BMI270"
```
(`$REPO` = this repo's absolute path, e.g. `/Users/maxpena/code/First_repository`.)
`-DIMU_BACKEND_L3GD20_LSM303` instead of `-DIMU_BACKEND_BMI270` selects the old
sensor backend (only meaningful on the N16R8 dev board, which is how that
sensor combo was ever actually wired).

## Flash

**Always use `idf.py flash`, never a hand-typed `esptool.py write_flash` command.**
A hand-picked `--flash_size` flag caused a real bootloop during Phase 0 validation --
`idf.py` reads the build's own generated `flasher_args.json`, which has the
flags that actually match this specific image; reconstructing them by hand
is exactly how that broke.

```bash
cd ~/esp/micropython/ports/esp32
PYENV_VERSION=3.14.5 bash -c "source ~/esp/esp-idf/export.sh && \
  idf.py -B build-ESP32_GENERIC_S3-SPIRAM_OCT -p /dev/cu.usbmodemXXXXXXXX flash"   # N16R8
# or -B build-ESP32_GENERIC_S3 (no -SPIRAM_OCT suffix in the build dir name) for N16R2
```

After flashing custom firmware, the filesystem (your `.py` files) survives --
only bootloader/partition-table/app get rewritten, not the filesystem region --
but re-upload via `mpremote cp` is the fallback if anything looks off.

**The custom (N16R2) board specifically needs manual BOOT+RESET before
`idf.py flash`/`esptool` will connect at all** -- its auto-reset circuit
(RTS/DTR toggling the chip into the ROM bootloader automatically, which the
N16R8 dev board's USB-JTAG interface handles transparently) doesn't work
reliably here. Symptom: `esptool`/`idf.py flash` hangs or fails with "No
serial data received" no matter how many times you retry, even though the
port is clearly present (`ls /dev/cu.usbmodem*` still shows it) and unchanged
across a cable replug. Fix: hold BOOT, tap RESET/EN once, release BOOT --
*then* run the flash command. The chip stays in download mode until it's
reset again, so there's a real window to work with. Getting back to normal
run mode afterward just needs a plain RESET/EN press (no BOOT held).
This only affects the hardware bootloader-entry handshake `esptool` needs --
`mpremote` (file uploads, REPL, `machine.reset()`) works completely normally
on this board with no button dance required; only a firmware *reflash* needs it.

## Verify

```bash
PYENV_VERSION=mousetrap mpremote connect /dev/cu.usbmodemXXXXXXXX exec "import sys; print(sys.implementation)"
PYENV_VERSION=mousetrap mpremote connect /dev/cu.usbmodemXXXXXXXX exec "import imu_native; print(imu_native.selftest())"
```

**Gotcha**: qstrs (the `MP_QSTR_xxx` identifiers) are interned globally across
the whole firmware image, not namespaced per-module -- a common function
name can silently collide with something unrelated elsewhere in the build
(frozen modules, other C modules, etc.) and fail with
`error: redeclaration of enumerator 'MP_QSTR_xxx'`. Hit this with `ping` (an
existing qstr elsewhere in the tree); renamed to `selftest`. Worth a quick
grep of `frozen_content.c` under the build dir if a similarly generic name
ever collides again.

## Status

- [x] Phase 0: toolchain installed and validated (stock build byte-identical
      to the board's original firmware; `USER_C_MODULES` scaffold builds,
      flashes, `imu_native.selftest() == 42` over the real REPL).
- [x] Phase 1: real FreeRTOS sampler task (`sampler_task.c`, core 1,
      priority 5, `vTaskDelayUntil` 50Hz) + queue (250-deep) +
      L3GD20/LSM303DLHC backend, built and smoke-tested (graceful failure
      with no sensor wired -- no crash, no hang).
- [x] **Architecture change, mid-Phase-2**: the custom board swapped to a
      real BMI270, wired to the *original* sensor location (GPIO8/9), not
      the dedicated I2C1 bus (GPIO17/18) Phase 1 assumed. Rather than
      requiring new wiring, the OLED driver was also ported to native C
      (`oled_ssd1306.c`) so it can share **one** I2C bus handle with the
      BMI270 sampler instead of needing a second physical bus. This is
      genuinely safe, not a shortcut: verified by reading ESP-IDF's
      `i2c_master.c` directly -- `i2c_master_transmit`/`_transmit_receive`
      (what both the BMI270 backend and the OLED driver call) go through
      `s_i2c_synchronous_transaction()`, which takes a real mutex
      (`bus_lock_mux`) around every transaction. Multiple devices sharing
      one natively-created bus handle are driver-guaranteed not to corrupt
      each other's transactions. The one real constraint: Python's
      `machine.I2C(0, ...)` can't *also* independently claim the same
      physical port -- only one `i2c_master_bus_handle_t` can own a port at
      a time -- so the OLED had to move off Python's I2C entirely, not just
      share wiring with it.
- [x] Phase 2 (BMI270 backend): Bosch's `BMI270_SensorAPI` vendored in
      (`vendor/bosch_bmi270/`, BSD-3-Clause), platform glue
      (`bmi270_platform.c`) wraps the same `driver/i2c_master.h` calls the
      L3GD20/LSM303 backend uses, `backend_bmi270.c` follows Bosch's own
      `bmi270_examples/accel_gyro/accel_gyro.c` calling sequence
      (`bmi270_init` → `bmi2_set_sensor_config` → `bmi2_sensor_enable` →
      `bmi2_get_sensor_data`), ±4g/±500dps ranges matched to the old
      backend so `main.py`'s calibration math sees the same dynamic range.
      **Fully verified on real BMI270 hardware, wired to GPIO8/9**:
      `imu_native.init(8, 9, 400000)` → `True`, real physically-sensible
      samples flowing through `pull()` (az≈1.0g at rest, low-noise gyro),
      `stats()` correctly counting produced/dropped/high-water.
- [x] OLED native driver (`oled_ssd1306.c`): direct C port of `ssd1306.py`'s
      init sequence; text rendering reuses MicroPython's own built-in font
      table (`extmod/font_petme128_8x8.h`, the same one `framebuf.text()`
      uses) via the identical column-major/LSB-at-top bit convention, so
      output is pixel-identical to the old Python driver at zero extra
      flash cost (the font's already compiled in for `framebuf`).
      **Visually confirmed on real hardware**: `oled_update("NATIVE C
      TEST", "HELLO")` rendered correctly on the physical screen.
- [x] Phase 3: `main.py`/`config.py` fully migrated -- `sensors.py` and
      `ssd1306.py` deleted from the device, no more `machine.I2C(0, ...)`
      in Python at all. `append_sample()` replaced by `drain_samples()`
      (pulls from the native queue, converts each sample's timestamp from
      "ms since boot" to "ms since this experiment's calibration began" via
      `imu_native.now_ms()`, discards any pre-experiment backlog, runs the
      orientation filter, writes CSV columns). The stale-read guard is
      gone -- nothing to guard against anymore. `RECORDING_POLL_INTERVAL_MS`
      reverted 2000ms -> 1000ms. All five stale "second core" comments fixed.
      **Full real-hardware validation, remote-triggered end to end** (arm ->
      voice cues -> calibrate -> countdown -> record 20s to a full
      1000-sample buffer -> auto-export -> auto-reset, with WiFi remote-stop
      polling active throughout recording): timestamps ran cleanly
      35ms -> 20015ms, **max inter-sample gap 30ms** (vs the old GIL-based
      firmware's 76ms under the same conditions), **zero anomalies** -- no
      duplicate rows, no out-of-range spikes, nothing for a stale-read guard
      to have needed to catch. This is the number the whole rewrite was
      for: tighter timing *and* structurally can't glitch, not just
      patched to tolerate glitching.

All four phases done. The BMI270 + native FreeRTOS sampler + native OLED
driver are now what's actually running the device.

## Custom board (N16R2) bring-up

The actual custom board arrived with a different pinout than the N16R8 dev
board everything above was validated on, and no SD card slot at all (that
export path was removed entirely from `main.py`/`config.py`, not just
disabled). Current pins, from the board's actual schematic:

| Signal | GPIO | Notes |
|---|---|---|
| Arm/disarm button | 4 | unchanged from dev board |
| Reset button | 5 | unchanged |
| Export button | 6 | unchanged |
| I2C SDA (BMI270 + OLED) | 9 | was 8 on the dev board |
| I2C SCL (BMI270 + OLED) | 10 | was 9 -- also used to be `SD_CS_PIN` before SD was removed |
| Audio out: PWM (tone()) or SDM (voice clips) (to RC filter -> PAM8304 amp) | 16 | unchanged |
| PAM8304 amp shutdown (active-low) | 17 | new -- `audio.py` drives this high only while playing, low otherwise (battery power). Initially misconfigured as GPIO14 (wrong guess, not confirmed against the board) -- silent amp with zero errors was the symptom; fixed once checked against actual hardware. |
| BQ25185 charger STAT1 | 21 | new, not yet read anywhere -- needs the datasheet's STAT1/STAT2 truth table before writing decode logic |
| BQ25185 charger STAT2 | 47 | new, same caveat |
| Battery voltage (ADC) | 7 | new, not yet read anywhere |

Verified on the real N16R2 board: BMI270 initializes and produces
physically-sane data on the new I2C pins (`az≈1.0g` at rest, low gyro
noise). OLED wired and **visually confirmed working** -- `oled_update()`
rendered correctly on the physical screen, sharing the same I2C bus handle
as the BMI270 exactly as designed. Audio **working, beeps and voice clips
both, meaningfully improved but still has a residual harsh/crackly/buzzy
character** -- see the gotchas log below for the full story of the bugs
found and fixed so far: `PAM8304_SHUTDOWN_PIN` (14 -> 17), the countdown
beeps' amp wiring; then voice-clip-specific garbling traced to
`machine.Timer`'s callback not being a real hardware ISR on this port
(fixed by clocking PCM playback from a native ESP-IDF gptimer ISR instead
-- `imu_native.audio_play()` in `audio_sdm.c`); then a two-stage RC
low-pass filter whose original values (4.7k/10nF x2) had a combined cutoff
around ~1.3kHz, well below what an 8kHz-sampled voice clip's real content
needs (4kHz Nyquist) -- fixed by moving both stages to 4.7k/2.2nF
(confirmed via the real schematic, `ACCELEROMETER V2.1.sch`: GPIO16 -> R12
-> [C12 to AGND] -> R11 -> [C11 to AGND] -> C10 (1uF series DC-block) ->
PAM8304 IN+, with C9 (0.1uF) biasing IN- -- R12/C12 is the "input" stage,
R11/C11 the "output" stage), giving a combined cutoff around ~5.8kHz.

**Both stages were already fully populated at 4.7k/2.2nF when the
residual harsh/crackly/buzzy distortion was last tested** -- ruling out
"insufficient carrier suppression from a missing pole" as the explanation
(earlier README revisions suspected exactly that; it was based on a
mistaken assumption about which capacitor was unpopulated at the time and
turned out not to be the case). Schematic-level checks all came back
clean: filter topology and values are correct, a decoupling capacitor (C6,
0.1uF) is present and reasonably valued directly on the PAM8304's
PVDD/VDD, and the board's ground is a solid continuous 4-layer plane (its
"AGND" schematic symbols are cosmetic only -- same electrical net as
regular GND, confirmed via each instance's `value="GND"` attribute -- but
a solid plane is standard/preferred practice for mixed-signal boards at
this complexity anyway, not a red flag by itself).

**Current leading hypothesis, not yet tested: C9/C11/C12 are unknown-
dielectric generic 0603 ceramic capacitors.** Class 2 ceramic dielectrics
(X7R, and especially Y5V/Z5U) exhibit real capacitance loss under DC bias
(sometimes 20-80%, which would shift the actual in-circuit filter cutoff
away from the calculated 5.8kHz) and are piezoelectric -- they can
physically vibrate from a signal carrying real energy near/in the audio
band (which describes both the voice signal and any residual SDM carrier
bleed-through here) and re-radiate that as audible buzz. Both mechanisms
produce exactly the "harsh/crackly/buzzy" character reported. **Next step
when picked back up: rework C11 and C12 (the two capacitors that actually
set the filter's audible-band-adjacent cutoff) to 2.2nF C0G/NP0 dielectric,
0603** -- C0G/NP0 has ~zero DC-bias derating and no meaningful piezoelectric
effect. C9 (0.1uF, biases the amp's IN- reference) is a secondary
candidate for the same upgrade if a C0G/NP0 part is practical to source at
that value/footprint (may need 0805 instead of 0603). C10 (1uF, series
DC-block only, not part of the audible-band filter) and C6 (simple bulk
decoupling) don't need this -- X7R is fine for both. If C0G/NP0 caps don't
resolve it, the next diagnostic step is direct oscilloscope probing at the
amp's IN+ pin (and separately its GND pin) to distinguish "signal-band
noise/carrier bleed-through" from "noise coupled in via the shared ground
plane" directly, rather than continuing to guess from the schematic alone.

Volume is separately tunable via `config.py`'s `VOICE_MAX_DENSITY`
(Python-only change, no rebuild) -- currently set to 90 (ESP-IDF's
suggested dithering cap); 127 (the hardware's true ceiling) was tried and
worked too, kept at 90 for now since the remaining issue is capacitor
quality/noise, not loudness.
Full remote arm/record/export cycle confirmed working end to end via the
web dashboard. Buttons haven't been individually tested yet, though the
export/arm/reset buttons are exercised indirectly by the dashboard's
remote-control commands, which do use the same code paths.

## Build gotchas log

- **`mpremote fs cp` of a large file (main.py in particular) can now trip
  the hardware watchdog mid-transfer.** WATCHDOG_TIMEOUT_MS is 10s
  (config.py) -- entering raw REPL for `fs cp` interrupts `main()`'s loop,
  which is the only thing that ever calls `wdt.feed()`, so nothing feeds
  the watchdog for the whole transfer. A large enough file takes long
  enough that the watchdog fires mid-upload, corrupting the transfer and
  disconnecting the USB CDC port (confirmed via uptime resetting to ~5s
  right after a failed `fs cp`). Workaround: before a big upload, connect
  and run `machine.WDT(timeout=60000)` (or similar) to temporarily extend
  it on the currently-running instance -- the next real reboot restores
  the correct production value from config.py automatically, since
  `machine.WDT(timeout=WATCHDOG_TIMEOUT_MS)` re-runs fresh at the top of
  every boot. Small file uploads (config.py, battery.py, etc.) haven't hit
  this in practice -- it's specifically main.py's size that matters.
- **qstr collisions are common with short/generic names.** Hit three times
  now (`ping`, `drain`, and none on the OLED names because they were
  checked first). Before naming a new Python-visible function, grep the
  build's own `frozen_content.c` for `MP_QSTR_<candidate>\b` and pick a name
  with zero hits -- cheaper than a failed build cycle.
- **`xTaskCreatePinnedToCore` needs `#include "freertos/idf_additions.h"`**
  on this ESP-IDF version -- it's an ESP-IDF extension to FreeRTOS, not
  declared by base `freertos/task.h` the way `xTaskCreate` is.
- **`uint` isn't a standard C type.** MicroPython's own source (e.g.
  `modframebuf.c`) can use it because their own headers typedef it; files
  outside that tree need `unsigned int` explicitly.
- **Testing native I2C code that shares a port with `main.py`'s own
  `machine.I2C(0, ...)` needs `main.py` out of the way first** (move it
  aside via `mpremote fs cp`/`fs rm`, reset, test, restore) -- otherwise the
  two both try to claim the same physical port and the native side's bus
  creation fails. Also: restoring a file via `mpremote fs cp` appears to
  trigger a soft-reset on this setup, which re-runs `main.py` immediately --
  don't assume a physical display's contents reflect your last native write
  until you've confirmed no `fs` operation happened after it.
- **mpremote's raw-REPL entry is flaky on this setup** (a recurring pattern
  all session, not new to native module work) -- `TransportError: could not
  enter raw repl` on maybe 1 in 4 attempts, unrelated to firmware
  correctness. Just retry once the device has settled; don't chase it as a
  real bug.
- **The N16R2 custom board needs manual BOOT+RESET for `esptool`/`idf.py
  flash`** -- see the Flash section above. Cost real time before the actual
  cause (missing/non-functional auto-reset circuit) was identified; the
  tell is `esptool` failing identically on every retry with "No serial data
  received" while the port itself stays present and unchanged across a
  cable replug -- that's a hardware auto-reset issue, not a flaky-cable or
  flaky-mpremote issue.
- **Never run two things that both want the serial port at once** (a
  background Python listener + `mpremote` in the same window, or two
  `mpremote` calls overlapping) -- one gets a port-busy/contention error,
  and the failure mode looks confusingly like a device hang rather than
  what it is. Trigger an action and wait for it to fully finish (even if it
  errors during its own cleanup, e.g. `machine.reset()` disconnecting the
  port mid-close) before opening a new connection.
- **`import main` re-executes the whole file, including its unconditional
  `main()` call at the bottom** -- MicroPython doesn't register the
  auto-run boot script in `sys.modules`, so "peek at the running instance's
  state" via `import main` isn't a read, it's a second competing instance
  of the entire state machine. Caused a real hang once. To inspect live
  state instead: call the specific native functions directly
  (`imu_native.init()`/`.pull()`/`.stats()` are idempotent/safe to re-call),
  or capture a fresh boot's `print()` output, never `import main`.
- **A wrong-but-plausible GPIO number for a new peripheral fails completely
  silently.** `PAM8304_SHUTDOWN_PIN` was set to 14 without checking it
  against the actual board (it's really 17) -- the code ran with zero
  errors, `machine.Pin(14, OUT)` toggled high/low exactly as commanded, and
  the amp was just permanently silent because nothing was actually listening
  on that pin. No exception, no log line, nothing to grep for. Confirmed by
  testing both polarities directly (bypassing `audio.py` entirely) and
  getting silence either way -- that ruled out a polarity bug and pointed at
  "wrong pin entirely," which turned out to be right. Lesson: for any new
  peripheral pin on a board this session hasn't independently verified,
  don't just trust the number as given -- if something is unexpectedly
  silent/inert with no errors at all, wrong-pin is a prime suspect, and
  testing the failure mode directly (both polarities, bypassing the
  higher-level module) narrows it down fast.
- **`machine.Timer` on this MicroPython port has no real hard-IRQ mode --
  `hard=True` raises "hard Timers are not implemented," and even the default
  mode's hardware ISR only calls `mp_sched_schedule()`, which queues the
  Python callback for whenever the interpreter next services it, not a real
  fixed tick.** This was the actual root cause of voice clips sounding
  "garbled/jumbled" while simple beeps always sounded fine -- beeps hold one
  fixed frequency for their whole duration and never depend on per-callback
  timing, voice playback was stepping through ~8000 samples/sec via exactly
  that unreliable scheduled callback. Switching the *output* stage from
  manually-stepped `machine.PWM` duty to the real SDM hardware peripheral
  did NOT fix it by itself -- that ruled out the modulation scheme and is
  what pointed at the timing mechanism instead. Fix: clock PCM samples from
  a real `driver/gptimer.h` hardware alarm ISR in native C
  (`imu_native.audio_play()` in `audio_sdm.c`), which touches only the SDM
  peripheral directly with no Python/scheduler involvement per sample.
  Lesson for next time a MicroPython `machine.Timer` callback needs to hit a
  precise rate: check whether the port supports `hard=True` at all before
  assuming the callback is a real ISR.
- **A low-pass filter sized only to reject a high-frequency carrier can
  silently eat the signal you actually wanted.** The two-stage cascaded RC
  filter (4.7k/10nF per stage, unbuffered) between `BUZZER_PIN` and the
  PAM8304 amp had a combined -3dB cutoff around **1.3kHz**, not the ~3.4kHz
  a single stage's `1/(2*pi*R*C)` alone would suggest -- an unbuffered
  cascade's second stage loads the first, pulling the real cutoff down
  further than that naive formula implies. Voice clips are sampled at
  8kHz (4kHz Nyquist), so most of the actual speech content (especially
  consonants, which live in the 2-4kHz range) was being filtered out before
  reaching the amp at all -- this looked exactly like "distortion" even
  after the timing bug above was fixed, because the audio was intelligible
  and pitch-correct but muffled/degraded. Confirmed by playing the *source*
  PCM files on a computer (converted to WAV, bypassing the ESP32 chain
  entirely) and hearing them clean -- that isolated the bug to the analog
  output stage, not the digital pipeline. Fixed by changing both stages'
  capacitors from 10nF to 2.2nF (raising the combined cutoff to ~5.8kHz,
  comfortably above the 4kHz Nyquist limit) while still keeping ~70dB+
  attenuation at the SDM's 1MHz carrier -- plenty of stopband margin
  remained. Lesson: when sizing an
  analog filter meant to reject a switching/carrier frequency, check its
  passband response against the *actual signal bandwidth* too, not just the
  stopband attenuation at the carrier -- and for an unbuffered multi-stage
  passive RC filter, don't assume the combined cutoff is what a single
  stage's naive formula gives you.
