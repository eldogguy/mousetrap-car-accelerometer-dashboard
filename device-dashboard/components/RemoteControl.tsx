"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

const STATE_LABELS: Record<string, string> = {
  idle: "Idle",
  arm_delay: "Placing…",
  calibrating: "Calibrating…",
  recording: "Recording",
};

function timeAgo(iso: string | null) {
  if (!iso) return null;
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

export default function RemoteControl({
  deviceKey,
  lastKnownState: initialState,
  lastSeenAt: initialSeenAt,
}: {
  deviceKey: string;
  lastKnownState: string | null;
  lastSeenAt: string | null;
}) {
  const router = useRouter();
  const [lastKnownState, setLastKnownState] = useState(initialState);
  const [lastSeenAt, setLastSeenAt] = useState(initialSeenAt);
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const prevStateRef = useRef(initialState);

  // Poll for the device's live state. It checks in with the server while
  // idle (every few seconds) AND while recording (so a remote Stop can
  // reach it) -- but NOT during the placement/calibration window, so this
  // display goes stale during that stretch, which is expected. The device
  // auto-exports + auto-resets once it disarms, so the moment we see it
  // transition back to idle, refresh the page to pull in the new run.
  useEffect(() => {
    const interval = setInterval(() => {
      fetch(`/api/devices/${deviceKey}/command`)
        .then((res) => res.json())
        .then((data) => {
          const newState: string | null = data.lastKnownState;
          setLastKnownState(newState);
          setLastSeenAt(data.lastSeenAt);
          if (newState === "idle" && prevStateRef.current && prevStateRef.current !== "idle") {
            router.refresh();
          }
          prevStateRef.current = newState;
        })
        .catch(() => {});
    }, 3000);
    return () => clearInterval(interval);
  }, [deviceKey, router]);

  async function sendCommand(command: "arm" | "stop") {
    setSending(true);
    setMessage(null);
    try {
      const res = await fetch(`/api/devices/${deviceKey}/command`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command }),
      });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      setMessage(
        command === "arm"
          ? "Queued — starts next time the device checks in (every few seconds)."
          : "Queued — stops next time the device checks in (about once a second while recording)."
      );
    } catch (err) {
      setMessage(`Failed to send: ${err}`);
    } finally {
      setSending(false);
    }
  }

  const isIdle = lastKnownState === null || lastKnownState === "idle";
  const isRecording = lastKnownState === "recording";
  const canInteract = isIdle || isRecording;
  const seenAgo = timeAgo(lastSeenAt);
  const stateLabel = lastKnownState ? STATE_LABELS[lastKnownState] ?? lastKnownState : null;

  let hint: string;
  if (isIdle) {
    hint = message ?? "Runs the placement + calibration routine automatically, then records.";
  } else if (isRecording) {
    hint = message ?? "Recording — press Stop to end it now, or let it finish on its own.";
  } else {
    hint = "Placing / calibrating — can't be interrupted remotely during this part.";
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-black dark:text-zinc-50">Remote control</span>
        <span className="text-xs text-zinc-500">
          {stateLabel ? `${stateLabel}${seenAgo ? ` · seen ${seenAgo}` : ""}` : "Not seen yet"}
        </span>
      </div>
      <button
        onClick={() => sendCommand(isIdle ? "arm" : "stop")}
        disabled={sending || !canInteract}
        className="w-full rounded-md bg-black px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-800 disabled:opacity-50 dark:bg-white dark:text-black dark:hover:bg-zinc-200"
      >
        {isIdle ? "Start" : "Stop"}
      </button>
      <p className="text-xs text-zinc-500">{hint}</p>
    </div>
  );
}
