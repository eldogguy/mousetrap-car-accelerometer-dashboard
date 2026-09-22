# ACC Flash App

Desktop app (Tkinter) that flashes the ESP32-S3 9DOF IMU logger's firmware,
uploads its Python source files, and configures its WiFi credentials --
all over USB serial, in one tool. Wraps the exact `esptool`/`mpremote`
commands this project's development has used manually all along (see
`../native/README.md` for the full history) plus the JSON-line WiFi
provisioning protocol (`../serial_provisioning.py`) into one guided tool,
so setting up a board doesn't require remembering any of that by hand or
switching between two separate apps.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt
python3 app.py
```

## What it does

- **Flash Firmware**: writes the pre-built N16R2/BMI270 firmware image from
  `firmware_bin/` (see that directory's own README for how to refresh it
  after a native C rebuild -- this tool never rebuilds from source itself).
  This board's auto-reset circuit doesn't work over USB, so the app walks
  you through the manual BOOT+RESET button sequence before flashing, and
  reminds you to press RESET (not BOOT) afterward to boot into the app.
- **Upload Code**: uploads `main.py`, `config.py`, `audio.py`, `battery.py`,
  `device_key.py`, `serial_provisioning.py`, `wifi_store.py`, and the two
  voice clips directly from the parent `firmware/esp32-9dof-imu-micropython/`
  directory (not a copy -- edits there are picked up the next time you
  click Upload). Temporarily extends the device's watchdog timeout before
  uploading `main.py` specifically, since that file is large enough to
  trip the normal 10s timeout mid-transfer otherwise; a final reset at the
  end restores the real (short) production value.
- **WiFi Setup**: connects over the same USB serial port and speaks the
  JSON-line protocol implemented in `../serial_provisioning.py` (ping,
  scan, save, test, clear) -- the same feature that used to live in the
  standalone `../wifi_provisioner_app/`, now folded in here.

## Port sharing between Flash/Upload and WiFi Setup

Flash and Upload shell out to `esptool`/`mpremote` per click, which need
exclusive access to the serial port. WiFi Setup instead holds the port open
persistently for as long as you're "Connected" in that panel (so it can
receive scan results, ping replies, etc. without polling). These two modes
can't share the port at once, so:

- Clicking **Flash Firmware** or **Upload Code** while WiFi Setup is
  connected automatically disconnects it first (logged in the panel) so
  the port is free -- no manual step needed.
- The WiFi Setup **Connect** button is disabled while a flash or upload is
  in progress, since the port is in use by that subprocess.

## What it deliberately doesn't do

- **Rebuilding firmware from source** -- that needs the full ESP-IDF
  toolchain (multi-GB) and isn't something you want to do just to flash a
  board. If you're actively changing the native C code, keep using the
  manual `idf.py`/`make` workflow in `../native/README.md` and just refresh
  `firmware_bin/` here when you're ready to distribute a new build.
