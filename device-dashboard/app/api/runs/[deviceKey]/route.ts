import { prisma } from "@/lib/prisma";

const DEVICE_KEY_RE = /^[0-9a-f]{5}$/i;
// Firmware's MAX_RECORD_SECONDS is 120s at 50Hz = 6000 samples
// (config.py). Each CSV row is ~80 bytes worst-case (14 comma-separated
// numeric fields, see main.py's format_csv_row), so a full recording is
// ~480KB -- this constant needs to scale with that, not stay fixed. An
// earlier revision left this at 512KB after the sample cap was raised from
// 1000 to 6000 samples, leaving almost no real margin over that new
// worst case (some devices hit 413 immediately). 2MB gives a real, durable
// safety margin rather than a tight one that breaks again the next time
// MAX_RECORD_SECONDS changes.
const MAX_CSV_BYTES = 2 * 1024 * 1024;
const MAX_RUNS_PER_DEVICE = 20;

export async function POST(
  request: Request,
  { params }: { params: Promise<{ deviceKey: string }> }
) {
  const { deviceKey: rawKey } = await params;
  const deviceKey = rawKey.toLowerCase();

  if (!DEVICE_KEY_RE.test(deviceKey)) {
    return new Response("bad device key\n", { status: 400 });
  }

  const csv = await request.text();
  if (csv.length === 0) {
    return new Response("empty body\n", { status: 400 });
  }
  if (csv.length > MAX_CSV_BYTES) {
    return new Response("payload too large\n", { status: 413 });
  }

  const sampleCount = Math.max(0, csv.trim().split("\n").length - 1); // minus header

  try {
    await prisma.$transaction(async (tx) => {
      await tx.device.upsert({
        where: { key: deviceKey },
        update: {},
        create: { key: deviceKey },
      });
      await tx.run.create({
        data: { deviceKey, csvRaw: csv, sampleCount },
      });

      const staleRuns = await tx.run.findMany({
        where: { deviceKey },
        orderBy: { uploadedAt: "desc" },
        skip: MAX_RUNS_PER_DEVICE,
        select: { id: true },
      });
      if (staleRuns.length > 0) {
        await tx.run.deleteMany({
          where: { id: { in: staleRuns.map((r) => r.id) } },
        });
      }
    });
  } catch (err) {
    console.error("[upload] failed", err);
    return new Response("server error\n", { status: 500 });
  }

  return new Response("OK\n", { status: 200 });
}

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ deviceKey: string }> }
) {
  const { deviceKey: rawKey } = await params;
  const deviceKey = rawKey.toLowerCase();
  if (!DEVICE_KEY_RE.test(deviceKey)) {
    return Response.json({ error: "bad device key" }, { status: 400 });
  }

  const runs = await prisma.run.findMany({
    where: { deviceKey },
    orderBy: { uploadedAt: "desc" },
    take: MAX_RUNS_PER_DEVICE,
    select: { id: true, uploadedAt: true, sampleCount: true },
  });
  return Response.json({ deviceKey, runs });
}

// Clears every stored run for a device -- used by the dashboard's "Clear
// all runs" button. Irreversible (no soft-delete/trash), so the UI is
// responsible for confirming with the user before calling this.
export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ deviceKey: string }> }
) {
  const { deviceKey: rawKey } = await params;
  const deviceKey = rawKey.toLowerCase();
  if (!DEVICE_KEY_RE.test(deviceKey)) {
    return Response.json({ error: "bad device key" }, { status: 400 });
  }

  const { count } = await prisma.run.deleteMany({ where: { deviceKey } });
  return Response.json({ deviceKey, deleted: count });
}
