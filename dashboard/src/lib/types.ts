// Shared types for dashboard
// These match the JSON report format from crsbench/reporting/generators/json.py

// Experiment-level types
export interface ExperimentSummary {
  total_trials: number;
  valid_trials: number;
  avg_povs_per_trial: number;
  avg_patches_per_trial: number;
  avg_cost_per_trial: number;
}

export interface CRSMetrics {
  crs: string;
  trial_count: number;
  avg_povs: number;
  avg_patches: number;
  avg_cost: number;
  total_cost: number;
  total_povs: number;
}

export interface BenchmarkMetrics {
  benchmark: string;
  trial_count: number;
  avg_povs: number;
  avg_patches: number;
  avg_time_to_first_pov: number | null;
  total_cost: number;
}

export interface TrialSummary {
  trial_dir: string;
  trial_num: number;
  crs: string;
  benchmark: string;
  harness: string;
  mode: string;
  total_povs: number;
  unique_povs: number;
  total_patches: number;
  unique_patches: number;
  total_cost: number;
  total_time: number;
  time_to_first_pov: number | null;
}

export interface ExperimentReport {
  report_type: 'experiment';
  generated_at: string;
  experiment_dir: string;
  summary: ExperimentSummary;
  by_crs: Record<string, CRSMetrics>;
  by_benchmark: Record<string, BenchmarkMetrics>;
  trial_summaries: TrialSummary[];
  trial_report_files?: string[];
}

export interface ExperimentListItem {
  name: string;
  summary: ExperimentSummary | null;
  crs_list: string[];
  benchmark_list: string[];
}

// Trial-level types
export interface TrialInfo {
  trial_dir: string;
  trial_num: number;
  crs: string;
  benchmark: string;
  harness: string;
  mode: string;
}

export interface TrialSummaryMetrics {
  total_povs_discovered: number;
  unique_povs: number;
  total_patches_generated: number;
  unique_patches: number;
  total_llm_cost: number;
  total_llm_tokens: number;
  total_time: number;
  time_to_first_pov: number | null;
  snapshot_count: number;
}

export interface ArtifactList {
  unique_names: string[];
  count: number;
}

export interface LLMUsageInfo {
  total_tokens: number;
  total_cost: number;
  by_model: Record<string, ModelUsage>;
}

export interface ModelUsage {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  request_count?: number;
}

export interface TimeSeriesPoint {
  elapsed_time: number;
  cumulative_povs: number;
  cumulative_patches: number;
  llm_tokens: number;
  llm_cost: number;
}

export interface SnapshotEntry {
  cycle: number;
  timestamp: number;
  elapsed_time: number;
  povs_in_snapshot: number;
  cumulative_povs: number;
  patches_in_snapshot: number;
  cumulative_patches: number;
  llm_cost: number;
  llm_tokens: number;
}

export interface TimelineData {
  total_snapshots: number;
  snapshots: SnapshotEntry[];
}

export interface TrialReport {
  report_type: 'trial';
  generated_at: string;
  trial: TrialInfo;
  summary: TrialSummaryMetrics;
  povs: ArtifactList;
  patches: ArtifactList;
  llm_usage: LLMUsageInfo;
  time_series: TimeSeriesPoint[];
  timeline: TimelineData;
}

export interface TrialFileInfo {
  trial_num: number;
  file_path: string;
}
