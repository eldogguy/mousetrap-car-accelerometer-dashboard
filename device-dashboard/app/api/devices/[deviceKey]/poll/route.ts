import { prisma } from "@/lib/prisma";

const DEVICE_KEY_RE = /^[0-9a-f]{5}$/i;
const VALID_STATES = new Set([
  "idle",
  "arm_delay",
  "calibrating",
  "recording",
]);

// Called by the ESP32 itself, periodically, only while it's idle (WiFi is
// off the rest of the time to avoid disturbing the sampling loop). Reports
// the device's current state and atomically fetches + clears any pending
// remote command in one request, so the device never has to poll twice.
export async function POST(
  request: Request,
  { params }: { params: Promise<{ deviceKey: string }> }
) {
  const { deviceKey: rawKey } = await params;
  const deviceKey = rawKey.toLowerCase();
  if (!DEVICE_KEY_RE.test(deviceKey)) {
    return Response.json({ error: "bad device key" }, { status: 400 });
  }

  let state: string | undefined;
  try {
    const body = await request.json();
    if (typeof body?.state === "string" && VALID_STATES.has(body.state)) {
      state = body.state;
    }
  } catch {
    // no/invalid body -- fine, state reporting is optional
  }

  const command = await prisma.$transaction(async (tx) => {
    const device = await tx.device.upsert({
      where: { key: deviceKey },
      update: { lastKnownState: state },
      create: { key: deviceKey, lastKnownState: state },
    });
    if (!device.pendingCommand) return null;
    await tx.device.update({
      where: { key: deviceKey },
      data: { pendingCommand: null },
    });
    return device.pendingCommand;
  });

  return Response.json({ command });
}
