import { NextRequest, NextResponse } from 'next/server';
import { readFile, readdir } from 'fs/promises';
import path from 'path';
import { getExperimentReport } from '@/lib/data/experiments';

export interface TrialError {
  trialIndex: number;
  crs: string;
  benchmark: string;
  harness: string;
  logSource: string;
  errors: string[];
}

export interface ExperimentErrorsResponse {
  errors: TrialError[];
  totalErrorCount: number;
  trialsWithErrors: number;
  totalTrials: number;
}

/**
 * Extract [ERROR] lines from CRS log content
 */
function extractErrors(logContent: string): string[] {
  const lines = logContent.split('\n');
  return lines.filter(line =>
    line.includes('[ERROR]') ||
    line.includes('[CRITICAL]') ||
    line.includes('[FATAL]')
  );
}

interface LogResult {
  content: string;
  source: string;
}

/**
 * Try to read CRS log from trial directory
 */
async function readCrsLog(trialDir: string): Promise<LogResult | null> {
  // Try crs-logs directory first
  const crsLogsDir = path.join(trialDir, 'crs-logs');
  try {
    const files = await readdir(crsLogsDir);
    const logFile = files.find(f => f.endsWith('.log'));
    if (logFile) {
      const logPath = path.join(crsLogsDir, logFile);
      const content = await readFile(logPath, 'utf-8');
      return { content, source: logPath };
    }
  } catch {
    // crs-logs dir doesn't exist, try fallback
  }

  // Fallback: try crs-output.log
  try {
    const logPath = path.join(trialDir, 'crs-output.log');
    const content = await readFile(logPath, 'utf-8');
    return { content, source: logPath };
  } catch {
    return null;
  }
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  try {
    const { name } = await params;

    // Load experiment report to get trial info
    const report = await getExperimentReport(name);
    if (!report) {
      return NextResponse.json({ error: 'Experiment not found' }, { status: 404 });
    }

    const trialErrors: TrialError[] = [];
    let totalErrorCount = 0;

    // Process each trial
    for (let i = 0; i < report.trial_summaries.length; i++) {
      const trial = report.trial_summaries[i];
      const logResult = await readCrsLog(trial.trial_dir);

      if (logResult) {
        const errors = extractErrors(logResult.content);
        if (errors.length > 0) {
          trialErrors.push({
            trialIndex: i,
            crs: trial.crs,
            benchmark: trial.benchmark,
            harness: trial.harness,
            logSource: logResult.source,
            errors,
          });
          totalErrorCount += errors.length;
        }
      }
    }

    const response: ExperimentErrorsResponse = {
      errors: trialErrors,
      totalErrorCount,
      trialsWithErrors: trialErrors.length,
      totalTrials: report.trial_summaries.length,
    };

    return NextResponse.json(response);
  } catch (error) {
    console.error('Error getting experiment errors:', error);
    return NextResponse.json(
      { error: 'Failed to get experiment errors' },
      { status: 500 }
    );
  }
}
