'use client';

import type { BenchmarkMetrics } from '@/lib/types';
import { PlotlyChart } from './PlotlyChart';

interface BenchmarkAnalysisProps {
  data: Record<string, BenchmarkMetrics>;
  title?: string;
}

export function BenchmarkAnalysis({
  data,
  title = 'Benchmark Analysis',
}: BenchmarkAnalysisProps) {
  const benchmarks = Object.keys(data).sort();

  if (benchmarks.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-muted rounded-lg">
        <p className="text-muted-foreground">No data available</p>
      </div>
    );
  }

  const avgPovs = benchmarks.map((b) => data[b].avg_povs);
  const avgPatches = benchmarks.map((b) => data[b].avg_patches);

  return (
    <PlotlyChart
      data={[
        {
          x: benchmarks,
          y: avgPovs,
          type: 'bar',
          name: 'Avg POVs',
          marker: { color: '#3b82f6' },
        },
        {
          x: benchmarks,
          y: avgPatches,
          type: 'bar',
          name: 'Avg Patches',
          marker: { color: '#10b981' },
        },
      ]}
      layout={{
        title: { text: title, font: { size: 16 } },
        barmode: 'group',
        xaxis: {
          title: { text: 'Benchmark' },
          tickangle: -45,
        },
        yaxis: { title: { text: 'Count' } },
        legend: { orientation: 'h', y: -0.3 },
        height: 350,
        margin: { b: 100 },
      }}
      className="h-[350px]"
    />
  );
}
