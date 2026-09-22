"use client";

export interface RunSummary {
  id: number;
  uploadedAt: string;
  sampleCount: number;
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export default function RunPicker({
  runs,
  selectedRunId,
  onSelect,
}: {
  runs: RunSummary[];
  selectedRunId: number;
  onSelect: (runId: number) => void;
}) {
  return (
    <select
      value={selectedRunId}
      onChange={(e) => onSelect(Number(e.target.value))}
      className="rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-black dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
    >
      {runs.map((run) => (
        <option key={run.id} value={run.id}>
          {formatTime(run.uploadedAt)} · {run.sampleCount} samples
        </option>
      ))}
    </select>
  );
}
