import { readdir, readFile, stat } from 'fs/promises';
import path from 'path';

import type { ExperimentListItem, ExperimentReport } from '@/lib/types';

// Re-export types for convenience
export type {
  ExperimentSummary,
  CRSMetrics,
  BenchmarkMetrics,
  TrialSummary,
  ExperimentReport,
  ExperimentListItem,
} from '@/lib/types';

/**
 * Get the base directory for experiments.
 * This is the root directory containing experiment directories.
 * Each experiment directory contains experiment-data/ and report-data/ subdirectories.
 */
export function getBaseDir(): string {
  const baseDir = process.env.BASE_DIR;
  if (!baseDir) {
    console.warn('BASE_DIR environment variable not set, using default ./data');
    return './data';
  }
  return baseDir;
}

/**
 * Get experiment data directory path.
 * Contains trial execution data (crs-logs, llm-logs.json, execution.json, etc.)
 *
 * Structure: BASE_DIR/<outer-dir>/experiment-data/<experiment-name>/
 */
export function getExperimentDataDir(outerDir: string, experimentName: string): string {
  return path.join(getBaseDir(), outerDir, 'experiment-data', experimentName);
}

/**
 * Get report data directory path.
 * Contains generated reports (experiment-*.json, trial-reports/)
 *
 * Structure: BASE_DIR/<outer-dir>/report-data/<experiment-name>/
 */
export function getReportDataDir(outerDir: string, experimentName: string): string {
  return path.join(getBaseDir(), outerDir, 'report-data', experimentName);
}

/**
 * List all experiments in the base directory.
 *
 * Structure: BASE_DIR/<outer-dir>/report-data/<experiment-name>/experiment-*.json
 */
export async function listExperiments(): Promise<ExperimentListItem[]> {
  const baseDir = getBaseDir();

  try {
    const outerEntries = await readdir(baseDir, { withFileTypes: true });
    const experiments: ExperimentListItem[] = [];

    for (const outerEntry of outerEntries) {
      if (!outerEntry.isDirectory()) continue;

      const outerDir = outerEntry.name;
      const reportDataPath = path.join(baseDir, outerDir, 'report-data');

      // Check if report-data directory exists
      try {
        await stat(reportDataPath);
      } catch {
        continue;
      }

      // Scan report-data for experiment subdirectories
      const innerEntries = await readdir(reportDataPath, { withFileTypes: true });

      for (const innerEntry of innerEntries) {
        if (!innerEntry.isDirectory()) continue;

        const experimentName = innerEntry.name;
        const experimentDir = path.join(reportDataPath, experimentName);

        // Try to load experiment report
        try {
          const files = await readdir(experimentDir);
          const experimentFile = files.find(
            (f) => f.startsWith('experiment-') && f.endsWith('.json')
          );

          if (experimentFile) {
            const filePath = path.join(experimentDir, experimentFile);
            const content = await readFile(filePath, 'utf-8');
            const report: ExperimentReport = JSON.parse(content);

            // Determine experiment mode from trial summaries
            const modes = new Set(report.trial_summaries.map((t) => t.mode));
            let mode: 'bug_finding' | 'patch_generation' | 'mixed' | null = null;
            if (modes.size === 1) {
              const singleMode = [...modes][0];
              if (singleMode === 'bug_finding' || singleMode === 'patch_generation') {
                mode = singleMode;
              }
            } else if (modes.size > 1) {
              mode = 'mixed';
            }

            experiments.push({
              id: outerDir,
              name: experimentName,
              mode,
              summary: report.summary,
              crs_list: Object.keys(report.by_crs),
              benchmark_list: Object.keys(report.by_benchmark),
            });
          } else {
            experiments.push({
              id: outerDir,
              name: experimentName,
              mode: null,
              summary: null,
              crs_list: [],
              benchmark_list: [],
            });
          }
        } catch (err) {
          console.error(`Failed to parse experiment ${outerDir}/${experimentName}:`, err);
          experiments.push({
            id: outerDir,
            name: experimentName,
            mode: null,
            summary: null,
            crs_list: [],
            benchmark_list: [],
          });
        }
      }
    }

    return experiments;
  } catch (err) {
    console.error('Failed to list experiments:', err);
    return [];
  }
}

/**
 * Find the experiment name inside an outer directory.
 * Scans report-data/ for subdirectories containing experiment-*.json.
 */
export async function findExperimentName(outerDir: string): Promise<string | null> {
  const reportDataPath = path.join(getBaseDir(), outerDir, 'report-data');

  try {
    const entries = await readdir(reportDataPath, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;

      const experimentDir = path.join(reportDataPath, entry.name);
      const files = await readdir(experimentDir);
      const hasExperimentFile = files.some(
        (f) => f.startsWith('experiment-') && f.endsWith('.json')
      );

      if (hasExperimentFile) {
        return entry.name;
      }
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * Get experiment report from report-data directory.
 * If experimentName is not provided, auto-detects it.
 */
export async function getExperimentReport(
  outerDir: string,
  experimentName?: string
): Promise<ExperimentReport | null> {
  const expName = experimentName ?? (await findExperimentName(outerDir));
  if (!expName) {
    console.error(`No experiment found in ${outerDir}`);
    return null;
  }

  const reportDataDir = getReportDataDir(outerDir, expName);

  try {
    const files = await readdir(reportDataDir);
    const experimentFile = files.find(
      (f) => f.startsWith('experiment-') && f.endsWith('.json')
    );

    if (!experimentFile) {
      console.error(`No experiment report found in ${reportDataDir}`);
      return null;
    }

    const filePath = path.join(reportDataDir, experimentFile);
    const content = await readFile(filePath, 'utf-8');
    return JSON.parse(content) as ExperimentReport;
  } catch (err) {
    console.error(`Failed to read experiment report ${outerDir}/${expName}:`, err);
    return null;
  }
}
