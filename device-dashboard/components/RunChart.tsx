"use client";

import { useEffect, useRef, useState } from "react";
import {
  Chart as ChartJS,
  LinearScale,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";
import { Line } from "react-chartjs-2";
import type { ImuRow } from "@/lib/csv";
import { computeMotion, type MotionSample, type ProvidedCalibration } from "@/lib/motion";

ChartJS.register(LinearScale, PointElement, LineElement, Tooltip, Legend, Filler);

// react-chartjs-2's own ref type (ChartJSOrUndefined) isn't part of its
// public exports map, so this mirrors it locally rather than reaching into
// dist/types directly.
type LineChartRef = ChartJS<"line"> | undefined;

// Triggers a browser download for a data: URL or blob: URL without
// navigating away -- a plain temporary <a download> is the simplest thing
// that reliably works across browsers for a client-generated file (a
// chart's PNG data URL here; the CSV download uses a real server URL with
// Content-Disposition instead, see the "Download CSV" link below).
function downloadDataUrl(dataUrl: string, filename: string) {
  const a = document.createElement("a");
  a.href = dataUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

const AXIS_COLORS = {
  x: "#e0724a",
  y: "#3fa66c",
  z: "#2a78d6",
};

function SingleLineChart({
  samples,
  pick,
  yLabel,
  color,
  fill,
  rawPick,
  chartRef,
}: {
  samples: MotionSample[];
  pick: (s: MotionSample) => number;
  yLabel: string;
  color: string;
  fill?: boolean;
  rawPick?: (s: MotionSample) => number;
  chartRef?: React.Ref<LineChartRef>;
}) {
  return (
    <div className="relative h-56 w-full">
      <Line
        ref={chartRef}
        data={{
          datasets: [
            ...(rawPick
              ? [
                  {
                    label: "raw (pre-deadzone)",
                    data: samples.map((s) => ({ x: s.timeS, y: rawPick(s) })),
                    borderColor: "#c4c4c4",
                    borderWidth: 1,
                    pointRadius: 0,
                    tension: 0.15,
                  },
                ]
              : []),
            {
              label: "filtered",
              data: samples.map((s) => ({ x: s.timeS, y: pick(s) })),
              borderColor: color,
              backgroundColor: color + "14",
              borderWidth: 2,
              pointRadius: 0,
              tension: 0.15,
              fill: !!fill,
            },
          ],
        }}
        options={{
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: !!rawPick, position: "top", labels: { boxWidth: 12 } } },
          scales: {
            x: { type: "linear", title: { display: true, text: "Time (s)" } },
            y: { title: { display: true, text: yLabel } },
          },
        }}
      />
    </div>
  );
}

function AxisChart({
  samples,
  pick,
  yLabel,
}: {
  samples: MotionSample[];
  pick: (s: MotionSample) => [number, number, number];
  yLabel: string;
}) {
  return (
    <div className="relative h-56 w-full">
      <Line
        data={{
          datasets: (["x", "y", "z"] as const).map((axis, i) => ({
            label: axis === "x" ? "X (forward is +X)" : axis.toUpperCase(),
            data: samples.map((s) => ({ x: s.timeS, y: pick(s)[i] })),
            borderColor: AXIS_COLORS[axis],
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.15,
          })),
        }}
        options={{
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: true, position: "top", labels: { boxWidth: 12 } } },
          scales: {
            x: { type: "linear", title: { display: true, text: "Time (s)" } },
            y: { title: { display: true, text: yLabel } },
          },
        }}
      />
    </div>
  );
}

export default function RunChart({ deviceKey, runId }: { deviceKey: string; runId: number }) {
  const [rows, setRows] = useState<ImuRow[] | null>(null);
  const [calibration, setCalibration] = useState<ProvidedCalibration | null>(null);
  const [error, setError] = useState<string | null>(null);
  const accelChartRef = useRef<LineChartRef>(null);
  const speedChartRef = useRef<LineChartRef>(null);
  const distanceChartRef = useRef<LineChartRef>(null);

  function downloadGraphs() {
    const charts: [LineChartRef | null, string][] = [
      [accelChartRef.current, "forward_acceleration"],
      [speedChartRef.current, "forward_speed"],
      [distanceChartRef.current, "distance_traveled"],
    ];
    for (const [chart, name] of charts) {
      if (chart) {
        downloadDataUrl(chart.toBase64Image(), `${deviceKey}_run${runId}_${name}.png`);
      }
    }
  }

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setCalibration(null);
    setError(null);
    fetch(`/api/runs/${deviceKey}/${runId}`)
      .then((res) => {
        if (!res.ok) throw new Error(`request failed (${res.status})`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled) {
          setRows(data.rows);
          setCalibration(data.calibration ?? null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [deviceKey, runId]);

  if (error) {
    return <p className="text-sm text-red-600 dark:text-red-400">Failed to load run: {error}</p>;
  }
  if (!rows) {
    return <p className="text-sm text-zinc-500">Loading…</p>;
  }

  const samples = computeMotion(rows, undefined, undefined, calibration);
  const interpolatedCount = samples.filter((s) => s.interpolated).length;
  const interpolatedNote =
    interpolatedCount > 0
      ? ` ${interpolatedCount} of ${samples.length} points were interpolated to fill dropped samples.`
      : "";

  return (
    <div className="flex w-full flex-col gap-6">
      <div className="flex flex-wrap gap-2">
        <a
          href={`/api/runs/${deviceKey}/${runId}?format=csv`}
          download
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-xs font-medium text-black hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-50 dark:hover:bg-zinc-900"
        >
          Download CSV
        </a>
        <button
          type="button"
          onClick={downloadGraphs}
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-xs font-medium text-black hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-50 dark:hover:bg-zinc-900"
        >
          Download graphs (PNG)
        </button>
      </div>

      <div>
        <h3 className="mb-1 text-sm font-medium text-black dark:text-zinc-50">
          Forward acceleration <span className="font-normal text-zinc-500">(+X axis)</span>
        </h3>
        <SingleLineChart
          chartRef={accelChartRef}
          samples={samples}
          pick={(s) => s.accelForwardMs2}
          rawPick={(s) => s.accelForwardRawMs2}
          yLabel="m/s²"
          color="#2a78d6"
          fill
        />
        <p className="mt-1 text-xs text-zinc-500">
          Signed acceleration along the car&apos;s actual direction of travel -- positive speeds it up,
          negative slows it down or reverses. This replaced a magnitude-only view (always ≥ 0, so it
          couldn&apos;t show deceleration); that view is still available below for reference.
        </p>
      </div>

      <div>
        <h3 className="mb-1 text-sm font-medium text-black dark:text-zinc-50">
          Forward speed <span className="font-normal text-zinc-500">(+X axis)</span>
        </h3>
        <SingleLineChart
          chartRef={speedChartRef}
          samples={samples}
          pick={(s) => s.velForwardMs}
          yLabel="m/s"
          color="#2a78d6"
          fill
        />
      </div>

      <div>
        <h3 className="mb-1 text-sm font-medium text-black dark:text-zinc-50">
          Distance traveled <span className="font-normal text-zinc-500">(+X axis)</span>
        </h3>
        <SingleLineChart
          chartRef={distanceChartRef}
          samples={samples}
          pick={(s) => s.posForwardM}
          yLabel="m"
          color="#3fa66c"
          fill
        />
        <p className="mt-1 text-xs text-zinc-500">
          Integrated from acceleration twice over -- drift accumulates the longer the car moves, so
          treat this as a rough estimate rather than a precise track, especially later in the run.
          {interpolatedNote}
        </p>
      </div>

      <details className="group">
        <summary className="cursor-pointer text-sm font-medium text-black dark:text-zinc-50">
          Per-axis breakdown (reference)
        </summary>
        <div className="mt-3 flex flex-col gap-6">
          <div>
            <h4 className="mb-1 text-xs font-medium text-zinc-500">
              Net acceleration magnitude (always ≥ 0 -- motion in any direction, not just forward)
            </h4>
            <div className="relative h-56 w-full">
              <Line
                data={{
                  datasets: [
                    {
                      label: "Net acceleration",
                      data: samples.map((s) => ({ x: s.timeS, y: s.accelNetMs2 })),
                      borderColor: "#2a78d6",
                      backgroundColor: "rgba(42,120,214,0.08)",
                      borderWidth: 2,
                      pointRadius: 0,
                      tension: 0.15,
                      fill: true,
                    },
                  ],
                }}
                options={{
                  responsive: true,
                  maintainAspectRatio: false,
                  plugins: { legend: { display: false } },
                  scales: {
                    x: { type: "linear", title: { display: true, text: "Time (s)" } },
                    y: { title: { display: true, text: "m/s²" } },
                  },
                }}
              />
            </div>
          </div>
          <div>
            <h4 className="mb-1 text-xs font-medium text-zinc-500">Velocity, all axes</h4>
            <AxisChart samples={samples} pick={(s) => [s.velXMs, s.velYMs, s.velZMs]} yLabel="m/s" />
          </div>
          <div>
            <h4 className="mb-1 text-xs font-medium text-zinc-500">Position, all axes</h4>
            <AxisChart samples={samples} pick={(s) => [s.posXM, s.posYM, s.posZM]} yLabel="m" />
          </div>
        </div>
      </details>
    </div>
  );
}
