import type { ImuRow } from "@/lib/csv";

const G_TO_MS2 = 9.80665;

// The car's direction of travel on this mount is the positive X axis (per
// the classroom rig's mounting orientation) -- forward = +X. Flipped from
// an earlier -X assumption once real runs showed the sign backwards
// (forward motion read as deceleration/reverse). If a car ever gets
// mounted the other way around again, this is the one place to flip.
const FORWARD_SIGN = 1;

export interface MotionSample {
  timeS: number;
  interpolated: boolean; // true if this row was synthesized to fill a dropped-sample gap, not measured
  accelNetMs2: number; // gravity-compensated, deadzone-filtered net magnitude -- what the accel chart plots
  accelNetGRaw: number; // pre-deadzone magnitude, for reference
  accelXMs2: number; // gravity-compensated, deadzone-clamped, signed -- what velocity/position integrate
  accelYMs2: number;
  accelZMs2: number;
  velXMs: number; // trapezoidal-integrated from accelXMs2 (and Y/Z), m/s
  velYMs: number;
  velZMs: number;
  posXM: number; // trapezoidal-integrated from velocity, m
  posYM: number;
  posZM: number;
  accelForwardMs2: number; // FORWARD_SIGN * X, deadzone-clamped -- the car's actual (signed) acceleration
  accelForwardRawMs2: number; // same, pre-deadzone -- for reference against the clamped series
  velForwardMs: number;
  posForwardM: number;
}

type FilledRow = ImuRow & { interpolated: boolean };

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

// A dropped/duplicate sample on the device (see append_sample's stale-read
// guard in main.py) shows up here as one real timestamp gap roughly 2x the
// nominal sample interval instead of 1x. Left alone, that doubled dt would
// throw off the trapezoidal integration below by a small but real amount --
// filling it with linearly-interpolated row(s) keeps velocity/position on
// an even time grid. The nominal interval is estimated from the data
// itself (median of consecutive deltas) rather than hardcoded, so this
// doesn't need to know the firmware's SAMPLE_RATE_HZ. A gap much larger
// than a few missed samples is treated as something else going on (a real
// hole, not a stale-read blip) and left as-is rather than papered over.
function fillGaps(rows: ImuRow[]): FilledRow[] {
  if (rows.length === 0) return [];

  const deltas: number[] = [];
  for (let i = 1; i < rows.length; i++) deltas.push(rows[i].timestampMs - rows[i - 1].timestampMs);
  const sorted = [...deltas].sort((a, b) => a - b);
  const nominalMs = sorted[Math.floor(sorted.length / 2)] || 1;

  const out: FilledRow[] = [{ ...rows[0], interpolated: false }];
  for (let i = 1; i < rows.length; i++) {
    const prev = rows[i - 1];
    const cur = rows[i];
    const dt = cur.timestampMs - prev.timestampMs;
    const missing = Math.round(dt / nominalMs) - 1;
    if (missing > 0 && missing <= 10) {
      for (let k = 1; k <= missing; k++) {
        const frac = k / (missing + 1);
        out.push({
          sampleIndex: prev.sampleIndex,
          timestampMs: prev.timestampMs + frac * dt,
          gyroX: lerp(prev.gyroX, cur.gyroX, frac),
          gyroY: lerp(prev.gyroY, cur.gyroY, frac),
          gyroZ: lerp(prev.gyroZ, cur.gyroZ, frac),
          accelX: lerp(prev.accelX, cur.accelX, frac),
          accelY: lerp(prev.accelY, cur.accelY, frac),
          accelZ: lerp(prev.accelZ, cur.accelZ, frac),
          magX: lerp(prev.magX, cur.magX, frac),
          magY: lerp(prev.magY, cur.magY, frac),
          magZ: lerp(prev.magZ, cur.magZ, frac),
          roll: lerp(prev.roll, cur.roll, frac),
          pitch: lerp(prev.pitch, cur.pitch, frac),
          yaw: lerp(prev.yaw, cur.yaw, frac),
          interpolated: true,
        });
      }
    }
    out.push({ ...cur, interpolated: false });
  }
  return out;
}

export interface ProvidedCalibration {
  gravity: [number, number, number];
  deadzoneG: number;
}

// Mirrors analysis/analyze_run.py field-for-field: mean accel over the
// first `calibrationSeconds` (by timestamp, not sample count) as the
// gravity baseline; mean + deadzoneSigma*stddev of residual magnitude over
// that same window as the deadzone threshold. Extends it with gap-filling
// and trapezoidal integration to velocity/position -- keep in sync with
// the Python version if either changes.
//
// This first-N-seconds approach is a fallback only -- it assumes the car
// is still sitting still at the start of the exported recording, which is
// true for a slow/bench-triggered run but false for a real launch that
// takes off almost immediately (confirmed on real classroom data: a
// contaminated "calibration window" produced a 0.68g deadzone, big enough
// to zero out nearly the entire real signal). Pass `providedCalibration`
// (the device's own baseline from its real, guaranteed-stationary
// STATE_CALIBRATING window, stored per-run) whenever it's available to
// skip this re-derivation entirely.
export function computeMotion(
  rows: ImuRow[],
  calibrationSeconds = 5.0,
  deadzoneSigma = 3.0,
  providedCalibration?: ProvidedCalibration | null
): MotionSample[] {
  if (rows.length < 2) return [];

  const filled = fillGaps(rows);

  const t0 = filled[0].timestampMs;
  let bx: number, by: number, bz: number, deadzoneG: number;

  if (providedCalibration) {
    [bx, by, bz] = providedCalibration.gravity;
    deadzoneG = providedCalibration.deadzoneG;
  } else {
    const cutoffMs = t0 + calibrationSeconds * 1000;
    const window = filled.filter((r) => r.timestampMs <= cutoffMs);
    const calWindow = window.length > 0 ? window : filled.slice(0, 1);

    const n = calWindow.length;
    bx = calWindow.reduce((s, r) => s + r.accelX, 0) / n;
    by = calWindow.reduce((s, r) => s + r.accelY, 0) / n;
    bz = calWindow.reduce((s, r) => s + r.accelZ, 0) / n;

    const residuals = calWindow.map((r) => {
      const cx = r.accelX - bx;
      const cy = r.accelY - by;
      const cz = r.accelZ - bz;
      return Math.sqrt(cx * cx + cy * cy + cz * cz);
    });
    const meanR = residuals.reduce((a, b) => a + b, 0) / n;
    const varR = residuals.reduce((a, r) => a + (r - meanR) ** 2, 0) / n;
    deadzoneG = meanR + deadzoneSigma * Math.sqrt(varR);
  }

  const out: MotionSample[] = [];
  let vx = 0, vy = 0, vz = 0;
  let px = 0, py = 0, pz = 0;
  let prevAx = 0, prevAy = 0, prevAz = 0;
  let prevT = 0;

  for (let i = 0; i < filled.length; i++) {
    const r = filled[i];
    const cx = r.accelX - bx;
    const cy = r.accelY - by;
    const cz = r.accelZ - bz;
    const magG = Math.sqrt(cx * cx + cy * cy + cz * cz);
    // Same deadzone that clamps the net-magnitude chart, now also applied
    // to the signed per-axis values that feed the integral below -- an
    // unclamped noise residual repeated every sample for 20 seconds would
    // otherwise integrate into a very real-looking but entirely fake
    // velocity/position drift.
    const belowDeadzone = magG < deadzoneG;
    const axMs2 = (belowDeadzone ? 0 : cx) * G_TO_MS2;
    const ayMs2 = (belowDeadzone ? 0 : cy) * G_TO_MS2;
    const azMs2 = (belowDeadzone ? 0 : cz) * G_TO_MS2;
    const filtG = belowDeadzone ? 0 : magG;
    const timeS = (r.timestampMs - t0) / 1000;

    if (i > 0) {
      const dt = timeS - prevT;
      const prevVx = vx, prevVy = vy, prevVz = vz;
      vx += ((axMs2 + prevAx) / 2) * dt;
      vy += ((ayMs2 + prevAy) / 2) * dt;
      vz += ((azMs2 + prevAz) / 2) * dt;
      px += ((vx + prevVx) / 2) * dt;
      py += ((vy + prevVy) / 2) * dt;
      pz += ((vz + prevVz) / 2) * dt;
    }

    out.push({
      timeS,
      interpolated: r.interpolated,
      accelNetMs2: filtG * G_TO_MS2,
      accelNetGRaw: magG,
      accelXMs2: axMs2,
      accelYMs2: ayMs2,
      accelZMs2: azMs2,
      velXMs: vx,
      velYMs: vy,
      velZMs: vz,
      posXM: px,
      posYM: py,
      posZM: pz,
      accelForwardMs2: FORWARD_SIGN * axMs2,
      accelForwardRawMs2: FORWARD_SIGN * cx * G_TO_MS2,
      velForwardMs: FORWARD_SIGN * vx,
      posForwardM: FORWARD_SIGN * px,
    });

    prevAx = axMs2;
    prevAy = ayMs2;
    prevAz = azMs2;
    prevT = timeS;
  }

  return out;
}
