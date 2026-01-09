'use client';

import type { TimeSeriesPoint } from '@/lib/types';
import { PlotlyChart } from './PlotlyChart';

interface CostOverTimeProps {
  data: TimeSeriesPoint[];
  title?: string;
}

export function CostOverTime({ data, title = 'LLM Cost Over Time' }: CostOverTimeProps) {
  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-muted rounded-lg">
        <p className="text-muted-foreground">No data available</p>
      </div>
    );
  }

  // Check if there's any cost data
  const totalCost = data.reduce((sum, d) => sum + d.llm_cost, 0);
  if (totalCost === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-muted rounded-lg">
        <p className="text-muted-foreground">No LLM cost data recorded</p>
      </div>
    );
  }

  const times = data.map((d) => d.elapsed_time / 3600); // Convert to hours
  const costs = data.map((d) => d.llm_cost);
  const tokens = data.map((d) => d.llm_tokens / 1000); // Convert to thousands

  return (
    <PlotlyChart
      data={[
        {
          x: times,
          y: costs,
          type: 'scatter',
          mode: 'lines+markers',
          name: 'Cost ($)',
          line: { color: '#f59e0b', width: 2 },
          marker: { size: 6 },
        },
        {
          x: times,
          y: tokens,
          type: 'scatter',
          mode: 'lines+markers',
          name: 'Tokens (K)',
          line: { color: '#8b5cf6', width: 2 },
          marker: { size: 6 },
          yaxis: 'y2',
        },
      ]}
      layout={{
        title: { text: title, font: { size: 16 } },
        xaxis: { title: { text: 'Time (hours)' } },
        yaxis: { title: { text: 'Cost ($)' }, side: 'left' },
        yaxis2: {
          title: { text: 'Tokens (K)' },
          side: 'right',
          overlaying: 'y',
        },
        legend: { orientation: 'h', y: -0.2 },
        height: 300,
      }}
      className="h-[300px]"
    />
  );
}
