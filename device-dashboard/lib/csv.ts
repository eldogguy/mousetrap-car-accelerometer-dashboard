export interface ImuRow {
  sampleIndex: number;
  timestampMs: number;
  gyroX: number;
  gyroY: number;
  gyroZ: number;
  accelX: number;
  accelY: number;
  accelZ: number;
  magX: number;
  magY: number;
  magZ: number;
  roll: number;
  pitch: number;
  yaw: number;
}

// Matches the fixed 14-column header produced by format_csv_row() in
// firmware/esp32-9dof-imu-micropython/main.py. No general CSV library
// needed since this app controls both producer and consumer.
export function parseImuCsv(csv: string): ImuRow[] {
  const lines = csv.trim().split("\n");
  return lines.slice(1).map((line) => {
    const c = line.split(",").map(Number);
    return {
      sampleIndex: c[0],
      timestampMs: c[1],
      gyroX: c[2],
      gyroY: c[3],
      gyroZ: c[4],
      accelX: c[5],
      accelY: c[6],
      accelZ: c[7],
      magX: c[8],
      magY: c[9],
      magZ: c[10],
      roll: c[11],
      pitch: c[12],
      yaw: c[13],
    };
  });
}
