import { prisma } from "@/lib/prisma";
import { parseImuCsv } from "@/lib/csv";

const DEVICE_KEY_RE = /^[0-9a-f]{5}$/i;

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

  // ?format=csv streams back the exact bytes the device uploaded (not a
  // re-serialization of the parsed JSON below) -- used by the dashboard's
  // "Download CSV" button/link.
  const format = new URL(request.url).searchParams.get("format");
  if (format === "csv") {
    return new Response(run.csvRaw, {
      headers: {
        "Content-Type": "text/csv",
        "Content-Disposition": `attachment; filename="${deviceKey}_run${id}.csv"`,
      },
    });
  }

  return Response.json({
    id: run.id,
    uploadedAt: run.uploadedAt,
    rows: parseImuCsv(run.csvRaw),
  });
}
