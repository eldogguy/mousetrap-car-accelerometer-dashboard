#!/usr/bin/env python3
"""Plots gravity-compensated, deadzone-filtered acceleration, velocity, and
position vs time from a CSV exported by the ESP32 9DOF IMU logger -- built
for mousetrap car runs, but works on any recording from this device.

Calibration routine (runs on the first `--calibration-seconds` of the
recording, so the car must be sitting still -- not yet released -- during
that window):
  1. Averages accel readings over the window to get the gravity vector for
     however the board happens to be mounted/tilted. A stationary
     accelerometer reads ~1g (it senses the normal force resisting
     gravity, not zero), so this has to be subtracted before "acceleration"
     means anything physically useful.
  2. After subtracting that gravity vector, measures how much residual
     magnitude is left in the *same still window* -- this is the noise
     floor: sensor noise plus any ambient vibration in the system (desk
     jitter, the mousetrap mechanism itself, etc), not real motion.
  3. Sets a deadzone threshold from that noise floor (mean + N standard
     deviations, N = --deadzone-sigma). Anywhere in the actual recording
     where the compensated acceleration magnitude stays under that
     threshold gets treated as "no real motion" (clamped to 0), so
     ambient vibration doesn't get mistaken for the car accelerating. This
     also applies to the signed per-axis values before they get integrated
     below, or noise would integrate into fake velocity/position drift.

Any dropped-sample gap in the raw timestamps (see append_sample's
stale-read guard in main.py) gets linearly interpolated first, so the
velocity/position integration below runs on an even time grid -- see
fill_gaps(). Velocity and position are trapezoidal integrals of the signed,
deadzone-clamped per-axis acceleration (not the net magnitude, which is
always >=0 and would only ever accumulate upward). Position is a rough
estimate, not a precise track -- it's a double integral of a noisy MEMS
sensor, so drift accumulates the longer the car moves.

Usage:
    python3 analyze_run.py recording.csv
    python3 analyze_run.py recording.csv --calibration-seconds 0.5 --output run1_accel.png
    python3 analyze_run.py recording.csv --deadzone-sigma 4   # stricter deadzone
    python3 analyze_run.py recording.csv --no-deadzone        # gravity compensation only
    python3 analyze_run.py recording.csv --no-show   # save files only, don't pop up a window
"""

import argparse
import csv
import math
import os
import sys

import matplotlib.pyplot as plt

G_TO_MS2 = 9.80665

# The car's direction of travel on this mount is the positive X axis (per
# the classroom rig's mounting orientation) -- forward = +X. Flipped from
# an earlier -X assumption once real runs showed the sign backwards
# (forward motion read as deceleration/reverse). If a car ever gets
# mounted the other way around again, this is the one place to flip. Keep
# in sync with device-dashboard/lib/motion.ts's FORWARD_SIGN.
FORWARD_SIGN = 1


def read_rows(path):
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "t_ms": int(row["timestamp_ms"]),
                "ax": float(row["accel_x_g"]),
                "ay": float(row["accel_y_g"]),
                "az": float(row["accel_z_g"]),
            })
    return rows


def fill_gaps(rows):
    """A dropped/duplicate sample on the device (see append_sample's
    stale-read guard in main.py) shows up here as one real timestamp gap
    roughly 2x the nominal sample interval instead of 1x. Left alone, that
    doubled dt throws off the trapezoidal integration below by a small but
    real amount -- filling it with linearly-interpolated row(s) keeps
    velocity/position on an even time grid. The nominal interval is
    estimated from the data itself (median of consecutive deltas), not
    hardcoded, so this doesn't need to know SAMPLE_RATE_HZ. A gap much
    larger than a few missed samples is left alone -- that's a real hole,
    not a stale-read blip, and papering over it would be misleading.
    Returns rows with an added "interpolated" bool field."""
    if not rows:
        return []
    deltas = sorted(rows[i]["t_ms"] - rows[i - 1]["t_ms"] for i in range(1, len(rows)))
    nominal_ms = deltas[len(deltas) // 2] if deltas else 1

    out = [dict(rows[0], interpolated=False)]
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        dt = cur["t_ms"] - prev["t_ms"]
        missing = round(dt / nominal_ms) - 1 if nominal_ms else 0
        if 0 < missing <= 10:
            for k in range(1, missing + 1):
                frac = k / (missing + 1)
                out.append({
                    "t_ms": prev["t_ms"] + frac * dt,
                    "ax": prev["ax"] + (cur["ax"] - prev["ax"]) * frac,
                    "ay": prev["ay"] + (cur["ay"] - prev["ay"]) * frac,
                    "az": prev["az"] + (cur["az"] - prev["az"]) * frac,
                    "interpolated": True,
                })
        out.append(dict(cur, interpolated=False))
    return out


def calibration_window(rows, calibration_seconds):
    cutoff_ms = rows[0]["t_ms"] + calibration_seconds * 1000
    window = [r for r in rows if r["t_ms"] <= cutoff_ms] or rows[:1]
    return window


def compute_gravity_baseline(window):
    n = len(window)
    bx = sum(r["ax"] for r in window) / n
    by = sum(r["ay"] for r in window) / n
    bz = sum(r["az"] for r in window) / n
    return bx, by, bz, n


def compute_deadzone_threshold(window, bx, by, bz, sigma):
    """Residual acceleration magnitude in the calibration window, after
    gravity subtraction, is pure noise/vibration by definition (the car
    was still). mean + sigma*stddev of that gives a threshold above which
    a reading in the actual recording is probably real motion."""
    residual_mags = []
    for r in window:
        cx, cy, cz = r["ax"] - bx, r["ay"] - by, r["az"] - bz
        residual_mags.append(math.sqrt(cx * cx + cy * cy + cz * cz))
    n = len(residual_mags)
    mean = sum(residual_mags) / n
    variance = sum((m - mean) ** 2 for m in residual_mags) / n
    stddev = math.sqrt(variance)
    return mean + sigma * stddev, mean, stddev


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", help="CSV file exported from the IMU logger")
    parser.add_argument("--calibration-seconds", type=float, default=5.0,
                         help="How much of the start of the recording is the calibration window "
                              "(default: 5.0s, matching CALIBRATION_SECONDS in config.py and "
                              "lib/motion.ts's default on the web app -- override this only if you "
                              "changed that firmware constant). The car must be motionless -- not yet "
                              "released -- for this entire window; it's used for both the gravity "
                              "baseline and the ambient-vibration noise floor.")
    parser.add_argument("--deadzone-sigma", type=float, default=3.0,
                         help="Deadzone threshold = calibration noise mean + this many standard "
                              "deviations (default: 3.0). Readings below the threshold are treated "
                              "as noise/vibration, not real motion, and clamped to 0. Raise this if "
                              "ambient vibration is still leaking through as false motion; lower it "
                              "if real low-acceleration motion is getting clamped away.")
    parser.add_argument("--no-deadzone", action="store_true",
                         help="Skip deadzone filtering entirely -- gravity compensation only.")
    parser.add_argument("--output", default=None, help="Output PNG path (default: <csv>_accel.png)")
    parser.add_argument("--csv-output", default=None,
                         help="Output CSV path for computed acceleration columns (default: <csv>_accel.csv)")
    parser.add_argument("--no-show", action="store_true",
                         help="Don't open an interactive plot window, just save the PNG")
    args = parser.parse_args()

    rows = read_rows(args.csv_path)
    if len(rows) < 2:
        sys.exit("Not enough samples in this CSV to analyze.")
    rows = fill_gaps(rows)
    n_interpolated = sum(1 for r in rows if r["interpolated"])
    if n_interpolated:
        print(f"Filled {n_interpolated} dropped-sample gap(s) with linear interpolation")

    window = calibration_window(rows, args.calibration_seconds)
    bx, by, bz, n_cal = compute_gravity_baseline(window)
    print(f"Calibration window: first {n_cal} samples ({args.calibration_seconds}s)")
    print(f"  Gravity vector: ({bx:.4f}, {by:.4f}, {bz:.4f}) g, "
          f"|gravity| = {math.sqrt(bx*bx + by*by + bz*bz):.4f} g")

    if args.no_deadzone:
        deadzone_g = 0.0
        print("  Deadzone: disabled (--no-deadzone)")
    else:
        deadzone_g, noise_mean, noise_std = compute_deadzone_threshold(window, bx, by, bz, args.deadzone_sigma)
        print(f"  Noise floor: mean={noise_mean:.4f}g, stddev={noise_std:.4f}g")
        print(f"  Deadzone threshold: {deadzone_g:.4f}g ({deadzone_g * G_TO_MS2:.4f} m/s^2), "
              f"mean + {args.deadzone_sigma}*stddev")

    t0 = rows[0]["t_ms"]
    t_s, net_g_raw, net_g_filt, net_ms2_filt, nx, ny, nz = [], [], [], [], [], [], []
    ax_ms2, ay_ms2, az_ms2 = [], [], []  # deadzone-clamped, signed, m/s^2 -- what gets integrated
    vx, vy, vz = [], [], []
    px, py, pz = [], [], []
    clamped_count = 0
    prev_ax_ms2 = prev_ay_ms2 = prev_az_ms2 = 0.0
    cur_vx = cur_vy = cur_vz = 0.0
    cur_px = cur_py = cur_pz = 0.0
    prev_t = 0.0
    for i, r in enumerate(rows):
        cx, cy, cz = r["ax"] - bx, r["ay"] - by, r["az"] - bz
        mag_g = math.sqrt(cx * cx + cy * cy + cz * cz)
        filt_g = 0.0 if mag_g < deadzone_g else mag_g
        if filt_g == 0.0 and mag_g != 0.0:
            clamped_count += 1
        t = (r["t_ms"] - t0) / 1000.0
        # Same deadzone that clamps the net-magnitude series, applied to
        # the signed per-axis values too -- an unclamped noise residual
        # repeated every sample for the whole recording would otherwise
        # integrate into a very real-looking but entirely fake
        # velocity/position drift.
        below = mag_g < deadzone_g
        cur_ax_ms2 = (0.0 if below else cx) * G_TO_MS2
        cur_ay_ms2 = (0.0 if below else cy) * G_TO_MS2
        cur_az_ms2 = (0.0 if below else cz) * G_TO_MS2

        if i > 0:
            dt = t - prev_t
            prev_vx, prev_vy, prev_vz = cur_vx, cur_vy, cur_vz
            cur_vx += (cur_ax_ms2 + prev_ax_ms2) / 2 * dt
            cur_vy += (cur_ay_ms2 + prev_ay_ms2) / 2 * dt
            cur_vz += (cur_az_ms2 + prev_az_ms2) / 2 * dt
            cur_px += (cur_vx + prev_vx) / 2 * dt
            cur_py += (cur_vy + prev_vy) / 2 * dt
            cur_pz += (cur_vz + prev_vz) / 2 * dt

        t_s.append(t)
        net_g_raw.append(mag_g)
        net_g_filt.append(filt_g)
        net_ms2_filt.append(filt_g * G_TO_MS2)
        nx.append(cx)
        ny.append(cy)
        nz.append(cz)
        ax_ms2.append(cur_ax_ms2)
        ay_ms2.append(cur_ay_ms2)
        az_ms2.append(cur_az_ms2)
        vx.append(cur_vx)
        vy.append(cur_vy)
        vz.append(cur_vz)
        px.append(cur_px)
        py.append(cur_py)
        pz.append(cur_pz)

        prev_ax_ms2, prev_ay_ms2, prev_az_ms2 = cur_ax_ms2, cur_ay_ms2, cur_az_ms2
        prev_t = t

    # Forward = FORWARD_SIGN * X. Integration is linear, so negating the
    # already-integrated X series is exact -- no need to re-run the loop.
    af = [FORWARD_SIGN * a for a in ax_ms2]
    af_raw = [FORWARD_SIGN * x * G_TO_MS2 for x in nx]  # pre-deadzone, for reference against af
    vf = [FORWARD_SIGN * v for v in vx]
    pf = [FORWARD_SIGN * p for p in px]

    if not args.no_deadzone:
        print(f"  {clamped_count}/{len(rows)} samples ({100*clamped_count/len(rows):.0f}%) "
              f"clamped to 0 as below-deadzone noise")
    print(f"  Final forward speed: {vf[-1]:.3f} m/s, forward distance: {pf[-1]:.3f} m -- "
          "double-integrated from acceleration, drift-prone; treat as a rough estimate")

    base, _ = os.path.splitext(args.csv_path)
    out_png = args.output or f"{base}_accel.png"
    out_csv = args.csv_output or f"{base}_accel.csv"

    with open(out_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", "interpolated", "accel_net_g_filtered", "accel_net_ms2_filtered",
                          "accel_net_g_raw", "accel_x_net_g", "accel_y_net_g", "accel_z_net_g",
                          "vel_x_ms", "vel_y_ms", "vel_z_ms", "pos_x_m", "pos_y_m", "pos_z_m",
                          "accel_forward_ms2", "vel_forward_ms", "pos_forward_m"])
        for i in range(len(t_s)):
            writer.writerow([f"{t_s[i]:.3f}", int(rows[i]["interpolated"]), f"{net_g_filt[i]:.4f}",
                              f"{net_ms2_filt[i]:.4f}", f"{net_g_raw[i]:.4f}", f"{nx[i]:.4f}",
                              f"{ny[i]:.4f}", f"{nz[i]:.4f}", f"{vx[i]:.4f}", f"{vy[i]:.4f}",
                              f"{vz[i]:.4f}", f"{px[i]:.4f}", f"{py[i]:.4f}", f"{pz[i]:.4f}",
                              f"{af[i]:.4f}", f"{vf[i]:.4f}", f"{pf[i]:.4f}"])
    print(f"Wrote computed acceleration/velocity/position CSV: {out_csv}")

    fig, (ax1, ax2, ax3, ax4, ax5, ax6) = plt.subplots(6, 1, figsize=(10, 19), sharex=True)

    if not args.no_deadzone:
        ax1.plot(t_s, af_raw, color="lightgray", linewidth=1, label="raw (pre-deadzone)")
    ax1.plot(t_s, af, color="tab:red", label="filtered")
    ax1.axhline(0, color="black", linewidth=0.6)
    ax1.set_ylabel("Forward acceleration (m/s^2)")
    ax1.set_title("Gravity-compensated, deadzone-filtered acceleration -- signed, along the car's "
                  "actual direction of travel (+X)")
    ax1.axvspan(0, args.calibration_seconds, color="gray", alpha=0.15,
                label=f"calibration window ({args.calibration_seconds}s)")
    ax1.legend(loc="upper right", fontsize=8)
    ax1.grid(True, alpha=0.3)

    ax2.plot(t_s, nx, label="x", alpha=0.8)
    ax2.plot(t_s, ny, label="y", alpha=0.8)
    ax2.plot(t_s, nz, label="z", alpha=0.8)
    ax2.set_ylabel("Compensated accel (g)")
    ax2.set_title("Per-axis compensated acceleration, unfiltered (identify direction of travel)")
    ax2.legend(loc="upper right")
    ax2.grid(True, alpha=0.3)

    ax3.plot(t_s, vf, color="tab:blue", label="forward speed (m/s)")
    ax3b = ax3.twinx()
    ax3b.plot(t_s, pf, color="tab:green", label="forward distance (m)")
    ax3.set_ylabel("Speed (m/s)", color="tab:blue")
    ax3b.set_ylabel("Distance (m)", color="tab:green")
    ax3.set_title("Forward speed & distance (+X axis -- the car's direction of travel on this mount)")
    ax3.grid(True, alpha=0.3)
    lines1, labels1 = ax3.get_legend_handles_labels()
    lines2, labels2 = ax3b.get_legend_handles_labels()
    ax3.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8)

    ax4.plot(t_s, vx, label="x", alpha=0.8)
    ax4.plot(t_s, vy, label="y", alpha=0.8)
    ax4.plot(t_s, vz, label="z", alpha=0.8)
    ax4.set_ylabel("Velocity (m/s)")
    ax4.set_title("Per-axis velocity, reference (trapezoidal integration of deadzone-clamped acceleration)")
    ax4.legend(loc="upper right")
    ax4.grid(True, alpha=0.3)

    ax5.plot(t_s, px, label="x", alpha=0.8)
    ax5.plot(t_s, py, label="y", alpha=0.8)
    ax5.plot(t_s, pz, label="z", alpha=0.8)
    ax5.set_ylabel("Position (m)")
    ax5.set_title("Per-axis position, reference (double integration -- drift accumulates, most trustworthy early in the run)")
    ax5.legend(loc="upper right")
    ax5.grid(True, alpha=0.3)

    if not args.no_deadzone:
        ax6.plot(t_s, [g * G_TO_MS2 for g in net_g_raw], color="lightgray", linewidth=1,
                 label="raw (pre-deadzone)")
        ax6.axhline(deadzone_g * G_TO_MS2, color="tab:orange", linestyle="--", linewidth=1,
                    label=f"deadzone threshold ({args.deadzone_sigma}σ)")
    ax6.plot(t_s, net_ms2_filt, color="tab:red", label="filtered")
    ax6.set_ylabel("Net accel (m/s^2)")
    ax6.set_xlabel("Time (s)")
    ax6.set_title("Net acceleration magnitude, reference (always >= 0 -- motion in any direction, not just forward)")
    ax6.legend(loc="upper right", fontsize=8)
    ax6.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    print(f"Saved plot: {out_png}")

    if not args.no_show:
        plt.show()


if __name__ == "__main__":
    main()
