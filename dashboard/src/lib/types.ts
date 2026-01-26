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
  /** Outer directory name (used for URL routing) */
  id: string;
  /** Experiment name from report */
  name: string;
  /** Experiment mode (bug_finding or patch_generation) */
  mode: 'bug_finding' | 'patch_generation' | 'mixed' | null;
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

// LLM Logs types
export interface LLMMessage {
  role: string;
  content: string;
}

export interface LLMResponseChoice {
  index: number;
  message: {
    role: string;
    content: string;
    images?: unknown[];
  };
  finish_reason?: string;
}

export interface LLMResponse {
  id: string;
  model: string;
  object: string;
  choices: LLMResponseChoice[];
  usage?: {
    total_tokens: number;
    prompt_tokens: number;
    completion_tokens: number;
  };
}

export interface ProxyServerRequest {
  model: string;
  messages: LLMMessage[];
  temperature?: number;
}

export interface LLMLogEntry {
  request_id: string;
  call_type: string;
  api_key: string;
  spend: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  startTime: string;
  endTime: string;
  completionStartTime: string;
  model: string;
  model_id: string;
  model_group: string;
  custom_llm_provider?: string;
  api_base: string;
  user: string;
  metadata: Record<string, unknown>;
  cache_hit: string;
  cache_key: string;
  request_tags: unknown;
  team_id: string | null;
  end_user: string | null;
  requester_ip_address: string | null;
  // messages field (may be empty)
  messages: Record<string, unknown> | LLMMessage[];
  // response is OpenAI ChatCompletion format
  response: LLMResponse;
  // proxy_server_request contains the full conversation history
  proxy_server_request?: ProxyServerRequest;
  session_id?: string;
  status?: string;
}

export interface LLMLogsFile {
  trial_id: string;
  timestamp: string;
  total_requests: number;
  logs: LLMLogEntry[];
}

export interface CRSLogsResponse {
  type: 'crs';
  content: string | null;
  error?: string;
}

export interface LLMLogsResponse {
  type: 'llm';
  content: LLMLogsFile | null;
  error?: string;
}

// Execution info from execution.json
export interface ExecutionInfo {
  timestamp: string;
  command: string[];
  crs_config: {
    build_timeout: number;
    run_timeout: number;
    hints_enabled: boolean;
    hint_sarif_level: string | null;
    hint_corpus_level: string | null;
    project_image_prefix: string;
    mode: string;
    allocated_cpus: number | null;
  };
  hints: {
    enabled: boolean;
    path: string | null;
    corpus_level: string | null;
    sarif_count: number;
    corpus_count: number;
  };
  povs: {
    provided: boolean;
    path: string;
    count: number;
  };
  execution: {
    duration_seconds: number;
    returncode: number;
    success: boolean;
  };
}

export interface ExecutionResponse {
  type: 'execution';
  content: ExecutionInfo | null;
  error?: string;
}

// Artifact types for viewing patches and POVs
export interface ArtifactInfo {
  name: string;
  type: 'patch' | 'pov';
  size?: number;
}

export interface ArtifactListResponse {
  patches: ArtifactInfo[];
  povs: ArtifactInfo[];
  error?: string;
}

export interface ArtifactContentResponse {
  type: 'patch' | 'pov';
  name: string;
  content: string | null;
  error?: string;
}
