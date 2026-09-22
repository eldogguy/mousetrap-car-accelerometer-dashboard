# Acceleration analysis

`analyze_run.py` turns a CSV exported by the IMU logger (SD card, WiFi
upload, or the `dump_csv` serial command) into a calibrated, deadzone-
filtered acceleration-vs-time plot — built for mousetrap car runs, but
works on any recording from this device.

## Calibration routine

The first `--calibration-seconds` of every recording (default 1.0s) is
treated as a calibration window — the car must be sitting completely
still, not yet released, for the whole window. From it, the script derives
two things:

1. **Gravity vector.** A stationary accelerometer reads ~1g (it senses the
   normal force resisting gravity, not zero), so this has to be subtracted
   from every sample before "acceleration" means anything physically
   useful. Also compensates for however the board happens to be
   mounted/tilted on the car.
2. **Deadzone threshold.** After subtracting gravity, whatever magnitude
   is *still* left over during that same still window is pure noise —
   sensor jitter plus any ambient vibration in the system (desk rumble,
   the mousetrap mechanism itself, etc), not real motion. The script takes
   the mean + `--deadzone-sigma` standard deviations of that residual as a
   threshold, and clamps any reading in the actual recording below it to
   0. This keeps ambient vibration from being mistaken for the car
   accelerating.

## For valid calibration, record it this way

1. Press **Start/Disarm** on the device.
2. Wait the full calibration window (default 1s) with the car completely
   stationary on the starting line.
3. Release the car, let it run.
4. Press **Start/Disarm** again to stop.

If the car is already moving during that window, both the gravity vector
and the noise floor get contaminated by real motion — the deadzone
threshold ends up too high and clamps away real data along with the noise.
The script has no way to detect this after the fact, so it's on you to
record it right.

## Usage

```bash
cd firmware/esp32-9dof-imu-micropython/analysis
pip3 install matplotlib   # if not already installed

python3 analyze_run.py path/to/recording.csv
```

Prints the calibration summary (gravity vector, noise floor, deadzone
threshold, % of samples clamped) and produces, next to the input CSV by
default:
- `<name>_accel.png` — two stacked plots: net acceleration magnitude (m/s²) vs time (raw signal in light gray, deadzone threshold as a dashed line, filtered result in red), and the individual gravity-compensated x/y/z components (g) vs time, unfiltered (useful for spotting which axis lines up with the car's direction of travel).
- `<name>_accel.csv` — computed columns (`time_s`, `accel_net_g_filtered`, `accel_net_ms2_filtered`, `accel_net_g_raw`, `accel_x_net_g`, `accel_y_net_g`, `accel_z_net_g`) if you want to do further analysis (e.g. numerically integrate for velocity) in a spreadsheet. Both the filtered and raw (pre-deadzone) magnitude are included in case you want to compare.

Options:
- `--calibration-seconds N` — length of the calibration window (default 1.0s).
- `--deadzone-sigma N` — how many standard deviations above the noise-floor mean to set the deadzone threshold at (default 3.0). Raise it if ambient vibration is still leaking through as false motion; lower it if real low-acceleration motion is getting clamped away.
- `--no-deadzone` — skip deadzone filtering entirely, gravity compensation only (same behavior as before this feature was added).
- `--output path.png` / `--csv-output path.csv` — override the default output paths.
- `--no-show` — just save the files, don't pop up an interactive plot window (useful for batch-processing several runs).
