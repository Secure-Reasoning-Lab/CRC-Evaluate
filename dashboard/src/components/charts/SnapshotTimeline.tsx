'use client';

import { PlotlyChart } from './PlotlyChart';

// Simple interface for snapshot data needed for the chart
interface SnapshotPoint {
  cycle: number;
  elapsed_time: number;
  pov_count: number;
  patch_count: number;
}

interface SnapshotTimelineProps {
  data: SnapshotPoint[];
  title?: string;
}

export function SnapshotTimeline({
  data,
  title = 'Snapshot Timeline',
}: SnapshotTimelineProps) {
  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-muted rounded-lg">
        <p className="text-muted-foreground">No snapshots available</p>
      </div>
    );
  }

  const cycles = data.map((d) => d.cycle);
  const times = data.map((d) => d.elapsed_time / 3600); // Convert to hours
  const povCounts = data.map((d) => d.pov_count);
  const patchCounts = data.map((d) => d.patch_count);

  // Calculate cumulative counts using reduce
  const cumulativePovs = povCounts.reduce<number[]>((acc, c) => {
    const prev = acc.length > 0 ? acc[acc.length - 1] : 0;
    acc.push(prev + c);
    return acc;
  }, []);
  const cumulativePatches = patchCounts.reduce<number[]>((acc, c) => {
    const prev = acc.length > 0 ? acc[acc.length - 1] : 0;
    acc.push(prev + c);
    return acc;
  }, []);

  return (
    <PlotlyChart
      data={[
        {
          x: times,
          y: cumulativePovs,
          type: 'scatter',
          mode: 'lines+markers',
          name: 'Cumulative POVs',
          line: { color: '#3b82f6', width: 2, shape: 'hv' },
          marker: { size: 8 },
          text: cycles.map((c) => `Cycle ${c}`),
          hovertemplate: '%{text}<br>Time: %{x:.2f}h<br>POVs: %{y}<extra></extra>',
        },
        {
          x: times,
          y: cumulativePatches,
          type: 'scatter',
          mode: 'lines+markers',
          name: 'Cumulative Patches',
          line: { color: '#10b981', width: 2, shape: 'hv' },
          marker: { size: 8 },
          text: cycles.map((c) => `Cycle ${c}`),
          hovertemplate: '%{text}<br>Time: %{x:.2f}h<br>Patches: %{y}<extra></extra>',
        },
      ]}
      layout={{
        title: { text: title, font: { size: 16 } },
        xaxis: { title: { text: 'Time (hours)' } },
        yaxis: { title: { text: 'Cumulative Count' } },
        legend: { orientation: 'h', y: -0.2 },
        height: 300,
      }}
      className="h-[300px]"
    />
  );
}
