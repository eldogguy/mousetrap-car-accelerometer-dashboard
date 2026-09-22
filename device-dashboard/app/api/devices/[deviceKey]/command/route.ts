import { prisma } from "@/lib/prisma";

const DEVICE_KEY_RE = /^[0-9a-f]{5}$/i;
const VALID_COMMANDS = new Set(["arm", "stop", "reset", "export"]);

// Called by the browser to queue a remote command. The device only picks
// this up the next time it polls -- every few seconds while idle, or every
// RECORDING_POLL_INTERVAL_MS while actively recording ("stop" only makes
// sense during that window) -- so there's inherent latency; this just
// queues it, it doesn't confirm execution.
export async function POST(
  request: Request,
  { params }: { params: Promise<{ deviceKey: string }> }
) {
  const { deviceKey: rawKey } = await params;
  const deviceKey = rawKey.toLowerCase();
  if (!DEVICE_KEY_RE.test(deviceKey)) {
    return Response.json({ error: "bad device key" }, { status: 400 });
  }

  let command: unknown;
  try {
    ({ command } = await request.json());
  } catch {
    return Response.json({ error: "invalid body" }, { status: 400 });
  }
  if (typeof command !== "string" || !VALID_COMMANDS.has(command)) {
    return Response.json({ error: "command must be one of: arm, reset, export" }, { status: 400 });
  }

  await prisma.device.upsert({
    where: { key: deviceKey },
    update: { pendingCommand: command },
    create: { key: deviceKey, pendingCommand: command },
  });

  return Response.json({ ok: true });
}

// Lets the device page poll for the device's last-reported state without a
// full page refresh.
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ deviceKey: string }> }
) {
  const { deviceKey: rawKey } = await params;
  const deviceKey = rawKey.toLowerCase();
  if (!DEVICE_KEY_RE.test(deviceKey)) {
    return Response.json({ error: "bad device key" }, { status: 400 });
  }

  const device = await prisma.device.findUnique({
    where: { key: deviceKey },
    select: { lastKnownState: true, lastSeenAt: true },
  });

  return Response.json({
    lastKnownState: device?.lastKnownState ?? null,
    lastSeenAt: device?.lastSeenAt?.toISOString() ?? null,
  });
}
