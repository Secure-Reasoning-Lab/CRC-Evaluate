"""HTML report generation with Tailwind CSS and Plotly.js."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from crsbench.reporting.models import (
    ExperimentMetrics,
    SnapshotData,
    TrialMetrics,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# shadcn-style CSS variables and base styles
SHADCN_STYLES = """
<style>
  :root {
    --background: 0 0% 100%;
    --foreground: 222.2 84% 4.9%;
    --card: 0 0% 100%;
    --card-foreground: 222.2 84% 4.9%;
    --primary: 221.2 83.2% 53.3%;
    --primary-foreground: 210 40% 98%;
    --secondary: 210 40% 96.1%;
    --secondary-foreground: 222.2 47.4% 11.2%;
    --muted: 210 40% 96.1%;
    --muted-foreground: 215.4 16.3% 46.9%;
    --accent: 210 40% 96.1%;
    --accent-foreground: 222.2 47.4% 11.2%;
    --destructive: 0 84.2% 60.2%;
    --border: 214.3 31.8% 91.4%;
    --ring: 221.2 83.2% 53.3%;
    --radius: 0.5rem;
  }
  body {
    background-color: hsl(var(--background));
    color: hsl(var(--foreground));
  }
  .card {
    background-color: hsl(var(--card));
    color: hsl(var(--card-foreground));
    border: 1px solid hsl(var(--border));
  }
  .badge {
    display: inline-flex;
    align-items: center;
    border-radius: 9999px;
    padding: 0.125rem 0.625rem;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .badge-default { background-color: hsl(var(--primary)); color: hsl(var(--primary-foreground)); }
  .badge-success { background-color: #22c55e; color: white; }
  .badge-secondary { background-color: hsl(var(--secondary)); color: hsl(var(--secondary-foreground)); }
  .text-muted { color: hsl(var(--muted-foreground)); }
  table { border-collapse: collapse; }
  th { background-color: hsl(var(--muted)); font-weight: 600; }
  th, td { padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid hsl(var(--border)); }
  tr:hover { background-color: hsl(var(--muted) / 0.5); }
  .empty-state {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: hsl(var(--muted-foreground));
    font-style: italic;
  }
</style>
"""

# HTML template for trial report
TRIAL_REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trial Report - {trial_num}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
  {shadcn_styles}
</head>
<body class="min-h-screen bg-gray-50">
  <!-- Header -->
  <div class="bg-gradient-to-r from-blue-600 to-purple-600 text-white">
    <div class="max-w-7xl mx-auto px-6 py-8">
      <h1 class="text-3xl font-bold mb-2">Trial Report: Trial {trial_num}</h1>
      <div class="flex flex-wrap items-center gap-4 text-sm opacity-90">
        <span><strong>Benchmark:</strong> {benchmark}</span>
        <span><strong>CRS:</strong> {crs}</span>
        <span><strong>Harness:</strong> {harness}</span>
        <span class="badge {mode_badge_class}">{mode_display}</span>
        <span class="ml-auto">Generated: {generated_at}</span>
      </div>
    </div>
  </div>

  <div class="max-w-7xl mx-auto px-6 py-8 space-y-6">
    <!-- Summary Stats -->
    <div class="grid gap-4 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
      {stat_cards}
    </div>

    <!-- Discovery Timeline -->
    <div class="card rounded-lg shadow-sm p-6">
      <h2 class="text-xl font-semibold mb-4 flex items-center gap-2">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"></path></svg>
        Discovery Timeline
      </h2>
      <div id="discovery-chart" style="height: 320px;"></div>
    </div>

    <!-- Cost Analysis -->
    <div class="grid gap-4 md:grid-cols-2">
      <div class="card rounded-lg shadow-sm p-6">
        <h2 class="text-xl font-semibold mb-4">Cost Over Time</h2>
        <div id="cost-chart" style="height: 288px;"></div>
      </div>
      <div class="card rounded-lg shadow-sm p-6">
        <h2 class="text-xl font-semibold mb-4">Cost by Model</h2>
        <div id="cost-breakdown-chart" style="height: 288px;"></div>
      </div>
    </div>

    <!-- Snapshot Timeline Chart -->
    <div class="card rounded-lg shadow-sm p-6">
      <h2 class="text-xl font-semibold mb-4">Snapshot Timeline</h2>
      <div id="snapshot-timeline-chart" style="height: 300px;"></div>
    </div>

    <!-- Snapshot Details Table -->
    <div class="card rounded-lg shadow-sm p-6">
      <h2 class="text-xl font-semibold mb-4">Snapshot Details</h2>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr>
              <th class="rounded-tl-lg">Cycle</th>
              <th>Elapsed Time</th>
              <th class="text-right">POVs (New)</th>
              <th class="text-right">Cumulative POVs</th>
              <th class="text-right">Patches (New)</th>
              <th class="text-right">Cumulative Patches</th>
              <th class="text-right rounded-tr-lg">Cumulative Cost</th>
            </tr>
          </thead>
          <tbody>
            {snapshot_rows}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Footer -->
    <div class="text-center text-sm text-muted py-4">
      Generated by CRSBench Reporting Module
    </div>
  </div>

  <script>
    const timeSeriesData = {time_series_json};
    const costBreakdownData = {cost_breakdown_json};

    const plotlyConfig = {{
      displayModeBar: true,
      responsive: true,
      modeBarButtonsToRemove: ['lasso2d', 'select2d']
    }};

    const plotlyLayout = {{
      margin: {{ t: 20, r: 20, b: 40, l: 50 }},
      paper_bgcolor: 'transparent',
      plot_bgcolor: 'transparent',
      font: {{ family: 'system-ui, sans-serif', size: 12 }},
      xaxis: {{ gridcolor: '#e5e7eb', linecolor: '#e5e7eb' }},
      yaxis: {{ gridcolor: '#e5e7eb', linecolor: '#e5e7eb' }},
      legend: {{ orientation: 'h', y: -0.15 }}
    }};

    function formatTime(seconds) {{
      if (seconds < 60) return seconds.toFixed(0) + 's';
      if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + Math.round(seconds % 60) + 's';
      return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
    }}

    // Discovery Timeline Chart
    if (timeSeriesData.length > 0) {{
      const discoveryTraces = [
        {{
          x: timeSeriesData.map(d => d.time),
          y: timeSeriesData.map(d => d.povs),
          name: 'POVs',
          type: 'scatter',
          mode: 'lines+markers',
          line: {{ color: '#3b82f6', width: 2 }},
          marker: {{ size: 6 }},
          hovertemplate: 'Time: %{{x:.0f}}s<br>POVs: %{{y}}<extra></extra>'
        }},
        {{
          x: timeSeriesData.map(d => d.time),
          y: timeSeriesData.map(d => d.patches),
          name: 'Patches',
          type: 'scatter',
          mode: 'lines+markers',
          line: {{ color: '#22c55e', width: 2 }},
          marker: {{ size: 6 }},
          hovertemplate: 'Time: %{{x:.0f}}s<br>Patches: %{{y}}<extra></extra>'
        }}
      ];
      Plotly.newPlot('discovery-chart', discoveryTraces, {{
        ...plotlyLayout,
        xaxis: {{ ...plotlyLayout.xaxis, title: 'Elapsed Time (seconds)' }},
        yaxis: {{ ...plotlyLayout.yaxis, title: 'Count' }}
      }}, plotlyConfig);
    }} else {{
      document.getElementById('discovery-chart').innerHTML = '<div class="empty-state">No discovery data available</div>';
    }}

    // Cost Over Time Chart
    if (timeSeriesData.length > 0 && timeSeriesData.some(d => d.cost > 0)) {{
      const costTrace = {{
        x: timeSeriesData.map(d => d.time),
        y: timeSeriesData.map(d => d.cost),
        type: 'scatter',
        mode: 'lines',
        fill: 'tozeroy',
        fillcolor: 'rgba(139, 92, 246, 0.2)',
        line: {{ color: '#8b5cf6', width: 2 }},
        hovertemplate: 'Time: %{{x:.0f}}s<br>Cost: $%{{y:.4f}}<extra></extra>'
      }};
      Plotly.newPlot('cost-chart', [costTrace], {{
        ...plotlyLayout,
        showlegend: false,
        xaxis: {{ ...plotlyLayout.xaxis, title: 'Elapsed Time (seconds)' }},
        yaxis: {{ ...plotlyLayout.yaxis, title: 'Cost (USD)', tickprefix: '$' }}
      }}, plotlyConfig);
    }} else {{
      document.getElementById('cost-chart').innerHTML = '<div class="empty-state">No LLM cost data recorded</div>';
    }}

    // Cost by Model Pie Chart
    if (costBreakdownData.length > 0 && costBreakdownData.some(d => d.value > 0)) {{
      const pieTrace = {{
        labels: costBreakdownData.map(d => d.name),
        values: costBreakdownData.map(d => d.value),
        type: 'pie',
        hole: 0.4,
        textinfo: 'label+percent',
        hovertemplate: '%{{label}}<br>$%{{value:.4f}}<br>%{{percent}}<extra></extra>',
        marker: {{
          colors: ['#3b82f6', '#ef4444', '#22c55e', '#f59e0b', '#8b5cf6', '#ec4899']
        }}
      }};
      Plotly.newPlot('cost-breakdown-chart', [pieTrace], {{
        ...plotlyLayout,
        showlegend: true,
        legend: {{ orientation: 'v', x: 1, y: 0.5 }}
      }}, plotlyConfig);
    }} else {{
      document.getElementById('cost-breakdown-chart').innerHTML = '<div class="empty-state">No LLM usage by model</div>';
    }}

    // Snapshot Timeline Chart (visual representation)
    if (timeSeriesData.length > 0) {{
      const snapshotTraces = [
        {{
          x: timeSeriesData.map(d => d.time),
          y: timeSeriesData.map(d => d.povs),
          name: 'Cumulative POVs',
          type: 'scatter',
          mode: 'lines+markers',
          line: {{ color: '#3b82f6', width: 2, shape: 'hv' }},
          marker: {{ size: 8, symbol: 'circle' }},
          hovertemplate: 'Cycle %{{pointNumber}}<br>Time: %{{x:.0f}}s<br>POVs: %{{y}}<extra></extra>'
        }},
        {{
          x: timeSeriesData.map(d => d.time),
          y: timeSeriesData.map(d => d.patches),
          name: 'Cumulative Patches',
          type: 'scatter',
          mode: 'lines+markers',
          line: {{ color: '#22c55e', width: 2, shape: 'hv' }},
          marker: {{ size: 8, symbol: 'diamond' }},
          hovertemplate: 'Cycle %{{pointNumber}}<br>Time: %{{x:.0f}}s<br>Patches: %{{y}}<extra></extra>'
        }}
      ];
      Plotly.newPlot('snapshot-timeline-chart', snapshotTraces, {{
        ...plotlyLayout,
        xaxis: {{ ...plotlyLayout.xaxis, title: 'Elapsed Time (seconds)' }},
        yaxis: {{ ...plotlyLayout.yaxis, title: 'Cumulative Count' }}
      }}, plotlyConfig);
    }} else {{
      document.getElementById('snapshot-timeline-chart').innerHTML = '<div class="empty-state">No snapshot data</div>';
    }}
  </script>
</body>
</html>
"""

# HTML template for experiment report
EXPERIMENT_REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Experiment Report - {experiment_name}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
  {shadcn_styles}
</head>
<body class="min-h-screen bg-gray-50">
  <!-- Header -->
  <div class="bg-gradient-to-r from-emerald-600 to-teal-600 text-white">
    <div class="max-w-7xl mx-auto px-6 py-8">
      <h1 class="text-3xl font-bold mb-2">Experiment Report: {experiment_name}</h1>
      <div class="flex flex-wrap items-center gap-4 text-sm opacity-90">
        <span><strong>Total Trials:</strong> {total_trials}</span>
        <span><strong>Valid Trials:</strong> {valid_trials}</span>
        <span class="ml-auto">Generated: {generated_at}</span>
      </div>
    </div>
  </div>

  <div class="max-w-7xl mx-auto px-6 py-8 space-y-6">
    <!-- Summary Stats -->
    <div class="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
      {stat_cards}
    </div>

    <!-- CRS Comparison -->
    <div class="card rounded-lg shadow-sm p-6">
      <h2 class="text-xl font-semibold mb-4">CRS Comparison</h2>
      <div class="grid gap-4 md:grid-cols-2">
        <div>
          <h3 class="text-sm font-medium text-muted mb-2">Average POVs by CRS</h3>
          <div id="crs-povs-chart" style="height: 288px;"></div>
        </div>
        <div>
          <h3 class="text-sm font-medium text-muted mb-2">Total Cost by CRS</h3>
          <div id="crs-cost-chart" style="height: 288px;"></div>
        </div>
      </div>
    </div>

    <!-- Benchmark Analysis -->
    <div class="card rounded-lg shadow-sm p-6">
      <h2 class="text-xl font-semibold mb-4">Benchmark Analysis</h2>
      <div id="benchmark-chart" style="height: 320px;"></div>
    </div>

    <!-- Trial Results Table -->
    <div class="card rounded-lg shadow-sm p-6">
      <h2 class="text-xl font-semibold mb-4">Trial Results</h2>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr>
              <th class="rounded-tl-lg">Trial</th>
              <th>Benchmark</th>
              <th>CRS</th>
              <th>Mode</th>
              <th class="text-right">POVs</th>
              <th class="text-right">Unique POVs</th>
              <th class="text-right">Patches</th>
              <th class="text-right">Cost</th>
              <th class="text-right rounded-tr-lg">Time</th>
            </tr>
          </thead>
          <tbody>
            {trial_rows}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Footer -->
    <div class="text-center text-sm text-muted py-4">
      Generated by CRSBench Reporting Module
    </div>
  </div>

  <script>
    const crsData = {crs_data_json};
    const benchmarkData = {benchmark_data_json};

    const plotlyConfig = {{
      displayModeBar: true,
      responsive: true,
      modeBarButtonsToRemove: ['lasso2d', 'select2d']
    }};

    const plotlyLayout = {{
      margin: {{ t: 20, r: 20, b: 60, l: 50 }},
      paper_bgcolor: 'transparent',
      plot_bgcolor: 'transparent',
      font: {{ family: 'system-ui, sans-serif', size: 12 }},
      xaxis: {{ gridcolor: '#e5e7eb', linecolor: '#e5e7eb' }},
      yaxis: {{ gridcolor: '#e5e7eb', linecolor: '#e5e7eb' }},
      legend: {{ orientation: 'h', y: -0.2 }}
    }};

    // CRS POVs Chart
    if (crsData.length > 0) {{
      const crsPovTrace = {{
        x: crsData.map(d => d.name),
        y: crsData.map(d => d.avgPovs),
        type: 'bar',
        marker: {{ color: '#3b82f6', cornerradius: 4 }},
        hovertemplate: '%{{x}}<br>Avg POVs: %{{y:.1f}}<extra></extra>'
      }};
      Plotly.newPlot('crs-povs-chart', [crsPovTrace], {{
        ...plotlyLayout,
        showlegend: false,
        yaxis: {{ ...plotlyLayout.yaxis, title: 'Average POVs' }}
      }}, plotlyConfig);
    }} else {{
      document.getElementById('crs-povs-chart').innerHTML = '<div class="empty-state">No CRS data available</div>';
    }}

    // CRS Cost Chart
    if (crsData.length > 0) {{
      const crsCostTrace = {{
        x: crsData.map(d => d.name),
        y: crsData.map(d => d.totalCost),
        type: 'bar',
        marker: {{ color: '#8b5cf6', cornerradius: 4 }},
        hovertemplate: '%{{x}}<br>Total Cost: $%{{y:.2f}}<extra></extra>'
      }};
      Plotly.newPlot('crs-cost-chart', [crsCostTrace], {{
        ...plotlyLayout,
        showlegend: false,
        yaxis: {{ ...plotlyLayout.yaxis, title: 'Total Cost (USD)', tickprefix: '$' }}
      }}, plotlyConfig);
    }} else {{
      document.getElementById('crs-cost-chart').innerHTML = '<div class="empty-state">No CRS cost data</div>';
    }}

    // Benchmark Chart
    if (benchmarkData.length > 0) {{
      const benchmarkTraces = [
        {{
          x: benchmarkData.map(d => d.name),
          y: benchmarkData.map(d => d.avgPovs),
          name: 'Avg POVs',
          type: 'bar',
          marker: {{ color: '#3b82f6' }},
          hovertemplate: '%{{x}}<br>Avg POVs: %{{y:.1f}}<extra></extra>'
        }},
        {{
          x: benchmarkData.map(d => d.name),
          y: benchmarkData.map(d => d.avgPatches),
          name: 'Avg Patches',
          type: 'bar',
          marker: {{ color: '#22c55e' }},
          hovertemplate: '%{{x}}<br>Avg Patches: %{{y:.1f}}<extra></extra>'
        }}
      ];
      Plotly.newPlot('benchmark-chart', benchmarkTraces, {{
        ...plotlyLayout,
        barmode: 'group',
        xaxis: {{ ...plotlyLayout.xaxis, tickangle: -45 }},
        yaxis: {{ ...plotlyLayout.yaxis, title: 'Average Count' }}
      }}, plotlyConfig);
    }} else {{
      document.getElementById('benchmark-chart').innerHTML = '<div class="empty-state">No benchmark data available</div>';
    }}
  </script>
</body>
</html>
"""


class HTMLReportGenerator:
    """Generate HTML reports with Tailwind CSS and Plotly.js.

    No build step required - uses CDN for all dependencies.
    """

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_trial_report(
        self,
        trial_metrics: TrialMetrics,
        snapshots: list[SnapshotData],
    ) -> Path:
        """Generate HTML report for a single trial."""
        sorted_snapshots = sorted(snapshots, key=lambda s: s.cycle)

        # Build time series data
        time_series = self._build_time_series(sorted_snapshots)
        cost_breakdown = self._build_cost_breakdown(trial_metrics)

        # Build stat cards
        stat_cards = self._build_stat_cards(
            [
                ("POVs Discovered", trial_metrics.total_povs_discovered),
                ("Unique POVs", len(trial_metrics.unique_pov_names)),
                ("Patches Generated", trial_metrics.total_patches_generated),
                ("Unique Patches", len(trial_metrics.unique_patch_names)),
                ("Total LLM Cost", f"${trial_metrics.total_llm_cost:.4f}"),
                ("Total Time", self._format_time(trial_metrics.total_time)),
            ]
        )

        # Build snapshot rows
        snapshot_rows = self._build_snapshot_rows(sorted_snapshots)

        # Mode badge
        mode_badge_class = {
            "bug_finding": "badge-default",
            "patch_generation": "badge-success",
        }.get(trial_metrics.mode.value, "badge-secondary")

        html = TRIAL_REPORT_TEMPLATE.format(
            trial_num=trial_metrics.trial_num,
            benchmark=trial_metrics.benchmark,
            crs=trial_metrics.crs,
            harness=trial_metrics.harness,
            mode_display=trial_metrics.mode.value.replace("_", " ").title(),
            mode_badge_class=mode_badge_class,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            shadcn_styles=SHADCN_STYLES,
            stat_cards=stat_cards,
            snapshot_rows=snapshot_rows,
            time_series_json=json.dumps(time_series),
            cost_breakdown_json=json.dumps(cost_breakdown),
        )

        trial_reports_dir = self.output_dir / "trial-reports"
        trial_reports_dir.mkdir(exist_ok=True)

        # Create unique filename from trial path
        # e.g., "exp/afc-curl/curl_fuzzer/delta/trial-1" -> "afc-curl-curl_fuzzer-delta-trial-1"
        trial_path = Path(trial_metrics.trial_dir)
        # Skip experiment dir (first part) and join the rest
        trial_id = (
            "-".join(trial_path.parts[1:])
            if len(trial_path.parts) > 1
            else trial_path.name
        )
        output_path = trial_reports_dir / f"{trial_id}.html"
        output_path.write_text(html)

        logger.info(f"Generated HTML trial report: {output_path}")
        return output_path

    def generate_experiment_report(self, experiment_metrics: ExperimentMetrics) -> Path:
        """Generate HTML report for an experiment."""
        experiment_name = Path(experiment_metrics.experiment_dir).name

        # Build stat cards
        stat_cards = self._build_stat_cards(
            [
                ("Total Trials", experiment_metrics.total_trials),
                ("Avg POVs / Trial", f"{experiment_metrics.avg_povs_per_trial:.1f}"),
                (
                    "Avg Patches / Trial",
                    f"{experiment_metrics.avg_patches_per_trial:.1f}",
                ),
                ("Avg Cost / Trial", f"${experiment_metrics.avg_cost_per_trial:.4f}"),
            ]
        )

        # Build CRS data
        crs_data = [
            {"name": name, "avgPovs": d.avg_povs, "totalCost": d.total_cost}
            for name, d in experiment_metrics.by_crs.items()
        ]

        # Build benchmark data
        benchmark_data = [
            {"name": name, "avgPovs": d.avg_povs, "avgPatches": d.avg_patches}
            for name, d in experiment_metrics.by_benchmark.items()
        ]

        # Build trial rows
        trial_rows = self._build_trial_rows(experiment_metrics.trial_metrics)

        html = EXPERIMENT_REPORT_TEMPLATE.format(
            experiment_name=experiment_name,
            total_trials=experiment_metrics.total_trials,
            valid_trials=experiment_metrics.valid_trials,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            shadcn_styles=SHADCN_STYLES,
            stat_cards=stat_cards,
            trial_rows=trial_rows,
            crs_data_json=json.dumps(crs_data),
            benchmark_data_json=json.dumps(benchmark_data),
        )

        output_path = self.output_dir / f"experiment-{experiment_name}.html"
        output_path.write_text(html)

        logger.info(f"Generated HTML experiment report: {output_path}")
        return output_path

    def _build_stat_cards(self, stats: list[tuple[str, Any]]) -> str:
        cards = []
        for title, value in stats:
            cards.append(f"""
      <div class="card rounded-lg shadow-sm p-6">
        <p class="text-sm font-medium text-muted">{title}</p>
        <p class="text-3xl font-bold tracking-tight">{value}</p>
      </div>""")
        return "\n".join(cards)

    def _build_time_series(self, snapshots: list[SnapshotData]) -> list[dict]:
        data = []
        cumulative_povs = 0
        cumulative_patches = 0
        cumulative_cost = 0.0

        for s in snapshots:
            cumulative_povs += s.pov_count
            cumulative_patches += s.patch_count
            # Use cumulative cost from llm_usage (it's already cumulative in snapshots)
            cumulative_cost = s.llm_usage.total_cost_usd
            data.append(
                {
                    "time": s.elapsed_time,
                    "povs": cumulative_povs,
                    "patches": cumulative_patches,
                    "cost": cumulative_cost,
                }
            )
        return data

    def _build_cost_breakdown(self, trial_metrics: TrialMetrics) -> list[dict]:
        return [
            {"name": model, "value": usage.get("cost_usd", 0.0)}
            for model, usage in trial_metrics.llm_usage_by_model.items()
            if usage.get("cost_usd", 0.0) > 0
        ]

    def _build_snapshot_rows(self, snapshots: list[SnapshotData]) -> str:
        rows = []
        cumulative_povs = 0
        cumulative_patches = 0

        for s in snapshots:
            new_povs = s.pov_count
            new_patches = s.patch_count
            cumulative_povs += new_povs
            cumulative_patches += new_patches

            # Highlight rows with new discoveries
            row_class = ""
            if new_povs > 0 or new_patches > 0:
                row_class = 'class="bg-green-50"'

            rows.append(f"""
            <tr {row_class}>
              <td class="font-medium">#{s.cycle}</td>
              <td>{self._format_time(s.elapsed_time)}</td>
              <td class="text-right">{"+" + str(new_povs) if new_povs > 0 else "-"}</td>
              <td class="text-right font-semibold">{cumulative_povs}</td>
              <td class="text-right">{"+" + str(new_patches) if new_patches > 0 else "-"}</td>
              <td class="text-right font-semibold">{cumulative_patches}</td>
              <td class="text-right">${s.llm_usage.total_cost_usd:.4f}</td>
            </tr>""")
        return "\n".join(rows)

    def _build_trial_rows(self, trials: list[TrialMetrics]) -> str:
        rows = []
        for t in trials:
            mode_class = {
                "bug_finding": "badge-default",
                "patch_generation": "badge-success",
            }.get(t.mode.value, "badge-secondary")
            rows.append(f"""
            <tr>
              <td class="font-medium">Trial {t.trial_num}</td>
              <td>{t.benchmark}</td>
              <td>{t.crs}</td>
              <td><span class="badge {mode_class}">{t.mode.value.replace("_", " ").title()}</span></td>
              <td class="text-right">{t.total_povs_discovered}</td>
              <td class="text-right">{len(t.unique_pov_names)}</td>
              <td class="text-right">{t.total_patches_generated}</td>
              <td class="text-right">${t.total_llm_cost:.4f}</td>
              <td class="text-right">{self._format_time(t.total_time)}</td>
            </tr>""")
        return "\n".join(rows)

    def _format_time(self, seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"
