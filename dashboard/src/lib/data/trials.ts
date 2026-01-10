import { readdir, readFile } from 'fs/promises';
import path from 'path';

import type { TrialFileInfo, TrialReport } from '@/lib/types';

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
} from '@/lib/types';

function getReportDir(): string {
  const reportDir = process.env.REPORT_DIR;
  if (!reportDir) {
    console.warn('REPORT_DIR environment variable not set, using default ./reports');
    return './reports';
  }
  return reportDir;
}

export async function listTrialReports(
  _experimentName: string
): Promise<TrialFileInfo[]> {
  const reportDir = getReportDir();
  const trialReportsDir = path.join(reportDir, 'trial-reports');

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
  _experimentName: string,
  trialNum: number
): Promise<TrialReport | null> {
  const reportDir = getReportDir();
  const filePath = path.join(reportDir, 'trial-reports', `trial-${trialNum}.json`);

  try {
    const content = await readFile(filePath, 'utf-8');
    return JSON.parse(content) as TrialReport;
  } catch (err) {
    console.error(`Failed to load trial report ${trialNum}:`, err);
    return null;
  }
}
