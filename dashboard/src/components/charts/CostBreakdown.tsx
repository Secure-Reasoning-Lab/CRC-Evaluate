'use client';

import type { ModelUsage } from '@/lib/types';
import { PlotlyChart } from './PlotlyChart';

interface CostBreakdownProps {
  data: Record<string, ModelUsage>;
  title?: string;
}

export function CostBreakdown({ data, title = 'Cost by Model' }: CostBreakdownProps) {
  const models = Object.keys(data);

  if (models.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-muted rounded-lg">
        <p className="text-muted-foreground">No LLM cost data available</p>
      </div>
    );
  }

  const labels = models;
  const values = models.map((m) => data[m].cost_usd);

  // Only show if there's actual cost data
  const totalCost = values.reduce((sum, v) => sum + v, 0);
  if (totalCost === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-muted rounded-lg">
        <p className="text-muted-foreground">No LLM cost data recorded</p>
      </div>
    );
  }

  return (
    <PlotlyChart
      data={[
        {
          labels,
          values,
          type: 'pie',
          textinfo: 'label+percent',
          hovertemplate: '%{label}<br>$%{value:.2f}<extra></extra>',
          marker: {
            colors: ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899'],
          },
        },
      ]}
      layout={{
        title: { text: title, font: { size: 16 } },
        height: 300,
        showlegend: true,
        legend: { orientation: 'h', y: -0.1 },
      }}
      className="h-[300px]"
    />
  );
}
