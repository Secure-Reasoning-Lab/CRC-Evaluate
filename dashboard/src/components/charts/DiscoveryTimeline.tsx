'use client';

import type { TimeSeriesPoint } from '@/lib/types';
import { PlotlyChart } from './PlotlyChart';

interface DiscoveryTimelineProps {
  data: TimeSeriesPoint[];
  title?: string;
}

export function DiscoveryTimeline({ data, title = 'Discovery Timeline' }: DiscoveryTimelineProps) {
  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-muted rounded-lg">
        <p className="text-muted-foreground">No data available</p>
      </div>
    );
  }

  const times = data.map((d) => d.elapsed_time / 3600); // Convert to hours
  const povs = data.map((d) => d.cumulative_povs);
  const patches = data.map((d) => d.cumulative_patches);

  return (
    <PlotlyChart
      data={[
        {
          x: times,
          y: povs,
          type: 'scatter',
          mode: 'lines+markers',
          name: 'POVs',
          line: { color: '#3b82f6', width: 2 },
          marker: { size: 6 },
        },
        {
          x: times,
          y: patches,
          type: 'scatter',
          mode: 'lines+markers',
          name: 'Patches',
          line: { color: '#10b981', width: 2 },
          marker: { size: 6 },
        },
      ]}
      layout={{
        title: { text: title, font: { size: 16 } },
        xaxis: { title: { text: 'Time (hours)' } },
        yaxis: { title: { text: 'Count' } },
        legend: { orientation: 'h', y: -0.2 },
        height: 300,
      }}
      className="h-[300px]"
    />
  );
}
