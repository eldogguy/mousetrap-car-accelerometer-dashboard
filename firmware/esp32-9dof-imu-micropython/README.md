# ESP32-S3 + L3GD20 / LSM303DLHC 9DOF data logger (MicroPython)

A MicroPython port of the firmware in [`../esp32-9dof-imu`](../esp32-9dof-imu)
(C++ / PlatformIO). Same sensors, same button behavior, same CSV format,
same wiring/pins — different language.

**Important:** this replaces the board's firmware at a lower level than the
C++ version. To run this, you flash **MicroPython itself** onto the
ESP32-S3 (erasing whatever's on it, including the PlatformIO firmware if
you flashed that earlier), then copy these `.py` files onto its filesystem.
You can't have both firmwares "installed" at once — flashing one replaces
the other. If you want to go back to the C++ version later, re-run
`../esp32-9dof-imu/install.sh`.

## Why you might pick this over the C++ version

- Edit-and-reset iteration (no compile step) — good for tuning constants or experimenting with the orientation filter.
- Easier to read/modify if you're more comfortable in Python than C++.

## Trade-offs vs. the C++ version

- **Smaller buffer by default**: 50 Hz × 20 s = 1000 samples (~52 KB) vs. the C++ version's 100 Hz × 30 s (~156 KB). MicroPython's interpreter itself eats into the ESP32-S3's RAM, so there's less room for the sample buffer — push `SAMPLE_RATE_HZ`/`MAX_RECORD_SECONDS` up carefully and watch for `MemoryError`.
- **Lower practical sample rate ceiling**: this is a plain Python loop, not compiled code — 50 Hz is a safer default than the C++ version's 100 Hz. If you need higher rates or a much bigger buffer, the C++ version is the better fit.
- Otherwise the behavior, CSV columns, and orientation-estimate caveats are identical — see the C++ README's "CSV format" section for the full column list and calibration caveats, they apply here unchanged.

## 1. Flash MicroPython onto the ESP32-S3

Download the ESP32-S3 MicroPython firmware `.bin` from
https://micropython.org/download/ (pick the build matching your board —
generic ESP32-S3, or one with PSRAM/octal-flash support if your board has
it), then:

```bash
pip3 install esptool

# Fully erase the flash first (removes any previous C++/PlatformIO firmware)
esptool.py --chip esp32s3 -p <PORT> erase_flash

# Flash MicroPython (adjust the filename to whatever you downloaded)
esptool.py --chip esp32s3 -p <PORT> -b 460800 write_flash 0x0 ESP32_GENERIC_S3-*.bin
```

`<PORT>` is the board's serial port (e.g. `/dev/cu.usbmodem1101` on macOS,
`/dev/ttyUSB0` on Linux) — same port you'd use with the C++ version's
`install.sh`.

## 2. Upload the files to the board

Use [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html)
(the official MicroPython tool, installable via pip):

```bash
pip3 install mpremote

cd firmware/esp32-9dof-imu-micropython
mpremote connect <PORT> fs cp config.py sensors.py wifi_store.py serial_provisioning.py main.py :
```

The board reboots and runs `main.py` automatically on power-up once it's on
the filesystem (MicroPython auto-runs `boot.py` then `main.py` at startup).
To watch its Serial output:

```bash
mpremote connect <PORT>
# Ctrl-] to exit the REPL/serial session
```

To edit a config value and re-test without a full reflash, just re-copy the
changed file with the same `mpremote ... cp` command and reset the board
(`mpremote connect <PORT> reset`, or the physical reset button).

## Wiring

Identical to the C++ version — see
[`../esp32-9dof-imu/README.md`](../esp32-9dof-imu/README.md#wiring-default-pins-see-srcconfigh-to-change)
for the full pinout table (I2C for the L3GD20/LSM303DLHC, SPI for the SD
card, GPIO4/5/6 for the three buttons, GPIO15 for the status LED).

## Configuration

Everything tunable lives in `config.py`, mirroring `Config.h` from the C++
version: `ENABLE_SD_EXPORT`/`ENABLE_WIFI_EXPORT`, `SAMPLE_RATE_HZ`/
`MAX_RECORD_SECONDS`, `COMPLEMENTARY_FILTER_ALPHA`, WiFi credentials, and
the upload server target.

## SD card

This uses MicroPython's built-in `machine.SDCard` class in SPI mode
(`slot=2` with explicit `sck`/`mosi`/`miso`/`cs` pins), which is included in
standard ESP32-S3 MicroPython builds. If your specific firmware build
doesn't include `machine.SDCard` (some minimal/custom builds omit it),
you'd need a community SPI SD driver (`sdcard.py`) instead — say so and
this can be swapped in.

## WiFi upload

A raw chunked-transfer-encoding HTTP POST to
`https://SERVER_HOST:SERVER_PORT{SERVER_UPLOAD_PATH_PREFIX}/{device_key}`,
built with the `socket` module directly (no `urequests` dependency), so it
streams row-by-row instead of holding the whole CSV in RAM. `SERVER_HOST`
points at the live `device-dashboard` Vercel deployment by default (a
hostname, not an IP -- both SNI and the HTTP `Host` header need it, since
Vercel's edge routes by hostname). TLS is a bare `ssl.wrap_socket()` with
no certificate verification (no CA bundle on the device) -- fine for a
classroom device with nothing sensitive in the payload, not a hardened
setup. Set `SERVER_USE_TLS = False` and point `SERVER_HOST`/`SERVER_PORT`
back at a plain-HTTP local dev server if you want to test against one
instead.

## Configuring WiFi credentials without reflashing

Instead of hardcoding `WIFI_SSID`/`WIFI_PASSWORD` in `config.py`, you can
set (or change) them at runtime over USB with the companion desktop app in
[`wifi_provisioner_app/`](wifi_provisioner_app/):

```bash
cd wifi_provisioner_app
pip3 install -r requirements.txt
python3 app.py
```

Plug the board in, pick its port, connect, click **Scan** to list nearby
networks (pick one to fill in the SSID), and enter the password — the app
also has a **Test Connection** button that has the device attempt to join
right now and reports back its IP (or the error) before you rely
on it in the field. See that folder's README for details.

Under the hood: `main.py` runs a small non-blocking JSON-over-serial
command listener (`serial_provisioning.py`) alongside its normal sampling
loop, and persists credentials to `/wifi.json` on the device's flash
(`wifi_store.py`). Saved credentials there take priority over `config.py`'s
`WIFI_SSID`/`WIFI_PASSWORD`, which become just a fallback default for
boards that haven't been provisioned yet. This only works with this
MicroPython firmware — the C++/PlatformIO version still requires editing
`Config.h` and reflashing to change WiFi credentials.

## Verification note

I syntax-checked all three `.py` files with CPython's `py_compile` (catches
typos/syntax errors), but **could not runtime-test this against real
hardware or a real MicroPython interpreter** in this environment — modules
like `machine`, `network`, and `os.mount`/`machine.SDCard` only exist on
the device itself. Flash it and watch the Serial output for the
`[gyro]`/`[accel]`/`[mag] initialized` vs. `NOT FOUND` messages; report
back anything that errors and I'll fix it.
