'use client';

import type { CRSMetrics } from '@/lib/types';
import { PlotlyChart } from './PlotlyChart';

interface CRSComparisonProps {
  data: Record<string, CRSMetrics>;
  title?: string;
}

export function CRSComparison({ data, title = 'CRS Comparison' }: CRSComparisonProps) {
  const crsList = Object.keys(data).sort();

  if (crsList.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-muted rounded-lg">
        <p className="text-muted-foreground">No data available</p>
      </div>
    );
  }

  const avgPovs = crsList.map((crs) => data[crs].avg_povs);
  const avgCost = crsList.map((crs) => data[crs].avg_cost);

  return (
    <PlotlyChart
      data={[
        {
          x: crsList,
          y: avgPovs,
          type: 'bar',
          name: 'Avg POVs',
          marker: { color: '#3b82f6' },
        },
        {
          x: crsList,
          y: avgCost,
          type: 'bar',
          name: 'Avg Cost ($)',
          marker: { color: '#f59e0b' },
          yaxis: 'y2',
        },
      ]}
      layout={{
        title: { text: title, font: { size: 16 } },
        barmode: 'group',
        xaxis: { title: { text: 'CRS' } },
        yaxis: { title: { text: 'Avg POVs' }, side: 'left' },
        yaxis2: {
          title: { text: 'Avg Cost ($)' },
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
