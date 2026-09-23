import { prisma } from "@/lib/prisma";
import { parseImuCsv, motionToCsv } from "@/lib/csv";
import { computeMotion, type ProvidedCalibration } from "@/lib/motion";
import type { Run } from "@/app/generated/prisma/client";

const DEVICE_KEY_RE = /^[0-9a-f]{5}$/i;

// null (not undefined) when the device's calibration headers were
// missing/malformed on upload -- computeMotion() then falls back to
// re-deriving a baseline from the recording's own first few seconds.
function runCalibration(run: Run): ProvidedCalibration | null {
  if (run.calibGravityX == null || run.calibGravityY == null || run.calibGravityZ == null || run.calibDeadzoneG == null) {
    return null;
  }
  return {
    gravity: [run.calibGravityX, run.calibGravityY, run.calibGravityZ],
    deadzoneG: run.calibDeadzoneG,
  };
}

export async function GET(
  request: Request,
  { params }: { params: Promise<{ deviceKey: string; runId: string }> }
) {
  const { deviceKey: rawKey, runId } = await params;
  const deviceKey = rawKey.toLowerCase();
  const id = Number(runId);

  if (!DEVICE_KEY_RE.test(deviceKey) || !Number.isInteger(id)) {
    return Response.json({ error: "bad request" }, { status: 400 });
  }

  const run = await prisma.run.findFirst({ where: { id, deviceKey } });
  if (!run) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  const calibration = runCalibration(run);

  // ?format=csv returns the computed acceleration/velocity/displacement
  // vs. time series (same numbers the dashboard's three main charts plot),
  // not the raw per-axis device CSV -- used by the "Download CSV" link.
  const format = new URL(request.url).searchParams.get("format");
  if (format === "csv") {
    const motionCsv = motionToCsv(computeMotion(parseImuCsv(run.csvRaw), undefined, undefined, calibration));
    return new Response(motionCsv, {
      headers: {
        "Content-Type": "text/csv",
        "Content-Disposition": `attachment; filename="${deviceKey}_run${id}_motion.csv"`,
      },
    });
  }

  return Response.json({
    id: run.id,
    uploadedAt: run.uploadedAt,
    rows: parseImuCsv(run.csvRaw),
    calibration,
  });
}
