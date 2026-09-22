"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

const DEVICE_KEY_RE = /^[0-9a-f]{5}$/i;

export default function Home() {
  const router = useRouter();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = key.trim();
    if (!DEVICE_KEY_RE.test(trimmed)) {
      setError("Enter the 5-character code shown on your device's screen.");
      return;
    }
    router.push(`/device/${trimmed.toLowerCase()}`);
  }

  return (
    <div className="flex flex-1 flex-col items-center justify-center bg-zinc-50 font-sans dark:bg-black">
      <main className="flex w-full max-w-sm flex-col items-center gap-6 px-6">
        <div className="flex flex-col items-center gap-2 text-center">
          <h1 className="text-2xl font-semibold tracking-tight text-black dark:text-zinc-50">
            Sensor run viewer
          </h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Enter the code shown on your device&apos;s screen to see its runs.
          </p>
        </div>
        <form onSubmit={handleSubmit} className="flex w-full flex-col gap-3">
          <input
            value={key}
            onChange={(e) => {
              setKey(e.target.value);
              setError(null);
            }}
            placeholder="a1b2c"
            maxLength={5}
            autoFocus
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            className="w-full rounded-md border border-zinc-300 bg-white px-4 py-3 text-center font-mono text-2xl tracking-[0.3em] uppercase text-black outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
          />
          {error && <p className="text-center text-sm text-red-600 dark:text-red-400">{error}</p>}
          <button
            type="submit"
            className="w-full rounded-md bg-black px-5 py-3 text-sm font-medium text-white transition-colors hover:bg-zinc-800 dark:bg-white dark:text-black dark:hover:bg-zinc-200"
          >
            View runs
          </button>
        </form>
      </main>
    </div>
  );
}
