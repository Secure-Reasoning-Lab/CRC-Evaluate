import { readdir, readFile } from 'fs/promises';
import path from 'path';

import type {
  ExperimentListItem,
  ExperimentReport,
} from '@/lib/types';

// Re-export types for convenience
export type {
  ExperimentSummary,
  CRSMetrics,
  BenchmarkMetrics,
  TrialSummary,
  ExperimentReport,
  ExperimentListItem,
} from '@/lib/types';

function getReportDir(): string {
  const reportDir = process.env.REPORT_DIR;
  if (!reportDir) {
    console.warn('REPORT_DIR environment variable not set, using default ./reports');
    return './reports';
  }
  return reportDir;
}

export async function listExperiments(): Promise<ExperimentListItem[]> {
  const reportDir = getReportDir();

  try {
    const files = await readdir(reportDir);
    const experimentFiles = files.filter(
      (f) => f.startsWith('experiment-') && f.endsWith('.json')
    );

    const experiments: ExperimentListItem[] = [];

    for (const file of experimentFiles) {
      try {
        const filePath = path.join(reportDir, file);
        const content = await readFile(filePath, 'utf-8');
        const report: ExperimentReport = JSON.parse(content);

        // Extract name from filename (experiment-{name}.json)
        const name = file.replace('experiment-', '').replace('.json', '');

        experiments.push({
          name,
          summary: report.summary,
          crs_list: Object.keys(report.by_crs),
          benchmark_list: Object.keys(report.by_benchmark),
        });
      } catch (err) {
        console.error(`Failed to parse ${file}:`, err);
      }
    }

    return experiments;
  } catch (err) {
    console.error('Failed to list experiments:', err);
    return [];
  }
}

export async function getExperimentReport(
  name: string
): Promise<ExperimentReport | null> {
  const reportDir = getReportDir();
  const filePath = path.join(reportDir, `experiment-${name}.json`);

  try {
    const content = await readFile(filePath, 'utf-8');
    return JSON.parse(content) as ExperimentReport;
  } catch (err) {
    console.error(`Failed to read experiment report ${name}:`, err);
    return null;
  }
}
