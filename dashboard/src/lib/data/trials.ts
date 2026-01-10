import { readdir, readFile } from 'fs/promises';
import path from 'path';

import type { TrialFileInfo, TrialReport } from '@/lib/types';
import { findExperimentName, getReportDataDir } from './experiments';

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
