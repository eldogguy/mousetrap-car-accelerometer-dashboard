# WiFi Setup (companion app)

**Superseded** -- this WiFi setup panel now also lives inside
[`../acc_flash_app`](../acc_flash_app), alongside firmware flashing and code
upload, so there's one app instead of two. This standalone version still
works and is kept here for reference, but `acc_flash_app` is the one to use
going forward (including the Desktop icon, if you've set one up).

A small desktop app for configuring the WiFi network the ESP32-S3 9DOF IMU
logger uploads to, over USB serial -- no reflashing, no editing
`config.py` by hand.

Only works with the **MicroPython** firmware in `..` (the C++/PlatformIO
version doesn't have the matching serial protocol). The device side is
implemented in [`../serial_provisioning.py`](../serial_provisioning.py).

## Setup

```bash
cd firmware/esp32-9dof-imu-micropython/wifi_provisioner_app
pip3 install -r requirements.txt
python3 app.py
```

`tkinter` (the GUI toolkit) ships with most Python installs, but a few
setups omit it:
- **pyenv-built Python**: rebuild after installing Tcl/Tk, e.g. on macOS
  `brew install tcl-tk` then reinstall your Python version with pyenv.
- **Linux**: `sudo apt install python3-tk` (Debian/Ubuntu) or your distro's
  equivalent package.

If `python3 app.py` fails immediately with an import error mentioning
`tkinter` or `_tkinter`, it's one of these two.

## Usage

1. Plug the ESP32-S3 in over USB (with the MicroPython firmware +
   `main.py`/`serial_provisioning.py`/`wifi_store.py` already uploaded —
   see the main [README](../README.md)).
2. Launch the app, pick the board's port from the dropdown (click
   **Refresh Ports** if it's not listed yet), and click **Connect**.
3. The app pings the device to confirm it's talking to the right firmware,
   then fetches and displays the currently saved SSID (if any).
4. Click **Scan** to have the device's radio scan for nearby networks and
   populate the **Networks** dropdown (sorted strongest signal first, with
   RSSI and open/secured shown). Picking one fills in the SSID field for
   you — you still need to type the password yourself, and hidden networks
   won't show up (type the SSID manually for those).

   Scanning briefly pauses the device's main loop (a few seconds), so
   avoid it while an experiment is actively recording.
5. Enter (or confirm) the SSID and password, click **Save to Device**.
   This writes `/wifi.json` on the device's flash — it's picked up
   automatically the next time the export button triggers a WiFi upload,
   and takes priority over the `WIFI_SSID`/`WIFI_PASSWORD` constants in
   `config.py`.
6. Click **Clear Saved** to delete the credentials stored on the device
   (after a confirmation prompt). Once cleared, the device falls back to
   whatever `WIFI_SSID`/`WIFI_PASSWORD` are set in `config.py` (if any) the
   next time it tries to upload.
7. Click **Test Connection** to have the device attempt to join that
   network right now. A popup reports the result directly — success shows
   the device's IP address, failure shows the specific error (wrong
   password, timed out, etc.), so you don't have to go read the serial
   log to find out — useful for catching a typo'd password before you're
   out in the field relying on it.

The **Device log** panel at the bottom shows the raw serial stream,
including the device's normal debug prints (sensor init messages, sample
recording logs, etc.) alongside the app's own protocol traffic — handy for
troubleshooting if something isn't responding as expected.

## Reset on disconnect

Clicking **Disconnect**, closing the app window, or the device dropping off
USB all trigger a hardware reset pulse on the board (the same DTR/RTS
sequence `esptool` uses for a normal reset, not bootloader mode) right
before the serial port closes. That makes the board reboot and cleanly
re-run `main.py` on its own — you shouldn't need to press the physical
reset button after using this app.

## Protocol notes

Commands are JSON, one per line, sent to the device over the same USB
serial connection used for the Serial monitor. Every reply the device
sends back is prefixed with `@IMU@` so the app can tell protocol replies
apart from the device's plain debug `print()` output sharing that same
serial stream. See the docstring at the top of
[`../serial_provisioning.py`](../serial_provisioning.py) for the exact
command/reply shapes if you want to script against the device directly
(e.g. from a shell script using `stty`/`cat`, or your own tool).

## What I could and couldn't verify

I syntax-checked `app.py` and smoke-tested that the UI actually constructs
without errors (using pyserial's port listing, which worked in my
environment). I could not connect it to a real ESP32-S3 running the
MicroPython firmware here — that needs your hardware. Run through the
Usage steps above and let me know if anything errors or behaves
unexpectedly, especially around the ping handshake or timeouts.
