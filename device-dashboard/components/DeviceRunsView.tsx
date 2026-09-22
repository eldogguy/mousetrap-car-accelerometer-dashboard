"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import RunPicker, { type RunSummary } from "@/components/RunPicker";
import RunChart from "@/components/RunChart";

export default function DeviceRunsView({
  deviceKey,
  runs,
}: {
  deviceKey: string;
  runs: RunSummary[];
}) {
  const router = useRouter();
  const [selectedRunId, setSelectedRunId] = useState(runs[0].id);
  const [clearing, setClearing] = useState(false);
  const latestSeenIdRef = useRef(runs[0].id);

  // When the parent server component refetches (e.g. after RemoteControl
  // sees the device go back to idle) and a newer run has appeared, jump
  // the chart to it automatically instead of leaving the old selection.
  useEffect(() => {
    if (runs[0] && runs[0].id !== latestSeenIdRef.current) {
      latestSeenIdRef.current = runs[0].id;
      setSelectedRunId(runs[0].id);
    }
  }, [runs]);

  async function clearAllRuns() {
    // Irreversible (no trash/undo -- see the DELETE route), so confirm
    // before actually calling it rather than after.
    if (!window.confirm(`Delete all ${runs.length} run(s) for ${deviceKey}? This can't be undone.`)) {
      return;
    }
    setClearing(true);
    try {
      const res = await fetch(`/api/runs/${deviceKey}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      router.refresh(); // re-runs the server component; DevicePage swaps to its "no runs yet" state
    } catch (err) {
      window.alert(`Failed to clear runs: ${err}`);
    } finally {
      setClearing(false);
    }
  }

  return (
    <div className="flex w-full flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <RunPicker runs={runs} selectedRunId={selectedRunId} onSelect={setSelectedRunId} />
        <button
          type="button"
          onClick={clearAllRuns}
          disabled={clearing}
          className="rounded-md border border-red-300 px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-50 disabled:opacity-50 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950"
        >
          {clearing ? "Clearing…" : "Clear all runs"}
        </button>
      </div>
      <RunChart deviceKey={deviceKey} runId={selectedRunId} />
    </div>
  );
}
