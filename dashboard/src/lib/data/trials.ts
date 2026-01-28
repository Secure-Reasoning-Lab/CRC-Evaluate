import { readdir, readFile } from 'fs/promises';
import path from 'path';

import type {
  TrialFileInfo,
  TrialReport,
  LLMLogsFile,
  ModelUsage,
  TimeSeriesPoint,
} from '@/lib/types';
import { findExperimentName, getReportDataDir } from './experiments';

/**
 * Parse LLM logs to build by_model breakdown and time_series data.
 */
async function parseLlmLogsFromTrialDir(
  trialDir: string
): Promise<{
  by_model: Record<string, ModelUsage>;
  time_series: TimeSeriesPoint[];
  total_tokens: number;
  total_cost: number;
} | null> {
  try {
    const llmLogsPath = path.join(trialDir, 'llm-logs.json');
    const content = await readFile(llmLogsPath, 'utf-8');
    const llmLogs: LLMLogsFile = JSON.parse(content);

    if (!llmLogs.logs || llmLogs.logs.length === 0) {
      return null;
    }

    // Build by_model breakdown
    const byModel: Record<string, ModelUsage> = {};
    let totalTokens = 0;
    let totalCost = 0;

    // Sort logs by startTime for time_series
    const sortedLogs = [...llmLogs.logs].sort((a, b) => {
      return new Date(a.startTime).getTime() - new Date(b.startTime).getTime();
    });

    // Get start time for elapsed_time calculation
    const startTime =
      sortedLogs.length > 0
        ? new Date(sortedLogs[0].startTime).getTime()
        : Date.now();

    // Build time_series
    const timeSeries: TimeSeriesPoint[] = [];
    let cumulativeTokens = 0;
    let cumulativeCost = 0;

    for (const entry of sortedLogs) {
      const model = entry.model || entry.model_id || 'unknown';

      // Update by_model
      if (!byModel[model]) {
        byModel[model] = {
          input_tokens: 0,
          output_tokens: 0,
          cost_usd: 0,
          request_count: 0,
        };
      }
      byModel[model].input_tokens += entry.prompt_tokens || 0;
      byModel[model].output_tokens += entry.completion_tokens || 0;
      byModel[model].cost_usd += entry.spend || 0;
      byModel[model].request_count = (byModel[model].request_count || 0) + 1;

      totalTokens += entry.total_tokens || 0;
      totalCost += entry.spend || 0;

      // Update cumulative values for time_series
      cumulativeTokens += entry.total_tokens || 0;
      cumulativeCost += entry.spend || 0;

      const elapsedTime =
        (new Date(entry.endTime).getTime() - startTime) / 1000; // Convert to seconds

      timeSeries.push({
        elapsed_time: elapsedTime,
        cumulative_povs: 0, // Will be updated from snapshots
        cumulative_patches: 0, // Will be updated from snapshots
        llm_tokens: cumulativeTokens,
        llm_cost: cumulativeCost,
      });
    }

    return {
      by_model: byModel,
      time_series: timeSeries,
      total_tokens: totalTokens,
      total_cost: totalCost,
    };
  } catch {
    return null;
  }
}

// Re-export types for convenience
export type {
  TrialInfo,
  TrialSummaryMetrics,
  ArtifactList,
  LLMUsageInfo,
  ModelUsage,
  TimeSeriesPoint,
  SnapshotEntry,
  TimelineData,
  TrialReport,
  TrialFileInfo,
  LLMLogsFile,
  LLMLogEntry,
  LLMMessage,
  LLMResponse,
  LLMResponseChoice,
  ProxyServerRequest,
  CRSLogsResponse,
  LLMLogsResponse,
  ExecutionInfo,
  ExecutionResponse,
  ArtifactInfo,
  ArtifactListResponse,
  ArtifactContentResponse,
} from '@/lib/types';

export async function listTrialReports(
  outerDir: string,
  experimentName?: string
): Promise<TrialFileInfo[]> {
  const expName = experimentName ?? (await findExperimentName(outerDir));
  if (!expName) {
    console.error(`No experiment found in ${outerDir}`);
    return [];
  }

  const reportDataDir = getReportDataDir(outerDir, expName);
  const trialReportsDir = path.join(reportDataDir, 'trial-reports');

  try {
    const files = await readdir(trialReportsDir);

    // Filter and parse trial files
    const trialFiles: TrialFileInfo[] = [];

    for (const file of files) {
      // Match pattern: trial-{num}.json
      const match = file.match(/^trial-(\d+)\.json$/);
      if (match) {
        trialFiles.push({
          trial_num: parseInt(match[1], 10),
          file_path: path.join(trialReportsDir, file),
        });
      }
    }

    // Sort by trial number
    trialFiles.sort((a, b) => a.trial_num - b.trial_num);

    return trialFiles;
  } catch (err) {
    console.error('Failed to list trial reports:', err);
    return [];
  }
}

export async function loadTrialReport(
  outerDir: string,
  trialNum: number,
  experimentName?: string
): Promise<TrialReport | null> {
  const expName = experimentName ?? (await findExperimentName(outerDir));
  if (!expName) {
    console.error(`No experiment found in ${outerDir}`);
    return null;
  }

  const reportDataDir = getReportDataDir(outerDir, expName);
  const filePath = path.join(reportDataDir, 'trial-reports', `trial-${trialNum}.json`);

  try {
    const content = await readFile(filePath, 'utf-8');
    return JSON.parse(content) as TrialReport;
  } catch (err) {
    console.error(`Failed to load trial report ${trialNum}:`, err);
    return null;
  }
}

/**
 * Load trial report by index in the experiment's trial_summaries array.
 * This is more reliable than using trial_num when multiple trials have the same number.
 */
export async function loadTrialReportByIndex(
  outerDir: string,
  trialIndex: number,
  experimentName?: string
): Promise<TrialReport | null> {
  const { getExperimentReport } = await import('./experiments');

  const expReport = await getExperimentReport(outerDir, experimentName);
  if (!expReport) {
    console.error(`No experiment report found in ${outerDir}`);
    return null;
  }

  const trialSummary = expReport.trial_summaries[trialIndex];
  if (!trialSummary) {
    console.error(`Trial index ${trialIndex} not found in experiment`);
    return null;
  }

  // Build trial report from trial_dir data
  const trialDir = trialSummary.trial_dir;

  // Try to load from trial-reports first (if it exists with unique naming)
  const expName = experimentName ?? (await findExperimentName(outerDir));
  if (expName) {
    const reportDataDir = getReportDataDir(outerDir, expName);
    const trialReportsDir = path.join(reportDataDir, 'trial-reports');

    try {
      // List all trial report files and find one matching this trial_dir
      const files = await readdir(trialReportsDir);
      for (const file of files) {
        if (file.endsWith('.json')) {
          const filePath = path.join(trialReportsDir, file);
          const content = await readFile(filePath, 'utf-8');
          const report = JSON.parse(content) as TrialReport;
          if (report.trial?.trial_dir === trialDir) {
            // Update trial_num to match the index for consistency
            report.trial.trial_num = trialIndex;
            return report;
          }
        }
      }
    } catch (err) {
      console.error('Failed to search trial reports:', err);
    }
  }

  // Fallback: construct a trial report from the summary + trial directory files
  console.warn(`Could not find trial report file for ${trialDir}, using summary data + trial files`);

  // Try to read LLM logs for more complete data
  const llmData = await parseLlmLogsFromTrialDir(trialDir);

  return {
    report_type: 'trial',
    generated_at: expReport.generated_at,
    trial: {
      trial_dir: trialSummary.trial_dir,
      trial_num: trialIndex,
      crs: trialSummary.crs,
      benchmark: trialSummary.benchmark,
      harness: trialSummary.harness,
      mode: trialSummary.mode,
    },
    summary: {
      total_povs_discovered: trialSummary.total_povs,
      unique_povs: trialSummary.unique_povs,
      total_patches_generated: trialSummary.total_patches,
      unique_patches: trialSummary.unique_patches,
      total_llm_cost: llmData?.total_cost ?? trialSummary.total_cost,
      total_llm_tokens: llmData?.total_tokens ?? 0,
      total_time: trialSummary.total_time,
      time_to_first_pov: trialSummary.time_to_first_pov,
      snapshot_count: 0,
    },
    povs: { unique_names: [], count: trialSummary.unique_povs },
    patches: { unique_names: [], count: trialSummary.unique_patches },
    llm_usage: {
      total_tokens: llmData?.total_tokens ?? 0,
      total_cost: llmData?.total_cost ?? trialSummary.total_cost,
      by_model: llmData?.by_model ?? {},
    },
    time_series: llmData?.time_series ?? [],
    timeline: { total_snapshots: 0, snapshots: [] },
  };
}
