'use client';

import dynamic from 'next/dynamic';
import type { Data, Layout, Config } from 'plotly.js';

// Dynamically import Plotly to avoid SSR issues
const Plot = dynamic(() => import('react-plotly.js'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-64 bg-muted rounded-lg">
      <p className="text-muted-foreground">Loading chart...</p>
    </div>
  ),
});

interface PlotlyChartProps {
  data: Data[];
  layout?: Partial<Layout>;
  config?: Partial<Config>;
  className?: string;
}

export function PlotlyChart({
  data,
  layout = {},
  config = {},
  className = '',
}: PlotlyChartProps) {
  const defaultLayout: Partial<Layout> = {
    autosize: true,
    margin: { l: 50, r: 30, t: 40, b: 50 },
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: {
      family: 'system-ui, sans-serif',
      size: 12,
    },
    ...layout,
  };

  const defaultConfig: Partial<Config> = {
    responsive: true,
    displayModeBar: false,
    ...config,
  };

  return (
    <div className={`w-full ${className}`}>
      <Plot
        data={data}
        layout={defaultLayout}
        config={defaultConfig}
        style={{ width: '100%', height: '100%' }}
        useResizeHandler
      />
    </div>
  );
}
