import Link from "next/link";
import { prisma } from "@/lib/prisma";
import DeviceRunsView from "@/components/DeviceRunsView";
import RemoteControl from "@/components/RemoteControl";

const DEVICE_KEY_RE = /^[0-9a-f]{5}$/i;

export default async function DevicePage({
  params,
}: {
  params: Promise<{ key: string }>;
}) {
  const { key: rawKey } = await params;
  const deviceKey = rawKey.toLowerCase();

  if (!DEVICE_KEY_RE.test(deviceKey)) {
    return (
      <Page>
        <p className="text-sm text-red-600 dark:text-red-400">
          &ldquo;{rawKey}&rdquo; isn&apos;t a valid device code (should be 5 characters).
        </p>
      </Page>
    );
  }

  const [device, runs] = await Promise.all([
    prisma.device.findUnique({
      where: { key: deviceKey },
      select: { lastKnownState: true, lastSeenAt: true },
    }),
    prisma.run.findMany({
      where: { deviceKey },
      orderBy: { uploadedAt: "desc" },
      take: 20,
      select: { id: true, uploadedAt: true, sampleCount: true },
    }),
  ]);

  return (
    <Page deviceKey={deviceKey}>
      <RemoteControl
        deviceKey={deviceKey}
        lastKnownState={device?.lastKnownState ?? null}
        lastSeenAt={device?.lastSeenAt?.toISOString() ?? null}
      />
      {runs.length === 0 ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          No runs yet for <span className="font-mono">{deviceKey}</span>. Check the code on the
          device&apos;s screen, then try again after pressing (or remotely triggering) Export.
        </p>
      ) : (
        <DeviceRunsView
          deviceKey={deviceKey}
          runs={runs.map((r) => ({ ...r, uploadedAt: r.uploadedAt.toISOString() }))}
        />
      )}
    </Page>
  );
}

function Page({ deviceKey, children }: { deviceKey?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-1 flex-col items-center bg-zinc-50 px-6 py-12 font-sans dark:bg-black">
      <main className="flex w-full max-w-2xl flex-col gap-6">
        <div className="flex items-center justify-between">
          <h1 className="font-mono text-xl font-semibold text-black dark:text-zinc-50">
            {deviceKey ?? "device"}
          </h1>
          <Link href="/" className="text-sm text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200">
            Change device
          </Link>
        </div>
        {children}
      </main>
    </div>
  );
}
